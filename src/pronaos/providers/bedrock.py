"""AWS Bedrock provider adapter (Phase 42).

Bedrock is AWS's managed-foundation-model API. The wire shape varies
per model family: Anthropic-on-Bedrock uses Anthropic's Messages shape
(minus the ``model`` field, which is in the URL); Llama-on-Bedrock uses
a flat prompt string with role tags; Amazon Nova has its own
``inferenceConfig`` envelope; Mistral-on-Bedrock uses Mistral's
``[INST]`` prompt format.

The discriminator is the model ID prefix (``anthropic.*``, ``meta.*``,
``amazon.*``, ``mistral.*``). Each family has its own per-request
translator + response translator; the common path handles SigV4
signing and HTTP transport.

Auth is SigV4 via :mod:`botocore.auth` — the same code path boto3
uses, so signatures are byte-identical to AWS-SDK-produced requests.
We deliberately do NOT use boto3's high-level clients — we issue the
HTTP via httpx so the gateway's existing async + circuit-breaker +
observability stack still wraps every call.

Phase 52 closes the streaming gap. Bedrock's streaming protocol uses
``application/vnd.amazon.eventstream`` (binary framing with vendored
headers + payload + CRC32 trailers); Pronaos implements a pure-Python
parser in :mod:`pronaos.providers.bedrock_eventstream` and uses it
here to translate each binary frame into one
:class:`ChatCompletionChunk` per family. The streaming code path is
identical to non-streaming in every other dimension — SigV4 signing,
httpx transport, per-family wire-shape translation — so circuit
breakers, hedging, OTel spans, and the rest of the middleware stack
wrap streaming Bedrock identically to non-streaming.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from pronaos.providers.base import (
    AuthError,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
    RateLimitError,
    UpstreamTimeoutError,
)
from pronaos.providers.bedrock_eventstream import (
    EventStreamParseError,
    iter_frames,
)
from pronaos.providers.catalog import get_pricing

BEDROCK_SERVICE: Final = "bedrock"
DEFAULT_MAX_TOKENS: Final = 4096
# Anthropic-on-Bedrock requires this exact string in the request body
# (NOT ``2023-06-01`` like the direct Anthropic API). The mismatch is
# AWS's; we just match what their docs require.
ANTHROPIC_BEDROCK_VERSION: Final = "bedrock-2023-05-31"


def _strip_prefix(model: str) -> str:
    """``bedrock/anthropic.claude-3-5-haiku-...`` -> ``anthropic.claude-3-5-haiku-...``."""
    return model.removeprefix("bedrock/")


def _model_family(model_id: str) -> str:
    """Return the family discriminator (``anthropic``/``meta``/``amazon``/``mistral``).

    Bedrock model IDs are ``vendor.model-version`` shaped — the part
    before the first dot is the vendor namespace. We treat that as the
    family for wire-shape selection.
    """
    return model_id.split(".", 1)[0]


# --------------------------------------------------------------------------- #
# Per-family request translators                                              #
# --------------------------------------------------------------------------- #


def _build_anthropic_body(req: ChatCompletionRequest, model_id: str) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Anthropic-on-Bedrock body.

    Same shape as the direct Anthropic Messages API EXCEPT:
    - No ``model`` field (model is in the URL).
    - Adds ``anthropic_version: "bedrock-2023-05-31"``.

    Reuses the same system-message hoist + tool-result translation as
    the direct Anthropic adapter, but kept inline here so the Bedrock
    adapter is self-contained (no cross-adapter import cycle).
    """
    del model_id  # carried in URL, not body
    systems = [m["content"] for m in req.messages if m.get("role") == "system"]
    others = [m for m in req.messages if m.get("role") != "system"]

    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
        "messages": others,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if systems:
        body["system"] = "\n\n".join(s for s in systems if isinstance(s, str))
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools is not None:
        body["tools"] = [_anthropic_tool(t) for t in req.tools]
    if req.tool_choice is not None:
        body["tool_choice"] = _anthropic_tool_choice(req.tool_choice)
    return body


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI tool def -> Anthropic-on-Bedrock tool def."""
    if "input_schema" in tool and "name" in tool:
        return tool
    func = tool.get("function") or {}
    return {
        "name": func.get("name") or tool.get("name") or "",
        "description": func.get("description") or tool.get("description") or "",
        "input_schema": func.get("parameters") or tool.get("input_schema") or {},
    }


def _anthropic_tool_choice(choice: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(choice, str):
        if choice == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(choice, dict) and choice.get("type") == "function":
        return {"type": "tool", "name": (choice.get("function") or {}).get("name") or ""}
    return choice if isinstance(choice, dict) else {"type": "auto"}


def _build_llama_body(req: ChatCompletionRequest, model_id: str) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Llama-on-Bedrock body.

    Llama-on-Bedrock takes a flat prompt string with the Llama 3 chat
    template tags (``<|begin_of_text|>``, ``<|start_header_id|>...<|end_header_id|>``,
    ``<|eot_id|>``). We assemble it from the OpenAI message list and emit:

        {
          "prompt": "<rendered template>",
          "max_gen_len": N,
          "temperature": ...,
          "top_p": ...
        }

    Tools and vision are NOT supported on Llama-on-Bedrock today (the
    capability matrix in the catalog reflects this).
    """
    del model_id
    rendered = _render_llama3_prompt(req.messages)
    body: dict[str, Any] = {
        "prompt": rendered,
        "max_gen_len": req.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    return body


def _render_llama3_prompt(messages: list[dict[str, Any]]) -> str:
    """Render OpenAI-shape messages into the Llama 3 chat template.

    The template is documented by Meta:
    https://www.llama.com/docs/model-cards-and-prompt-formats/meta-llama-3/

    We render plain text only — Llama-on-Bedrock doesn't accept
    multimodal content blocks, so list-shaped content is flattened by
    concatenating its text parts.
    """
    parts: list[str] = ["<|begin_of_text|>"]
    for m in messages:
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        else:
            text = str(content) if content is not None else ""
        parts.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{text}<|eot_id|>")
    # Open the assistant turn so the model knows it should produce a reply.
    parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
    return "".join(parts)


def _build_nova_body(req: ChatCompletionRequest, model_id: str) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Amazon Nova body.

    Nova's shape (from the AWS docs):

        {
          "messages": [
            {"role": "user", "content": [{"text": "..."}, {"image": {...}}]}
          ],
          "system": [{"text": "..."}],
          "inferenceConfig": {"maxTokens": N, "temperature": ..., "topP": ...}
        }

    Image content uses ``{"image": {"format": "png", "source": {"bytes": "<base64>"}}}``
    (similar to but not identical to Anthropic's shape).
    """
    del model_id
    systems: list[dict[str, Any]] = []
    nova_messages: list[dict[str, Any]] = []
    for m in req.messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            text = (
                content
                if isinstance(content, str)
                else " ".join(
                    p.get("text", "")
                    for p in (content or [])
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            )
            systems.append({"text": text})
            continue
        parts = _content_to_nova_parts(content)
        nova_messages.append({"role": role or "user", "content": parts})

    inference: dict[str, Any] = {"maxTokens": req.max_tokens or DEFAULT_MAX_TOKENS}
    if req.temperature is not None:
        inference["temperature"] = req.temperature

    body: dict[str, Any] = {
        "messages": nova_messages,
        "inferenceConfig": inference,
    }
    if systems:
        body["system"] = systems
    return body


def _content_to_nova_parts(content: Any) -> list[dict[str, Any]]:
    """Convert OpenAI-shape content (str | list[dict]) into Nova content parts."""
    if content is None:
        return [{"text": ""}]
    if isinstance(content, str):
        return [{"text": content}]
    out: list[dict[str, Any]] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "text":
            out.append({"text": p.get("text", "")})
        elif ptype == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # data:image/png;base64,iVBORw0...
                _, _, payload = url.partition(",")
                media_type = url.split(";", 1)[0].removeprefix("data:") or "image/png"
                fmt = media_type.split("/", 1)[1] if "/" in media_type else "png"
                out.append({"image": {"format": fmt, "source": {"bytes": payload}}})
            # HTTPS URL: Nova doesn't accept URL-shaped images via
            # InvokeModel directly. Skip rather than fail loudly —
            # callers using URL images on Nova should be redirected
            # via the documented S3 reference shape (out of scope here).
    return out


def _build_mistral_body(req: ChatCompletionRequest, model_id: str) -> dict[str, Any]:
    """Translate an OpenAI-compat request to Mistral-on-Bedrock body.

    Mistral-on-Bedrock accepts a flat ``prompt`` with Mistral's
    ``[INST]...[/INST]`` instruction format. We render the message list
    into a single Mistral-style prompt.
    """
    del model_id
    prompt = _render_mistral_prompt(req.messages)
    body: dict[str, Any] = {
        "prompt": prompt,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    return body


def _render_mistral_prompt(messages: list[dict[str, Any]]) -> str:
    """Render OpenAI messages into Mistral's [INST]...[/INST] format.

    Conventions per the Mistral docs:
    - Each user turn wraps in ``[INST] ... [/INST]``.
    - System content (if any) is prepended to the first user turn,
      INSIDE the [INST] tags.
    - Assistant turns are emitted as plain text.
    """
    parts: list[str] = []
    pending_system: str | None = None
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        text = (
            content
            if isinstance(content, str)
            else " ".join(
                p.get("text", "")
                for p in (content or [])
                if isinstance(p, dict) and p.get("type") == "text"
            )
        )
        if role == "system":
            pending_system = (pending_system + "\n" + text) if pending_system else text
        elif role == "user":
            if pending_system:
                wrapped = f"[INST] {pending_system}\n\n{text} [/INST]"
            else:
                wrapped = f"[INST] {text} [/INST]"
            parts.append(wrapped)
            pending_system = None
        elif role == "assistant":
            parts.append(f" {text}")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Per-family response translators                                             #
# --------------------------------------------------------------------------- #


def _parse_anthropic_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Anthropic-on-Bedrock response -> ChatCompletionChunk."""
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
    # Phase 56: extended-thinking surface mirrors direct Anthropic.
    # Bedrock's Claude wire shape is identical to direct's, so the same
    # blocks (``type: "thinking"`` with ``.thinking`` text) appear here.
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
    finish = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop_reason or "", stop_reason)
    return ChatCompletionChunk(
        content_delta="".join(text_blocks),
        finish_reason=finish,
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
        tool_calls=tool_calls or None,
        raw=data,
        # Phase 55: Anthropic-on-Bedrock surfaces the SAME prompt-cache
        # usage fields as direct Anthropic (Claude's wire format is
        # identical regardless of hosting). Extract here so the gateway's
        # FinOps headers + weighted cost math apply uniformly to
        # Bedrock-hosted Claude. ``input_tokens`` per Anthropic spec
        # already EXCLUDES the cached portion; the cache fields are
        # additive — total billable input == regular + cache_creation
        # (1.25x) + cache_read (0.10x). Phases 21/22 do the same for
        # direct Anthropic / OpenAI; this closes the gap for the ~50%
        # of Claude usage that flows through AWS.
        cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
        # Phase 56: extended-thinking surface.
        reasoning_tokens=reasoning_tokens,
        reasoning_content=reasoning_content,
    )


def _parse_llama_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Llama-on-Bedrock response -> ChatCompletionChunk.

    Bedrock's Llama response shape:
        {
          "generation": "<text>",
          "prompt_token_count": N,
          "generation_token_count": N,
          "stop_reason": "stop" | "length"
        }
    """
    finish = data.get("stop_reason")
    if finish == "length":
        finish = "length"
    elif finish:
        finish = "stop"
    return ChatCompletionChunk(
        content_delta=data.get("generation", "") or "",
        finish_reason=finish,
        prompt_tokens=data.get("prompt_token_count"),
        completion_tokens=data.get("generation_token_count"),
        raw=data,
    )


def _parse_nova_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Nova response -> ChatCompletionChunk.

    Nova's shape:
        {
          "output": {"message": {"role": "assistant", "content": [{"text": "..."}]}},
          "stopReason": "end_turn" | "max_tokens" | "tool_use",
          "usage": {"inputTokens": N, "outputTokens": N, "totalTokens": N}
        }
    """
    output = data.get("output", {}) or {}
    message = output.get("message", {}) or {}
    parts = message.get("content", []) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
    tool_calls: list[dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        use = p.get("toolUse")
        if not isinstance(use, dict):
            continue
        tool_calls.append(
            {
                "id": use.get("toolUseId", ""),
                "type": "function",
                "function": {
                    "name": use.get("name", ""),
                    "arguments": json.dumps(use.get("input") or {}, separators=(",", ":")),
                },
            }
        )
    usage = data.get("usage", {}) or {}
    stop = data.get("stopReason")
    finish = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "stop_sequence": "stop",
    }.get(stop or "", stop)
    return ChatCompletionChunk(
        content_delta=text,
        finish_reason=finish,
        prompt_tokens=usage.get("inputTokens"),
        completion_tokens=usage.get("outputTokens"),
        tool_calls=tool_calls or None,
        raw=data,
    )


def _parse_mistral_response(data: dict[str, Any]) -> ChatCompletionChunk:
    """Mistral-on-Bedrock response -> ChatCompletionChunk.

    Bedrock's Mistral response shape:
        {
          "outputs": [{"text": "<text>", "stop_reason": "stop" | "length"}]
        }

    Token counts aren't returned by Bedrock for Mistral — we leave
    them as ``None`` and let the gateway's heuristic estimator carry
    the FinOps math.
    """
    outputs = data.get("outputs", []) or []
    if not outputs:
        return ChatCompletionChunk(content_delta="", finish_reason=None, raw=data)
    first = outputs[0]
    finish = first.get("stop_reason")
    if finish == "length":
        finish = "length"
    elif finish:
        finish = "stop"
    return ChatCompletionChunk(
        content_delta=first.get("text", "") or "",
        finish_reason=finish,
        prompt_tokens=None,
        completion_tokens=None,
        raw=data,
    )


# --------------------------------------------------------------------------- #
# Per-family streaming translators                                            #
# --------------------------------------------------------------------------- #
#
# Each translator takes one decoded payload object and a mutable
# ``state`` dict (the function gets to thread per-stream metadata —
# accumulated usage tokens, the active content-block index, etc.) and
# returns one :class:`ChatCompletionChunk` per emitted delta, or
# ``None`` for events that don't translate to a chunk (Anthropic's
# ``message_start``, ``content_block_start``, etc.).
#
# Bedrock wraps every streamed event in a JSON object whose ``bytes``
# field is a base64-encoded UTF-8 JSON string. ``_decode_frame_payload``
# does that unwrapping once before dispatching to the family.


def _decode_frame_payload(payload: bytes) -> dict[str, Any]:
    """Unwrap Bedrock's base64-wrapped JSON payload.

    Bedrock streaming frames carry payload of shape
    ``{"bytes": "<base64-of-utf8-json>"}``. Decode + parse so the
    family translators see a normal dict.
    """
    outer = json.loads(payload.decode("utf-8"))
    inner_b64 = outer.get("bytes")
    if not isinstance(inner_b64, str):
        # Some Bedrock frames (e.g. metadata) carry the payload at the
        # outer level. Treat the outer dict as the event in that case.
        return outer if isinstance(outer, dict) else {}
    inner_bytes = base64.b64decode(inner_b64)
    parsed = json.loads(inner_bytes.decode("utf-8"))
    if isinstance(parsed, dict):
        return parsed
    return {}


def _anthropic_finish_from_stop_reason(stop_reason: str | None) -> str | None:
    if stop_reason is None:
        return None
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }
    return mapping.get(stop_reason, stop_reason)


