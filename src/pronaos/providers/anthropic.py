"""Anthropic provider adapter.

Translates OpenAI-shape chat requests to Anthropic's Messages API and yields
OpenAI-shape chunks back to the router. Uses raw httpx so the wire protocol is
mockable directly with respx and we are not coupled to SDK version churn.

Design notes
------------
- ``system`` messages in the OpenAI request are hoisted to Anthropic's
  top-level ``system`` parameter; Anthropic rejects ``system`` as a message
  role.
- Anthropic requires ``max_tokens``; if the caller omitted it we fall back to
  ``DEFAULT_MAX_TOKENS``. This is documented in the adapter rather than
  silently guessed at the router layer.
- Model names may be passed as ``anthropic/<name>`` or bare ``<name>``.
- Pricing constants are maintained by hand here — update when Anthropic
  changes their price list. Hundredths-of-a-cent precision is deliberate so
  we never drop sub-cent usage to rounding.
- Streaming: we translate Anthropic's multi-event SSE into a single stream of
  ChatCompletionChunk objects. The first emitted chunks carry content deltas;
  a final sentinel chunk carries the finish reason and usage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx

from pronaos.providers.base import (
    AuthError,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
    RateLimitError,
    UpstreamTimeoutError,
)

ANTHROPIC_API_URL: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2023-06-01"
DEFAULT_MAX_TOKENS: Final = 4096


@dataclass(frozen=True, slots=True)
class _Price:
    input_hcents_per_mtok: int
    output_hcents_per_mtok: int


_PRICING: Final[dict[str, _Price]] = {
    "claude-opus-4-7": _Price(input_hcents_per_mtok=1_500_000, output_hcents_per_mtok=7_500_000),
    "claude-sonnet-4-6": _Price(input_hcents_per_mtok=300_000, output_hcents_per_mtok=1_500_000),
    "claude-haiku-4-5": _Price(input_hcents_per_mtok=80_000, output_hcents_per_mtok=400_000),
}


def _strip_prefix(model: str) -> str:
    return model.removeprefix("anthropic/")


def _split_system(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Split OpenAI-style messages into (system_prompt, non_system_messages)."""
    systems = [m["content"] for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]
    return ("\n\n".join(systems) if systems else None), others


