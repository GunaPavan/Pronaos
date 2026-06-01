"""Google Cloud Vertex AI provider adapter (Phase 53).

Vertex AI is GCP's managed-foundation-model API — the GCP equivalent
of AWS Bedrock. Like Bedrock, it hosts multiple model families behind
one HTTPS endpoint, with per-family wire-shape differences:

- **Gemini** (publisher ``google``): Vertex's native shape — ``contents``
  array (NOT ``messages``), per-content ``parts`` (text / functionCall /
  functionResponse / inlineData), ``generationConfig`` for params,
  ``tools.functionDeclarations`` for function calling.
- **Claude on Vertex** (publisher ``anthropic``): Anthropic's Messages
  shape (identical to direct Anthropic) but with an
  ``anthropic_version: "vertex-2023-10-16"`` discriminator and the
  model in the URL.

Auth: GCP service-account JWT bearer flow via
:mod:`pronaos.providers.vertex_auth`. No google-auth dep on the hot
path.

Streaming: Vertex emits SSE (lucky us — no binary framing like
Bedrock) at the ``:streamGenerateContent?alt=sse`` action for Gemini
and at the standard Anthropic SSE shape for Claude-on-Vertex.

Model-ID convention
-------------------
Pronaos uses ``vertex/{publisher}/{model}``. Examples::

    vertex/google/gemini-1.5-flash
    vertex/google/gemini-2.0-flash
    vertex/anthropic/claude-3-5-haiku@20241022

The ``@version`` suffix matches Anthropic's Vertex-side naming
convention exactly.
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
from pronaos.providers.catalog import get_pricing
from pronaos.providers.vertex_auth import VertexAuth, VertexAuthError

# Anthropic-on-Vertex requires this exact string in the request body
# (different from direct Anthropic and from Anthropic-on-Bedrock).
ANTHROPIC_VERTEX_VERSION: Final = "vertex-2023-10-16"

# Default generation budget if the caller didn't specify max_tokens.
# Aligned with the direct Anthropic / Bedrock adapters' default.
DEFAULT_MAX_TOKENS: Final = 4096


# --------------------------------------------------------------------------- #
# Model-ID parsing                                                            #
# --------------------------------------------------------------------------- #


def _strip_prefix(model: str) -> str:
    """``vertex/google/gemini-1.5-flash`` -> ``google/gemini-1.5-flash``."""
    return model.removeprefix("vertex/")


def _split_publisher_model(model_id: str) -> tuple[str, str]:
    """Split ``publisher/model`` into its components.

    Raises if the model ID doesn't have a publisher prefix — Vertex
    URLs need both pieces, so a request that forgot one is a routing
    bug we want to fail loud on.
    """
    if "/" not in model_id:
        raise ProviderError(
            f"vertex: model {model_id!r} missing publisher prefix "
            f"(expected 'publisher/model', e.g. 'google/gemini-1.5-flash')",
            status=400,
        )
    publisher, _, model = model_id.partition("/")
    return publisher, model


# --------------------------------------------------------------------------- #
# Per-family request translators                                              #
# --------------------------------------------------------------------------- #


def _build_gemini_body(req: ChatCompletionRequest) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Gemini's body shape.

    Mapping:
    - ``role: system`` messages → top-level ``systemInstruction``
      (Vertex doesn't put system in ``contents``; it's a separate
      sibling key).
    - ``role: user`` / ``role: assistant`` → ``contents[]`` entries
      with role ``user`` / ``model`` (note: Vertex says ``model``,
      not ``assistant``).
    - ``content: "string"`` → one ``part: {text: ...}``.
    - ``content: [parts]`` with OpenAI image_url parts → Vertex
      ``inlineData`` / ``fileData`` parts (deferred to a follow-up;
      Phase 53 ships text-only for Gemini multimodal).
    - ``max_tokens`` → ``generationConfig.maxOutputTokens``.
    - ``temperature`` / ``top_p`` → ``generationConfig.*``.
    - ``tools`` → ``tools[0].functionDeclarations`` (Vertex wraps
      tool defs in one object, not a flat array).
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in req.messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        system_parts.append(p.get("text") or "")
            continue
        # Map role + content into Vertex shape.
        vertex_role = "model" if role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append({"text": p.get("text") or ""})
                # Image / vision content for Gemini is a follow-up.
        if parts:
            contents.append({"role": vertex_role, "parts": parts})

    body: dict[str, Any] = {"contents": contents}

    if system_parts:
        body["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_parts)}],
        }

    generation_config: dict[str, Any] = {}
    if req.max_tokens is not None:
        generation_config["maxOutputTokens"] = req.max_tokens
    else:
        generation_config["maxOutputTokens"] = DEFAULT_MAX_TOKENS
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature
    # ``top_p`` is not a field on Pronaos's normalized ChatCompletionRequest;
    # it lives in the OpenAI-compat passthrough's ``extra`` map. If a
    # team ships top_p via that channel and routes to Vertex, the
    # current adapter ignores it (matching how Bedrock handles
    # provider-specific knobs today).
    if generation_config:
        body["generationConfig"] = generation_config

    if req.tools:
        function_declarations = []
        for tool in req.tools:
            func = tool.get("function") or {}
            function_declarations.append(
                {
                    "name": func.get("name") or tool.get("name") or "",
                    "description": (func.get("description") or tool.get("description") or ""),
                    "parameters": (
                        func.get("parameters")
                        or tool.get("input_schema")
                        or {"type": "object", "properties": {}}
                    ),
                }
            )
        body["tools"] = [{"functionDeclarations": function_declarations}]

    return body


def _build_anthropic_on_vertex_body(req: ChatCompletionRequest) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Anthropic-on-Vertex body.

    Same shape as direct Anthropic Messages API EXCEPT:
    - No ``model`` field (model is in the URL).
    - Adds ``anthropic_version: "vertex-2023-10-16"``.

    Anthropic-on-Vertex's wire shape is otherwise identical to the
    direct Anthropic API: system as a sibling, ``messages[]`` with
    role + content, tools as ``[{name, description, input_schema}]``.
    """
    systems = [m["content"] for m in req.messages if m.get("role") == "system"]
    others = [m for m in req.messages if m.get("role") != "system"]

    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERTEX_VERSION,
        "messages": others,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if systems:
        body["system"] = "\n\n".join(s for s in systems if isinstance(s, str))
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools is not None:
        anthropic_tools = []
        for tool in req.tools:
            if "input_schema" in tool and "name" in tool:
                anthropic_tools.append(tool)
                continue
            func = tool.get("function") or {}
            anthropic_tools.append(
                {
                    "name": func.get("name") or tool.get("name") or "",
                    "description": (func.get("description") or tool.get("description") or ""),
                    "input_schema": (func.get("parameters") or tool.get("input_schema") or {}),
                }
            )
        body["tools"] = anthropic_tools
    return body