def _translate_anthropic_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """Anthropic-on-Bedrock streaming event -> ChatCompletionChunk.

    Anthropic emits typed events: ``message_start``,
    ``content_block_start``, ``content_block_delta``,
    ``content_block_stop``, ``message_delta``, ``message_stop``. Only
    delta and the final stop-bearing events produce visible chunks;
    block-start/stop events update state but don't yield content.

    ``state`` keys we use:
    - ``prompt_tokens`` — captured from ``message_start.usage.input_tokens``
    - ``completion_tokens`` — accumulated from ``message_delta.usage.output_tokens``
    - ``stop_reason`` — captured from ``message_delta.delta.stop_reason``
    - ``tool_calls`` — accumulated dict keyed by content-block index
    - ``cache_creation_tokens`` / ``cache_read_tokens`` — captured from
      ``message_start.usage.cache_creation_input_tokens`` /
      ``.cache_read_input_tokens`` (Phase 55)
    """
    etype = event.get("type")

    if etype == "message_start":
        message = event.get("message") or {}
        usage = message.get("usage") or {}
        if isinstance(usage.get("input_tokens"), int):
            state["prompt_tokens"] = usage["input_tokens"]
        # Phase 55: capture Anthropic prompt-cache usage. Same field
        # names + same semantics as direct Anthropic — Bedrock just
        # passes Claude's wire format through unchanged.
        if isinstance(usage.get("cache_creation_input_tokens"), int):
            state["cache_creation_tokens"] = usage["cache_creation_input_tokens"]
        if isinstance(usage.get("cache_read_input_tokens"), int):
            state["cache_read_tokens"] = usage["cache_read_input_tokens"]
        return None

    if etype == "content_block_start":
        # Tool-use blocks start with id + name; we hold them in state
        # so subsequent input_json_delta events can accumulate args.
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
            # Phase 56: extended-thinking block. Initial text may be on
            # the start event or arrive entirely via subsequent
            # thinking_delta events. Per-index accumulator mirrors the
            # direct-Anthropic streaming adapter.
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
            # Accumulate tool-call arguments. We don't emit per-fragment
            # delta chunks here; the assembled tool_calls list goes out
            # on the message_stop frame to keep the OpenAI-compat
            # surface clean (tool_calls is non-incremental on OpenAI's
            # non-streaming shape, and the gateway's SSE accumulator
            # already handles streaming tool_calls upstream).
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
            # Phase 56: accumulate CoT text per content-block index.
            # Don't emit as content_delta — the user-visible content
            # stream stays text-only; CoT lands on the terminal chunk.
            idx = event.get("index")
            frag = delta.get("thinking", "")
            if isinstance(idx, int) and isinstance(frag, str) and "thinking_blocks" in state:
                state["thinking_blocks"][idx] = state["thinking_blocks"].get(idx, "") + frag
        return None

    if etype == "content_block_stop":
        return None

    if etype == "message_delta":
        delta = event.get("delta") or {}
        if isinstance(delta.get("stop_reason"), str):
            state["stop_reason"] = delta["stop_reason"]
        usage = event.get("usage") or {}
        if isinstance(usage.get("output_tokens"), int):
            # Anthropic's streaming output_tokens is the cumulative
            # count, not per-delta. Replace, don't add.
            state["completion_tokens"] = usage["output_tokens"]
        return None

    if etype == "message_stop":
        finish = _anthropic_finish_from_stop_reason(state.get("stop_reason"))
        tool_calls = None
        tc_dict = state.get("tool_calls") or {}
        if tc_dict:
            tool_calls = [tc_dict[k] for k in sorted(tc_dict)]
            # If we emitted tool_calls, the finish_reason should be
            # ``tool_calls`` even when Anthropic reported ``end_turn``.
            finish = finish or "tool_calls"
        # Phase 56: assemble accumulated thinking text + estimate
        # tokens. Anthropic doesn't separate thinking tokens in usage —
        # they're already in output_tokens — so cost math is unchanged.
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
            # None when the request didn't use cache_control, so the
            # chat handler's downstream cost math + headers skip the
            # cache-pricing branch gracefully.
            cache_creation_tokens=state.get("cache_creation_tokens"),
            cache_read_tokens=state.get("cache_read_tokens"),
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
        )

    return None