def _translate_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rewrite an OpenAI-shape message list into Anthropic's Messages API shape.

    Two shape gaps to bridge for the agent loop to work end-to-end:

    1. **Tool-result echoes.** OpenAI represents each tool result as a
       standalone ``{"role":"tool", "tool_call_id":..., "content":...}``
       message. Anthropic represents the same as a ``user`` message
       whose content is a list of ``{"type":"tool_result",
       "tool_use_id":..., "content":...}`` blocks. Anthropic ALSO
       requires that all tool_result blocks for one assistant turn
       arrive in a single user message — so we coalesce consecutive
       OpenAI tool messages into one Anthropic user message.

    2. **Assistant tool_calls echoes.** When the client sends back the
       previous turn's assistant message containing ``tool_calls``,
       OpenAI uses ``{"role":"assistant", "content":null,
       "tool_calls":[...]}`` with arguments as a JSON string;
       Anthropic uses ``{"role":"assistant",
       "content":[{"type":"text",...}, {"type":"tool_use",
       "id":..., "name":..., "input":<object>}]}`` with input as a
       parsed object.

    Plain user/assistant text messages pass through unchanged
    (Anthropic accepts string content for either role).
    """
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        role = m.get("role")

        if role == "tool":
            # Coalesce with adjacent tool messages — they're all results
            # paired with the SAME prior assistant tool_use batch.
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                # OpenAI sends content as a string (usually JSON-encoded
                # result). Anthropic accepts string content on a
                # tool_result block, so no further translation needed.
                "content": m.get("content") if m.get("content") is not None else "",
            }
            pending_tool_results.append(block)
            continue

        # Any non-tool message terminates the tool_result run.
        _flush_tool_results()

        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = m.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            for tc in m["tool_calls"]:
                func = tc.get("function") or {}
                args_str = func.get("arguments") or "{}"
                # OpenAI ships arguments as a JSON STRING; Anthropic
                # wants the parsed object. If the string isn't valid
                # JSON we fall back to the literal string under a
                # synthetic key so the model still sees something —
                # garbage in / garbage out, but no crash.
                try:
                    args_obj: Any = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args_obj = {"_raw": args_str}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args_obj,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue

        # Plain user / assistant text — pass through, but trim the
        # OpenAI-extra fields (tool_call_id, tool_calls, name) that
        # Anthropic doesn't model. The handler already strips ``None``
        # fields, but a client could still pass extras here.
        #
        # Phase 41: multi-modal content. When ``content`` is a list of
        # OpenAI-shape parts (text + image_url), translate to
        # Anthropic's block shape (text passes through; image_url
        # becomes image-with-source). Single-string content stays
        # a string — Anthropic accepts both.
        raw_content = m.get("content", "")
        cleaned_content: str | list[dict[str, Any]]
        if isinstance(raw_content, list):
            cleaned_content = _translate_content_parts_to_anthropic(raw_content)
        else:
            cleaned_content = raw_content if raw_content is not None else ""
        cleaned = {"role": role, "content": cleaned_content}
        out.append(cleaned)

    # Tail flush — covers the (unusual) case where the last message
    # is a tool result, e.g. tests that drive a single-turn round trip.
    _flush_tool_results()
    return out


def _translate_content_parts_to_anthropic(
    parts: list[Any],
) -> list[dict[str, Any]]:
    """Translate OpenAI-shape multi-modal parts to Anthropic block shape.

    Phase 41 helper: a request from the client carries
    ``content: [{"type":"text",...}, {"type":"image_url","image_url":{"url":"..."}}]``.
    Anthropic wants text parts as-is plus ``image`` blocks with
    ``source.type=base64`` (for data URIs) or ``source.type=url``
    (for HTTPS).

    Reuses the production translator in ``core.multimodal`` so the
    translation logic stays in one place. Anthropic-native parts
    (``{"type":"image","source":{...}}``) pass through unchanged.
    """
    # Local import — avoid a circular import at module load time. The
    # core module doesn't depend on providers, but providers/* depends
    # on core/* through several other paths, so we keep this lazy.
    from pronaos.core.multimodal import translate_messages_for_anthropic

    # ``translate_messages_for_anthropic`` is per-message; wrap parts
    # in a one-message envelope to reuse it, then unwrap.
    wrapped = [{"role": "user", "content": parts}]
    translated = translate_messages_for_anthropic(wrapped)
    return translated[0]["content"]  # type: ignore[no-any-return]


def _finish_reason(anthropic_stop_reason: str | None) -> str | None:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(anthropic_stop_reason or "", anthropic_stop_reason)


async def _parse_sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON payloads from SSE ``data:`` lines. Non-JSON or ``[DONE]`` skipped."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            # Malformed event mid-stream: skip rather than crash the whole response.
            continue


class AnthropicProvider(Provider):
    """Adapter for Anthropic's Messages API."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise AuthError("anthropic: missing api key")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ---- Provider interface ---------------------------------------------------

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        if req.stream:
            return self._chat_streaming(req)
        return await self._chat_non_streaming(req)

    def cost_cents(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        *,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> int:
        """Return call cost in hundredths of a cent; ``0`` for unknown models.

        Anthropic prompt-cache pricing (per Anthropic docs):
          - Regular input tokens: 1.0x input_hcents_per_mtok
          - Cache writes (cache_creation_input_tokens): 1.25x (25% premium
            for creating the cache entry — Anthropic charges this once)
          - Cache reads (cache_read_input_tokens): 0.10x (90% discount —
            this is the FinOps win)
          - Output tokens: output_hcents_per_mtok unchanged

        Integer math: scale numerators by 100 (cache write multiplier
        becomes 125; cache read becomes 10), divide by 100_000_000.
        Avoids float drift on big token counts.
        """
        price = _PRICING.get(_strip_prefix(model))
        if price is None:
            return 0
        # Regular non-cached input.
        input_cost = prompt_tokens * price.input_hcents_per_mtok // 1_000_000
        # Cache writes — 1.25x regular rate.
        cache_write_cost = cache_creation_tokens * price.input_hcents_per_mtok * 125 // 100_000_000
        # Cache reads — 0.10x regular rate.
        cache_read_cost = cache_read_tokens * price.input_hcents_per_mtok * 10 // 100_000_000
        output_cost = completion_tokens * price.output_hcents_per_mtok // 1_000_000
        return input_cost + cache_write_cost + cache_read_cost + output_cost

    # ---- Non-streaming --------------------------------------------------------

    async def _chat_non_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        body = self._build_request_body(req)
        headers = self._build_headers()

        try:
            resp = await self._http.post(ANTHROPIC_API_URL, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("anthropic: upstream timeout") from e

        self._raise_for_status(resp)
        data = resp.json()
        chunk = self._build_response_chunk(data)

        async def _single() -> AsyncIterator[ChatCompletionChunk]:
            yield chunk

        return _single()

    # ---- Streaming ------------------------------------------------------------

    async def _chat_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        body = {**self._build_request_body(req), "stream": True}
        headers = self._build_headers()

        prompt_tokens: int = 0
        completion_tokens: int = 0
        # Phase 34: Anthropic prompt-cache usage fields. Anthropic emits
        # cache_creation_input_tokens + cache_read_input_tokens alongside
        # input_tokens in the message_start event. ``input_tokens`` already
        # EXCLUDES the cached portion — adding the three gives the total
        # input set Anthropic processed.
        cache_creation_tokens: int = 0
        cache_read_tokens: int = 0
        stop_reason: str | None = None
        # Phase 16: streaming tool_use accumulator.
        #
        # Anthropic's streaming SSE shape for a tool call is:
        #   content_block_start  {index:N, content_block:{type:"tool_use",
        #                         id:"toolu_...", name:"X", input:{}}}
        #   content_block_delta  {index:N, delta:{type:"input_json_delta",
        #                         partial_json:"{\""}}        ← multiple of these
        #   content_block_delta  {index:N, delta:{type:"input_json_delta",
        #                         partial_json:"city\":\"Paris\"}"}}
        #   content_block_stop   {index:N}
        #
        # Parallel tool calls live at different content_block indices. We
        # accumulate per-index, then translate the assembled blocks to
        # OpenAI tool_calls shape on the tail chunk — symmetric with the
        # OpenAI-compat adapter's streaming-tools path.
        tool_use_blocks: dict[int, dict[str, Any]] = {}
        # Phase 56: thinking blocks arrive as a content_block_start with
        # type="thinking" followed by content_block_delta events with
        # delta.type="thinking_delta" carrying .thinking text fragments.
        # Accumulate per-block-index so parallel thinking + text blocks
        # (rare today but spec-allowed) don't collide.
        thinking_blocks: dict[int, str] = {}

        try:
            async with self._http.stream(
                "POST", ANTHROPIC_API_URL, json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for_status(resp)

                async for event in _parse_sse(resp):
                    etype = event.get("type")
                    if etype == "message_start":
                        usage = event.get("message", {}).get("usage", {}) or {}
                        prompt_tokens = usage.get("input_tokens", 0) or 0
                        # Phase 34: pull cache stats. Both fields are
                        # absent when the client didn't use cache_control.
                        cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
                        cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        btype = block.get("type")
                        if btype == "tool_use":
                            idx = event.get("index", 0)
                            tool_use_blocks[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                # Anthropic sends ``input: {}`` here; the
                                # actual argument JSON arrives via
                                # subsequent input_json_delta events.
                                "arguments_str": "",
                            }
                        elif btype == "thinking":
                            # Initial thinking text may be present on
                            # the start event itself, or arrive entirely
                            # via subsequent thinking_delta events.
                            idx = event.get("index", 0)
                            initial = block.get("thinking") or ""
                            thinking_blocks[idx] = initial if isinstance(initial, str) else ""
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {}) or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            yield ChatCompletionChunk(
                                content_delta=delta.get("text", ""),
                                finish_reason=None,
                            )
                        elif dtype == "input_json_delta":
                            idx = event.get("index", 0)
                            entry = tool_use_blocks.get(idx)
                            if entry is not None:
                                # Append the partial_json fragment. We
                                # ONLY validate the JSON when we assemble
                                # the final tool_call — Anthropic chunks
                                # the JSON byte-stream and only the
                                # concatenation is parseable.
                                entry["arguments_str"] += delta.get("partial_json", "")
                        elif dtype == "thinking_delta":
                            # Phase 56: accumulate CoT text. We don't
                            # surface thinking as content_delta — most
                            # clients (and the SSE wire shape) expect
                            # content_delta to be the user-visible text.
                            # CoT lands on the terminal chunk only.
                            idx = event.get("index", 0)
                            thinking_blocks[idx] = thinking_blocks.get(idx, "") + (
                                delta.get("thinking", "") or ""
                            )
                    elif etype == "message_delta":
                        usage = event.get("usage", {}) or {}
                        completion_tokens = usage.get("output_tokens", 0) or 0
                        stop_reason = (event.get("delta", {}) or {}).get("stop_reason")
                    # content_block_stop / ping / message_stop carry no
                    # OpenAI-relevant signal — already accumulated all the
                    # input fragments by the time stop arrives.
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("anthropic: upstream timeout") from e

        # Assemble OpenAI-shape tool_calls from the accumulated blocks.
        # Order by content_block index so the output matches the order
        # Anthropic emitted (which matches the order the model decided).
        assembled_tool_calls: list[dict[str, Any]] | None = None
        if tool_use_blocks:
            assembled_tool_calls = []
            for idx in sorted(tool_use_blocks.keys()):
                blk = tool_use_blocks[idx]
                # Empty input is valid (tool takes no arguments) — emit
                # "{}" so the wire shape matches what non-streaming
                # produces from a tool with empty input.
                args_str = blk["arguments_str"] or "{}"
                assembled_tool_calls.append(
                    {
                        "id": blk["id"],
                        "type": "function",
                        "function": {
                            "name": blk["name"],
                            "arguments": args_str,
                        },
                    }
                )

        # Phase 56: assemble accumulated thinking text + estimate count.
        # ~4 chars/token ceil-rounded, matches the non-streaming helper.
        reasoning_content: str | None = None
        reasoning_tokens = 0
        if thinking_blocks:
            ordered = [thinking_blocks[i] for i in sorted(thinking_blocks.keys())]
            combined = "\n\n".join(t for t in ordered if t)
            if combined:
                reasoning_content = combined
                reasoning_tokens = (len(combined) + 3) // 4

        # Final sentinel chunk carrying finish reason, usage totals, and
        # (if the model called tools) the assembled tool_calls list.
        yield ChatCompletionChunk(
            content_delta="",
            finish_reason=_finish_reason(stop_reason)
            or ("tool_calls" if assembled_tool_calls else "stop"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_calls=assembled_tool_calls,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
        )

    # ---- Helpers --------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _build_request_body(self, req: ChatCompletionRequest) -> dict[str, Any]:
        system, messages = _split_system(req.messages)
        # Tool-result loop: translate OpenAI tool/assistant-echo shapes
        # to Anthropic equivalents. Plain user/assistant text messages
        # pass through unchanged.
        messages = _translate_messages_to_anthropic(messages)

        body: dict[str, Any] = {
            "model": _strip_prefix(req.model),
            "messages": messages,
            "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system is not None:
            body["system"] = system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.tools is not None:
            # Translate OpenAI tool shape → Anthropic. The two schemas
            # differ in two places:
            #   - OpenAI nests under {"type":"function","function":{...}}
            #   - OpenAI calls the schema "parameters"; Anthropic calls it "input_schema"
            body["tools"] = [_translate_tool_to_anthropic(t) for t in req.tools]
        if req.tool_choice is not None:
            body["tool_choice"] = _translate_tool_choice_to_anthropic(req.tool_choice)
        if req.extra:
            for k, v in req.extra.items():
                body.setdefault(k, v)
        return body

    @staticmethod
    def _build_response_chunk(data: dict[str, Any]) -> ChatCompletionChunk:
        # Anthropic response content is a list of typed blocks. Text blocks
        # carry the assistant message; tool_use blocks carry tool invocations.
        # We extract both and synthesise OpenAI-shape tool_calls so clients
        # pinned to the OpenAI schema get a uniform response regardless of
        # whether the upstream was Anthropic or Groq/OpenAI/etc.
        content_blocks = data.get("content", []) or []
        text_blocks = [b["text"] for b in content_blocks if b.get("type") == "text"]
        tool_calls = _translate_tool_uses_to_openai(content_blocks)
        # Phase 56: extract extended-thinking content. Anthropic's
        # thinking tokens are already counted in output_tokens, so the
        # estimate is purely for visibility — no double-counting.
        reasoning_content, reasoning_tokens = _extract_thinking_from_content_blocks(
            content_blocks
        )
        usage = data.get("usage", {}) or {}
        return ChatCompletionChunk(
            content_delta="".join(text_blocks),
            finish_reason=_finish_reason(data.get("stop_reason")),
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            tool_calls=tool_calls if tool_calls else None,
            raw=data,
            # Phase 34: Anthropic prompt-cache stats. Both fields are
            # absent when the client didn't use cache_control.
            cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
            # Phase 56: extended-thinking surface.
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        # Defensive in the error path: we never want secondary decode failures
        # to mask the original upstream error.
        try:
            payload = resp.json()
            detail = payload.get("error", {}).get("message") or payload.get("error", {}).get("type")
        except Exception:
            detail = resp.text[:200]

        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"anthropic: auth failed: {detail}")
        if status == 429:
            raise RateLimitError(f"anthropic: rate limited: {detail}")
        if status >= 500:
            raise ProviderError(
                f"anthropic: upstream {status}: {detail}",
                status=502,
                retryable=True,
            )
        raise ProviderError(
            f"anthropic: {status}: {detail}",
            status=400,
            retryable=False,
        )


# --------------------------------------------------------------------------- #
# Tool translation (Phase 12)                                                 #
# --------------------------------------------------------------------------- #
#
# Two schemas to bridge:
#
#   OpenAI tool definition:
#     {"type": "function",
#      "function": {"name": ..., "description": ..., "parameters": <jsonschema>}}
#
#   Anthropic tool definition:
#     {"name": ..., "description": ..., "input_schema": <jsonschema>}
#
# We accept the OpenAI shape (the gateway's public surface) and translate
# both directions so clients pinned to the OpenAI schema get correct
# behaviour regardless of upstream.


def _translate_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI tool definition → Anthropic tool definition.

    Defensive: if the tool already looks Anthropic-shaped (has top-level
    ``input_schema``), we pass it through unchanged. That lets advanced
    callers supply the native shape directly when they're sending an
    Anthropic-specific request and don't want translation."""
    if "input_schema" in tool and "name" in tool:
        return tool  # already Anthropic-shaped
    func = tool.get("function") or {}
    return {
        "name": func.get("name") or tool.get("name") or "",
        "description": func.get("description") or tool.get("description") or "",
        # OpenAI calls this ``parameters``; Anthropic calls it ``input_schema``.
        # Both are JSON Schema objects, so no schema-level translation needed.
        "input_schema": func.get("parameters") or tool.get("input_schema") or {},
    }


def _translate_tool_choice_to_anthropic(
    tool_choice: str | dict[str, Any],
) -> dict[str, Any]:
    """OpenAI tool_choice → Anthropic tool_choice.

    OpenAI shape values:
        "auto"     → Anthropic {"type": "auto"}
        "required" → Anthropic {"type": "any"}   (force-any-tool)
        "none"     → Anthropic has no equivalent; we pass {"type": "auto"}
                     and rely on the caller having omitted ``tools`` to
                     signal "no tool calls"
        {"type":"function","function":{"name":"X"}} → {"type":"tool","name":"X"}
    """
    if isinstance(tool_choice, str):
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice in ("auto", "none"):
            return {"type": "auto"}
        # Unknown string — be conservative, fall back to auto.
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            name = (tool_choice.get("function") or {}).get("name") or ""
            return {"type": "tool", "name": name}
        # Already Anthropic-shaped or unknown — pass through.
        return tool_choice
    return {"type": "auto"}


def _extract_thinking_from_content_blocks(
    content_blocks: list[dict[str, Any]],
) -> tuple[str | None, int]:
    """Phase 56: pull extended-thinking content out of an Anthropic
    response's content array.

    Anthropic's extended-thinking shape inserts ``{"type": "thinking",
    "thinking": "<cot text>", "signature": "..."}`` blocks BEFORE the
    text blocks. The signature is opaque to Pronaos — it's the
    upstream's tamper-detection token; we don't validate or persist it
    here.

    Returns ``(reasoning_content, estimated_tokens)``. When no thinking
    blocks are present, returns ``(None, 0)``.

    Anthropic does NOT expose a separate thinking-token count in its
    usage block — those tokens are counted toward ``output_tokens``
    already. Pronaos estimates the count from the text length using
    the conservative ~4-chars-per-token heuristic Anthropic publishes
    for English; this is for FinOps visibility, not billing (cost math
    on output_tokens already covers it).
    """
    pieces: list[str] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "thinking":
            continue
        text = block.get("thinking")
        if isinstance(text, str) and text:
            pieces.append(text)
    if not pieces:
        return None, 0
    combined = "\n\n".join(pieces)
    # ceil-divide so a 1-char string still scores >= 1 token (~4 chars
    # per token, rounded up — matches Anthropic's published heuristic
    # for English-language token estimation).
    estimated = (len(combined) + 3) // 4
    return combined, estimated


def _translate_tool_uses_to_openai(
    content_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Anthropic ``content[].type == "tool_use"`` blocks → OpenAI tool_calls.

    Each Anthropic tool_use block:
        {"type":"tool_use", "id":..., "name":..., "input": <object>}

    Each OpenAI tool_call:
        {"id":..., "type":"function", "function":{"name":..., "arguments": <json-string>}}

    Note that ``arguments`` in OpenAI is a JSON-encoded STRING, while
    Anthropic's ``input`` is a parsed object. Clients pinned to OpenAI
    parse the string themselves; we do the encoding here so the wire
    shape matches exactly."""
    tool_calls: list[dict[str, Any]] = []
    for block in content_blocks:
        if block.get("type") != "tool_use":
            continue
        tool_calls.append(
            {
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(
                        block.get("input") or {},
                        separators=(",", ":"),
                        default=str,
                    ),
                },
            }
        )
    return tool_calls