# --------------------------------------------------------------------------- #
# Per-family response translators (non-streaming)                             #
# --------------------------------------------------------------------------- #


def _parse_gemini_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Gemini's non-streaming response shape::

    {
      "candidates": [{
        "content": {"role": "model", "parts": [{"text": "..."}]},
        "finishReason": "STOP" | "MAX_TOKENS" | ...,
      }],
      "usageMetadata": {
        "promptTokenCount": N,
        "candidatesTokenCount": N,
        "totalTokenCount": N
      }
    }
    """
    candidates = data.get("candidates") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_raw: str | None = None
    if candidates:
        c = candidates[0]
        finish_raw = c.get("finishReason")
        content = c.get("content") or {}
        for p in content.get("parts", []) or []:
            if not isinstance(p, dict):
                continue
            if isinstance(p.get("text"), str):
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"] or {}
                tool_calls.append(
                    {
                        # Gemini doesn't return an explicit id on function
                        # calls; OpenAI-compat clients expect one. We
                        # synthesize a stable-per-call id from the name.
                        "id": f"vertex_{fc.get('name', 'func')}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args") or {}, separators=(",", ":")),
                        },
                    }
                )
    finish = _gemini_finish_to_openai(finish_raw)
    usage = data.get("usageMetadata") or {}
    # Phase 56: Gemini 2.0 Flash Thinking / 2.5 Pro report thinking
    # tokens in usageMetadata.thoughtsTokenCount. That count is BILLED
    # at the same per-token rate as output BUT is EXCLUDED from
    # candidatesTokenCount. Without this fix Pronaos was under-billing
    # by 100% of the thinking portion on Gemini thinking-mode requests.
    # We add thoughts → completion_tokens so the chat handler's cost
    # math (which multiplies completion_tokens by output_rate) bills it
    # correctly. The raw thoughts count ALSO lands in reasoning_tokens
    # for visibility / header surfacing.
    candidates = usage.get("candidatesTokenCount")
    thoughts = usage.get("thoughtsTokenCount") or 0
    if isinstance(thoughts, int) and thoughts > 0 and isinstance(candidates, int):
        billable_completion: int | None = candidates + thoughts
    else:
        billable_completion = candidates
    reasoning_tokens = int(thoughts) if isinstance(thoughts, int) else 0
    return ChatCompletionChunk(
        content_delta="".join(text_parts),
        finish_reason=finish,
        prompt_tokens=usage.get("promptTokenCount"),
        completion_tokens=billable_completion,
        tool_calls=tool_calls or None,
        raw=data,
        reasoning_tokens=reasoning_tokens,
    )


def _gemini_finish_to_openai(reason: str | None) -> str | None:
    """Map Vertex's UPPER_CASE finish reasons to OpenAI-compat values."""
    if reason is None:
        return None
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "OTHER": "stop",
        "FINISH_REASON_UNSPECIFIED": None,
        "TOOL_USE": "tool_calls",
    }
    return mapping.get(reason, reason.lower())