def _translate_llama_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """Llama-on-Bedrock streaming event -> ChatCompletionChunk.

    Bedrock emits one frame per ``generation`` increment::

        {"generation": "Hello", "prompt_token_count": 5,
         "generation_token_count": 1, "stop_reason": null}

    The final frame carries ``stop_reason: "stop"|"length"`` and the
    final token counts. We emit one chunk per frame.
    """
    text = event.get("generation") or ""
    stop_reason = event.get("stop_reason")
    if stop_reason == "length":
        finish: str | None = "length"
    elif stop_reason:
        finish = "stop"
    else:
        finish = None
    # Track the last-seen counts in state so a final empty-text frame
    # still surfaces the tokens.
    if isinstance(event.get("prompt_token_count"), int):
        state["prompt_tokens"] = event["prompt_token_count"]
    if isinstance(event.get("generation_token_count"), int):
        state["completion_tokens"] = event["generation_token_count"]
    if not text and finish is None:
        # Heartbeat-style frame with neither text nor stop — skip.
        return None
    return ChatCompletionChunk(
        content_delta=text,
        finish_reason=finish,
        prompt_tokens=(state.get("prompt_tokens") if finish is not None else None),
        completion_tokens=(state.get("completion_tokens") if finish is not None else None),
    )


def _translate_nova_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """Nova streaming event -> ChatCompletionChunk.

    Nova's streaming events::

        {"messageStart": {"role": "assistant"}}
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}}
        {"contentBlockStop": {"contentBlockIndex": 0}}
        {"messageStop": {"stopReason": "end_turn"}}
        {"metadata": {"usage": {"inputTokens": N, "outputTokens": N, "totalTokens": N}}}

    The ``metadata`` frame typically arrives AFTER ``messageStop`` —
    we emit the finish chunk on ``messageStop`` with whatever usage we
    have so far, then a second tiny chunk on ``metadata`` if it
    actually carries new usage.
    """
    if "messageStart" in event:
        return None
    if "contentBlockDelta" in event:
        block = event["contentBlockDelta"] or {}
        delta = block.get("delta") or {}
        text = delta.get("text")
        if isinstance(text, str) and text:
            return ChatCompletionChunk(
                content_delta=text,
                finish_reason=None,
            )
        return None
    if "contentBlockStop" in event:
        return None
    if "messageStop" in event:
        stop = (event["messageStop"] or {}).get("stopReason")
        finish_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "stop_sequence": "stop",
        }
        finish = finish_map.get(stop or "", stop)
        state["finish_reason"] = finish
        # Defer the final chunk until ``metadata`` arrives with usage,
        # so the chunk that carries the finish_reason also carries the
        # token counts. Some Nova streams omit ``metadata`` entirely,
        # so we emit the finish chunk here too — the subsequent
        # ``metadata`` frame, if any, will surface usage on its own
        # chunk (gateway accumulator handles the merge).
        return ChatCompletionChunk(
            content_delta="",
            finish_reason=finish,
        )
    if "metadata" in event:
        usage = (event["metadata"] or {}).get("usage") or {}
        return ChatCompletionChunk(
            content_delta="",
            finish_reason=None,
            prompt_tokens=usage.get("inputTokens"),
            completion_tokens=usage.get("outputTokens"),
        )
    return None


