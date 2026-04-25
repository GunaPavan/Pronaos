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

    def cost_cents(self, prompt_tokens: int, completion_tokens: int, model: str) -> int:
        """Return call cost in hundredths of a cent; ``0`` for unknown models."""
        price = _PRICING.get(_strip_prefix(model))
        if price is None:
            return 0
        input_cost = prompt_tokens * price.input_hcents_per_mtok // 1_000_000
        output_cost = completion_tokens * price.output_hcents_per_mtok // 1_000_000
        return input_cost + output_cost

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
        stop_reason: str | None = None

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
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {}) or {}
                        if delta.get("type") == "text_delta":
                            yield ChatCompletionChunk(
                                content_delta=delta.get("text", ""),
                                finish_reason=None,
                            )
                    elif etype == "message_delta":
                        usage = event.get("usage", {}) or {}
                        completion_tokens = usage.get("output_tokens", 0) or 0
                        stop_reason = (event.get("delta", {}) or {}).get("stop_reason")
                    # Other event types (content_block_start/stop, ping,
                    # message_stop) carry no OpenAI-relevant signal.
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("anthropic: upstream timeout") from e

        # Final sentinel chunk carrying finish reason + usage totals.
        yield ChatCompletionChunk(
            content_delta="",
            finish_reason=_finish_reason(stop_reason) or "stop",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
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
            body["tools"] = req.tools
        if req.extra:
            for k, v in req.extra.items():
                body.setdefault(k, v)
        return body

    @staticmethod
    def _build_response_chunk(data: dict[str, Any]) -> ChatCompletionChunk:
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        usage = data.get("usage", {}) or {}
        return ChatCompletionChunk(
            content_delta="".join(text_blocks),
            finish_reason=_finish_reason(data.get("stop_reason")),
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            raw=data,
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