def _parse_anthropic_on_vertex_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Anthropic-on-Vertex non-streaming response = same shape as direct
    Anthropic. Reuse the same parsing logic the Bedrock adapter uses
    for Anthropic-on-Bedrock (kept inline to avoid an inter-adapter
    import cycle)."""
    content_blocks = data.get("content", []) or []
    text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
    tool_calls: list[dict[str, Any]] = []
    for b in content_blocks:
        if b.get("type") != "tool_use":
            continue
        tool_calls.append(
            {
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input") or {}, separators=(",", ":")),
                },
            }
        )
    # Phase 56: extended-thinking surface. Anthropic-on-Vertex returns
    # the same content_block shape as direct Anthropic, so thinking
    # blocks land here too. Same estimation heuristic as direct /
    # Bedrock (no separate thinking-token count in usage — they're in
    # output_tokens already).
    thinking_pieces: list[str] = []
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "thinking":
            text = b.get("thinking")
            if isinstance(text, str) and text:
                thinking_pieces.append(text)
    reasoning_content: str | None = None
    reasoning_tokens = 0
    if thinking_pieces:
        reasoning_content = "\n\n".join(thinking_pieces)
        reasoning_tokens = (len(reasoning_content) + 3) // 4
    usage = data.get("usage", {}) or {}
    stop_reason = data.get("stop_reason")
    finish_map = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }
    finish = finish_map.get(stop_reason or "", stop_reason)
    return ChatCompletionChunk(
        content_delta="".join(text_blocks),
        finish_reason=finish,
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
        tool_calls=tool_calls or None,
        raw=data,
        # Phase 55: Anthropic-on-Vertex surfaces the same prompt-cache
        # usage fields as direct Anthropic + Anthropic-on-Bedrock.
        # Same Claude bytes, same wire format, same FinOps story —
        # weighted cost math (1.25x write, 0.10x read) applied in
        # ``cost_cents`` below.
        cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
        # Phase 56: extended-thinking surface.
        reasoning_tokens=reasoning_tokens,
        reasoning_content=reasoning_content,
    )


# --------------------------------------------------------------------------- #
# Per-family streaming-event translators                                      #
# --------------------------------------------------------------------------- #
#
# Both Gemini and Anthropic-on-Vertex emit SSE (``data: <json>\n\n``).
# The per-family streaming-event translator takes one decoded SSE
# event and emits 0..1 ChatCompletionChunk instances. State is
# threaded through the second arg (``state``) so token counts and
# tool-call accumulation survive across chunks.


def _translate_gemini_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """One Gemini streaming event = one ``candidates[0].content.parts``
    delta + optional ``finishReason`` + optional ``usageMetadata``."""
    candidates = event.get("candidates") or []
    if not candidates:
        # Some streams begin with a ``promptFeedback`` event that has
        # no candidates — skip.
        return None
    c = candidates[0]
    delta_text_parts: list[str] = []
    incremental_tool_calls: list[dict[str, Any]] = []
    content = c.get("content") or {}
    for p in content.get("parts", []) or []:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("text"), str):
            delta_text_parts.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"] or {}
            incremental_tool_calls.append(
                {
                    "id": f"vertex_{fc.get('name', 'func')}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args") or {}, separators=(",", ":")),
                    },
                }
            )
    delta_text = "".join(delta_text_parts)

    # Usage lives on ``usageMetadata`` and only appears on the final
    # event; stash it in state for the finish-bearing chunk.
    usage = event.get("usageMetadata") or {}
    if isinstance(usage.get("promptTokenCount"), int):
        state["prompt_tokens"] = usage["promptTokenCount"]
    if isinstance(usage.get("candidatesTokenCount"), int):
        state["completion_tokens"] = usage["candidatesTokenCount"]
    # Phase 56: Gemini thinking tokens. Mirror the non-streaming fix:
    # add thoughtsTokenCount to completion_tokens (billable output)
    # AND surface as reasoning_tokens for visibility.
    if isinstance(usage.get("thoughtsTokenCount"), int):
        state["reasoning_tokens"] = usage["thoughtsTokenCount"]

    finish_raw = c.get("finishReason")
    finish = _gemini_finish_to_openai(finish_raw)

    # Stash tool-calls until the terminal chunk for OpenAI-shape
    # compatibility (OpenAI emits the full tool_calls list on the
    # finish-reason chunk, not per-fragment).
    if incremental_tool_calls:
        state.setdefault("tool_calls", []).extend(incremental_tool_calls)

    if finish is not None:
        thoughts = state.get("reasoning_tokens") or 0
        candidates_count = state.get("completion_tokens")
        if isinstance(candidates_count, int) and isinstance(thoughts, int) and thoughts > 0:
            billable_completion: int | None = candidates_count + thoughts
        else:
            billable_completion = candidates_count
        return ChatCompletionChunk(
            content_delta=delta_text,
            finish_reason=finish,
            prompt_tokens=state.get("prompt_tokens"),
            completion_tokens=billable_completion,
            tool_calls=state.get("tool_calls") or None,
            reasoning_tokens=int(thoughts) if isinstance(thoughts, int) else 0,
        )
    if not delta_text and not incremental_tool_calls:
        return None
    # Mid-stream chunk with content but no finish_reason yet — emit
    # just the content delta; usage + tool_calls land on the terminal.
    return ChatCompletionChunk(
        content_delta=delta_text,
        finish_reason=None,
    )


def _translate_anthropic_on_vertex_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """Anthropic-on-Vertex streaming events are identical to direct
    Anthropic. Reproduce the same accumulator the direct adapter
    uses (kept inline to avoid cross-adapter import cycles)."""
    etype = event.get("type")
    if etype == "message_start":
        message = event.get("message") or {}
        usage = message.get("usage") or {}
        if isinstance(usage.get("input_tokens"), int):
            state["prompt_tokens"] = usage["input_tokens"]
        # Phase 55: Anthropic prompt-cache usage on Vertex.
        if isinstance(usage.get("cache_creation_input_tokens"), int):
            state["cache_creation_tokens"] = usage["cache_creation_input_tokens"]
        if isinstance(usage.get("cache_read_input_tokens"), int):
            state["cache_read_tokens"] = usage["cache_read_input_tokens"]
        return None
    if etype == "content_block_start":
        block = event.get("content_block") or {}
        btype = block.get("type")
        if btype == "tool_use":
            idx = event.get("index")
            if isinstance(idx, int):
                state.setdefault("tool_calls", {})[idx] = {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": "",
                    },
                }
        elif btype == "thinking":
            # Phase 56: extended-thinking on Anthropic-on-Vertex (same
            # wire shape as direct + Bedrock). Per-index accumulator.
            idx = event.get("index")
            if isinstance(idx, int):
                initial = block.get("thinking") or ""
                state.setdefault("thinking_blocks", {})[idx] = (
                    initial if isinstance(initial, str) else ""
                )
        return None
    if etype == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                return ChatCompletionChunk(
                    content_delta=text,
                    finish_reason=None,
                )
        elif dtype == "input_json_delta":
            idx = event.get("index")
            frag = delta.get("partial_json", "")
            if (
                isinstance(idx, int)
                and isinstance(frag, str)
                and "tool_calls" in state
                and idx in state["tool_calls"]
            ):
                tc = state["tool_calls"][idx]
                tc["function"]["arguments"] += frag
        elif dtype == "thinking_delta":
            # Phase 56: accumulate CoT text. Not emitted as
            # content_delta (consistent with direct + Bedrock).
            idx = event.get("index")
            frag = delta.get("thinking", "")
            if isinstance(idx, int) and isinstance(frag, str) and "thinking_blocks" in state:
                state["thinking_blocks"][idx] = state["thinking_blocks"].get(idx, "") + frag
        return None
    if etype == "message_delta":
        delta = event.get("delta") or {}
        if isinstance(delta.get("stop_reason"), str):
            state["stop_reason"] = delta["stop_reason"]
        usage = event.get("usage") or {}
        if isinstance(usage.get("output_tokens"), int):
            state["completion_tokens"] = usage["output_tokens"]
        return None
    if etype == "message_stop":
        finish_map = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        finish = finish_map.get(state.get("stop_reason") or "", state.get("stop_reason"))
        tool_calls_dict = state.get("tool_calls") or {}
        tool_calls = None
        if tool_calls_dict and isinstance(tool_calls_dict, dict):
            tool_calls = [tool_calls_dict[k] for k in sorted(tool_calls_dict)]
            finish = finish or "tool_calls"
        # Phase 56: assemble accumulated thinking text + estimate.
        thinking_dict = state.get("thinking_blocks") or {}
        reasoning_content: str | None = None
        reasoning_tokens = 0
        if thinking_dict:
            ordered = [thinking_dict[k] for k in sorted(thinking_dict)]
            combined = "\n\n".join(t for t in ordered if t)
            if combined:
                reasoning_content = combined
                reasoning_tokens = (len(combined) + 3) // 4
        return ChatCompletionChunk(
            content_delta="",
            finish_reason=finish,
            prompt_tokens=state.get("prompt_tokens"),
            completion_tokens=state.get("completion_tokens"),
            tool_calls=tool_calls,
            # Phase 55: prompt-cache totals captured at message_start.
            cache_creation_tokens=state.get("cache_creation_tokens"),
            cache_read_tokens=state.get("cache_read_tokens"),
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
        )
    return None


# --------------------------------------------------------------------------- #
# Adapter                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _PublisherDispatch:
    """Per-publisher routing — body builder, response parser, streaming
    translator, all stamped on the publisher name."""

    publisher: str
    build_body: Any
    parse_response: Any
    parse_stream_event: Any


_PUBLISHERS: Final[dict[str, _PublisherDispatch]] = {
    "google": _PublisherDispatch(
        publisher="google",
        build_body=_build_gemini_body,
        parse_response=_parse_gemini_response,
        parse_stream_event=_translate_gemini_stream_event,
    ),
    "anthropic": _PublisherDispatch(
        publisher="anthropic",
        build_body=_build_anthropic_on_vertex_body,
        parse_response=_parse_anthropic_on_vertex_response,
        parse_stream_event=_translate_anthropic_on_vertex_stream_event,
    ),
}


class VertexProvider(Provider):
    """Adapter for GCP Vertex AI's foundation-model API."""

    name = "vertex"

    def __init__(
        self,
        *,
        auth: VertexAuth,
        project_id: str,
        region: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not project_id:
            raise AuthError("vertex: missing project_id")
        if not region:
            raise AuthError("vertex: missing region")
        self._auth = auth
        self._project_id = project_id
        self._region = region
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
        await self._auth.aclose()

    # ---- Provider interface --------------------------------------------------

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        if req.stream:
            return await self._invoke_streaming(req)
        return await self._invoke_non_streaming(req)

    def cost_cents(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        *,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> int:
        """Vertex catalog pricing.

        Phase 55: Anthropic-on-Vertex supports prompt caching (same
        Claude bytes as direct Anthropic + Anthropic-on-Bedrock; same
        usage shape; same weighted pricing — Anthropic charges Vertex
        customers the same cache premium / discount it charges direct).
        For ``anthropic/*`` publisher model IDs we apply the weighted
        math (1.25x write, 0.10x read); for ``google/*`` (Gemini)
        prompt caching is offered but with a different fee structure
        we don't yet model — the cache args fall through to the
        non-cache math for those.

        ``prompt_tokens`` per Anthropic spec already EXCLUDES the
        cached portion; cache args are additive.
        """
        model_with_publisher = _strip_prefix(model)
        pricing = get_pricing("vertex", model_with_publisher)
        if pricing is None:
            return 0
        publisher = model_with_publisher.partition("/")[0]
        if publisher == "anthropic" and (cache_creation_tokens or cache_read_tokens):
            input_cost = prompt_tokens * pricing.input_hcents_per_mtok // 1_000_000
            cache_write_cost = (
                cache_creation_tokens * pricing.input_hcents_per_mtok * 125 // 100_000_000
            )
            cache_read_cost = cache_read_tokens * pricing.input_hcents_per_mtok * 10 // 100_000_000
            output_cost = completion_tokens * pricing.output_hcents_per_mtok // 1_000_000
            return input_cost + cache_write_cost + cache_read_cost + output_cost
        return (
            prompt_tokens * pricing.input_hcents_per_mtok
            + completion_tokens * pricing.output_hcents_per_mtok
        ) // 1_000_000

    # ---- Internals -----------------------------------------------------------

    async def _invoke_non_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        model_with_publisher = _strip_prefix(req.model)
        publisher, model = _split_publisher_model(model_with_publisher)
        dispatch = _PUBLISHERS.get(publisher)
        if dispatch is None:
            raise ProviderError(
                f"vertex: unsupported publisher {publisher!r} (model={model_with_publisher})",
                status=400,
            )
        body = dispatch.build_body(req)
        url = self._endpoint_url(publisher, model, action="generateContent")
        try:
            headers = await self._auth.authorization_header()
        except VertexAuthError as e:
            raise AuthError(f"vertex: {e}") from e
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                url, content=json.dumps(body).encode("utf-8"), headers=headers
            )
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("vertex: upstream timeout") from e

        self._raise_for_status(resp)
        data = resp.json()
        chunk = dispatch.parse_response(data)

        async def _single() -> AsyncIterator[ChatCompletionChunk]:
            yield chunk

        return _single()

    async def _invoke_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        model_with_publisher = _strip_prefix(req.model)
        publisher, model = _split_publisher_model(model_with_publisher)
        dispatch = _PUBLISHERS.get(publisher)
        if dispatch is None:
            raise ProviderError(
                f"vertex: unsupported publisher {publisher!r} for streaming "
                f"(model={model_with_publisher})",
                status=400,
            )
        body = dispatch.build_body(req)
        # Anthropic-on-Vertex sets ``stream: true`` in the body; Gemini
        # uses an URL parameter ``alt=sse``. Handle both branches.
        if publisher == "anthropic":
            body["stream"] = True
            url = self._endpoint_url(publisher, model, action="streamRawPredict")
        else:
            url = self._endpoint_url(publisher, model, action="streamGenerateContent", alt_sse=True)

        try:
            headers = await self._auth.authorization_header()
        except VertexAuthError as e:
            raise AuthError(f"vertex: {e}") from e
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"

        translator = dispatch.parse_stream_event

        async def _gen() -> AsyncIterator[ChatCompletionChunk]:
            state: dict[str, Any] = {}
            try:
                async with self._http.stream(
                    "POST",
                    url,
                    content=json.dumps(body).encode("utf-8"),
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        self._raise_for_status(resp)
                    async for raw_line in resp.aiter_lines():
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:") :].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        chunk = translator(event, state)
                        if chunk is not None:
                            yield chunk
            except httpx.TimeoutException as e:
                raise UpstreamTimeoutError("vertex: upstream timeout during streaming") from e

        return _gen()

    def _endpoint_url(
        self,
        publisher: str,
        model: str,
        *,
        action: str,
        alt_sse: bool = False,
    ) -> str:
        """Build the Vertex URL.

        Standard shape::

            https://{region}-aiplatform.googleapis.com/v1/projects/{project}/
                locations/{region}/publishers/{publisher}/models/{model}:{action}

        ``alt_sse`` adds ``?alt=sse`` for the Gemini streaming endpoint;
        Anthropic-on-Vertex uses ``:streamRawPredict`` without that flag.
        """
        base = (
            f"https://{self._region}-aiplatform.googleapis.com/v1"
            f"/projects/{self._project_id}/locations/{self._region}"
            f"/publishers/{publisher}/models/{model}:{action}"
        )
        if alt_sse:
            base += "?alt=sse"
        return base

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            payload = resp.json()
            # Vertex errors come in {"error": {"code", "message", "status"}}.
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                detail = err.get("message") or resp.text[:200]
            else:
                detail = payload.get("message") if isinstance(payload, dict) else resp.text[:200]
        except (ValueError, KeyError):
            detail = resp.text[:200]
        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"vertex: auth failed ({status}): {detail}")
        if status == 429:
            raise RateLimitError(f"vertex: throttled: {detail}")
        if status >= 500:
            raise ProviderError(
                f"vertex: upstream {status}: {detail}",
                status=502,
                retryable=True,
            )
        raise ProviderError(
            f"vertex: {status}: {detail}",
            status=400,
            retryable=False,
        )