def _translate_mistral_stream_event(
    event: dict[str, Any], state: dict[str, Any]
) -> ChatCompletionChunk | None:
    """Mistral-on-Bedrock streaming event -> ChatCompletionChunk.

    Mistral emits::

        {"outputs": [{"text": "Hello", "stop_reason": null}]}

    Per-frame. Final frame's ``stop_reason`` is ``"stop"`` or
    ``"length"``. Token counts are not exposed (matches the
    non-streaming Mistral parser).
    """
    del state  # Mistral stream has no per-stream state to thread
    outputs = event.get("outputs") or []
    if not outputs:
        return None
    first = outputs[0] or {}
    text = first.get("text") or ""
    stop = first.get("stop_reason")
    if stop == "length":
        finish: str | None = "length"
    elif stop:
        finish = "stop"
    else:
        finish = None
    if not text and finish is None:
        return None
    return ChatCompletionChunk(
        content_delta=text,
        finish_reason=finish,
    )


_STREAMING_TRANSLATORS: Final[dict[str, Any]] = {
    "anthropic": _translate_anthropic_stream_event,
    "meta": _translate_llama_stream_event,
    "amazon": _translate_nova_stream_event,
    "mistral": _translate_mistral_stream_event,
}


# --------------------------------------------------------------------------- #
# Adapter                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _SignedHttp:
    """Snapshot of the SigV4-signed request, used by tests for assertions."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes


class BedrockProvider(Provider):
    """Adapter for AWS Bedrock's foundation-model API."""

    name = "bedrock"

    def __init__(
        self,
        *,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        session_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not access_key_id or not secret_access_key:
            raise AuthError("bedrock: missing AWS credentials")
        self._creds = Credentials(
            access_key=access_key_id,
            secret_key=secret_access_key,
            token=session_token,
        )
        self._region = region
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

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
        """Bedrock cost = catalog pricing for the resolved model.

        Phase 55: Anthropic-on-Bedrock supports prompt caching (same
        Claude bytes as direct Anthropic; same usage shape; same
        weighted pricing — Anthropic charges Bedrock customers the
        same cache premium / discount it charges direct-API
        customers). For ``anthropic.*`` model IDs we apply the
        weighted math (1.25x write, 0.10x read); for other Bedrock
        families (Llama / Nova / Mistral) prompt caching is not
        offered today, so the cache args are ignored.

        ``prompt_tokens`` per Anthropic spec already EXCLUDES the
        cached portion — ``input_tokens`` in the usage block is the
        non-cached input. So the math is additive, not subtractive.
        """
        model_id = _strip_prefix(model)
        pricing = get_pricing("bedrock", model_id)
        if pricing is None:
            return 0
        family = _model_family(model_id)
        if family == "anthropic" and (cache_creation_tokens or cache_read_tokens):
            # Weighted math, same shape as Phase 34's direct Anthropic
            # cost_cents. Integer math: multipliers scaled by 100,
            # divisor by 100_000_000 to avoid float drift on big
            # token counts.
            input_cost = prompt_tokens * pricing.input_hcents_per_mtok // 1_000_000
            cache_write_cost = (
                cache_creation_tokens * pricing.input_hcents_per_mtok * 125 // 100_000_000
            )
            cache_read_cost = cache_read_tokens * pricing.input_hcents_per_mtok * 10 // 100_000_000
            output_cost = completion_tokens * pricing.output_hcents_per_mtok // 1_000_000
            return input_cost + cache_write_cost + cache_read_cost + output_cost
        # Non-Anthropic families OR Anthropic with no cache tokens —
        # fall through to the plain math.
        return (
            prompt_tokens * pricing.input_hcents_per_mtok
            + completion_tokens * pricing.output_hcents_per_mtok
        ) // 1_000_000

    # ---- Internals -----------------------------------------------------------

    async def _invoke_non_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        model_id = _strip_prefix(req.model)
        family = _model_family(model_id)
        body = self._build_body_for_family(family, req, model_id)
        url = self._endpoint_url(model_id)

        signed = self._sign("POST", url, json.dumps(body).encode("utf-8"))

        try:
            resp = await self._http.post(
                signed.url,
                content=signed.body,
                headers=signed.headers,
            )
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("bedrock: upstream timeout") from e

        self._raise_for_status(resp)

        data = resp.json()
        chunk = self._parse_for_family(family, data)

        async def _single() -> AsyncIterator[ChatCompletionChunk]:
            yield chunk

        return _single()

    async def _invoke_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        model_id = _strip_prefix(req.model)
        family = _model_family(model_id)
        body = self._build_body_for_family(family, req, model_id)
        url = self._streaming_endpoint_url(model_id)

        signed = self._sign("POST", url, json.dumps(body).encode("utf-8"))
        # Bedrock streaming responses always carry the event-stream
        # content-type. Setting Accept narrows the negotiation and
        # matches what botocore sends.
        signed_headers = dict(signed.headers)
        signed_headers["Accept"] = "application/vnd.amazon.eventstream"

        # ``httpx.AsyncClient.stream(...)`` yields the response as a
        # context manager so the connection releases promptly when the
        # consumer stops iterating (gateway-side stream-cancel still
        # works — see Phase "Stream cancel" tests).
        translator = _STREAMING_TRANSLATORS.get(family)
        if translator is None:
            raise ProviderError(
                f"bedrock: streaming not supported for family {family!r} (model_id={model_id})",
                status=400,
            )

        async def _gen() -> AsyncIterator[ChatCompletionChunk]:
            try:
                async with self._http.stream(
                    "POST",
                    signed.url,
                    content=signed.body,
                    headers=signed_headers,
                ) as resp:
                    if resp.status_code >= 400:
                        # Read the error body once for a useful message,
                        # then raise the right typed error. Streaming
                        # responses with error status are JSON not
                        # event-stream.
                        await resp.aread()
                        self._raise_for_status(resp)
                    state: dict[str, Any] = {}
                    try:
                        async for frame in iter_frames(resp.aiter_bytes()):
                            if frame.is_exception:
                                # Bedrock surfaces upstream model errors
                                # in-band as exception frames. Translate
                                # to ProviderError so the failover layer
                                # can see them.
                                detail = frame.payload[:500].decode("utf-8", errors="replace")
                                raise ProviderError(
                                    f"bedrock: stream exception: {detail}",
                                    status=502,
                                    retryable=True,
                                )
                            payload_obj = _decode_frame_payload(frame.payload)
                            chunk = translator(payload_obj, state)
                            if chunk is not None:
                                yield chunk
                    except EventStreamParseError as e:
                        raise ProviderError(
                            f"bedrock: malformed event-stream frame: {e}",
                            status=502,
                            retryable=False,
                        ) from e
            except httpx.TimeoutException as e:
                raise UpstreamTimeoutError("bedrock: upstream timeout during streaming") from e

        return _gen()

    def _endpoint_url(self, model_id: str) -> str:
        return f"https://bedrock-runtime.{self._region}.amazonaws.com/model/{model_id}/invoke"

    def _streaming_endpoint_url(self, model_id: str) -> str:
        return (
            f"https://bedrock-runtime.{self._region}.amazonaws.com/"
            f"model/{model_id}/invoke-with-response-stream"
        )

    @staticmethod
    def _build_body_for_family(
        family: str, req: ChatCompletionRequest, model_id: str
    ) -> dict[str, Any]:
        if family == "anthropic":
            return _build_anthropic_body(req, model_id)
        if family == "meta":
            return _build_llama_body(req, model_id)
        if family == "amazon":
            return _build_nova_body(req, model_id)
        if family == "mistral":
            return _build_mistral_body(req, model_id)
        raise ProviderError(
            f"bedrock: unsupported model family {family!r} (model_id={model_id})",
            status=400,
        )

    @staticmethod
    def _parse_for_family(family: str, data: dict[str, Any]) -> ChatCompletionChunk:
        if family == "anthropic":
            return _parse_anthropic_response(data)
        if family == "meta":
            return _parse_llama_response(data)
        if family == "amazon":
            return _parse_nova_response(data)
        if family == "mistral":
            return _parse_mistral_response(data)
        return ChatCompletionChunk(content_delta="", finish_reason="stop", raw=data)

    def _sign(self, method: str, url: str, body: bytes) -> _SignedHttp:
        """Produce the SigV4-signed request for httpx.

        botocore's ``SigV4Auth.add_auth`` mutates an ``AWSRequest`` in
        place; we then pull the headers off and re-issue via httpx. The
        important property is that the resulting Authorization header
        is byte-for-byte what boto3 would produce — see the unit tests
        for the explicit assertion against the AWS-published test vector.
        """
        aws_req = AWSRequest(
            method=method,
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        signer = SigV4Auth(self._creds, BEDROCK_SERVICE, self._region)
        signer.add_auth(aws_req)
        # Pull all headers out — including Authorization and any X-Amz-*.
        headers = dict(aws_req.headers.items())
        return _SignedHttp(method=method, url=url, headers=headers, body=body)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            payload = resp.json()
            detail = (
                payload.get("message")
                or payload.get("Message")
                or payload.get("error")
                or resp.text[:200]
            )
        except Exception:
            detail = resp.text[:200]
        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"bedrock: auth failed ({status}): {detail}")
        if status == 429:
            raise RateLimitError(f"bedrock: throttled: {detail}")
        if status >= 500:
            raise ProviderError(
                f"bedrock: upstream {status}: {detail}",
                status=502,
                retryable=True,
            )
        raise ProviderError(
            f"bedrock: {status}: {detail}",
            status=400,
            retryable=False,
        )
