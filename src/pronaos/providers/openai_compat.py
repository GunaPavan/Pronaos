"""Generic OpenAI-compatible provider.

Most LLM providers today already expose the OpenAI chat-completions API shape
(Groq, DeepSeek, OpenRouter, Together, Fireworks, Perplexity, xAI, Cerebras,
Mistral, OpenAI itself, Azure OpenAI, Ollama, vLLM, LM Studio, and every
"custom OpenAI-compatible endpoint"). One config-driven adapter handles them
all.

Design notes
------------
- **Request shape is pass-through.** Callers already send OpenAI-compat JSON;
  we forward it verbatim after stripping the provider prefix from ``model``.
- **Streaming is a byte-level passthrough** — upstream already emits
  ``chat.completion.chunk`` SSE events, so we don't re-translate them. We do
  parse them just enough to extract usage totals into a sentinel chunk for
  downstream accounting.
- **Pricing is per-adapter-instance**, injected at construction. This keeps
  the catalog ("Groq Llama-3.3 costs $X/Y") separate from the transport.
- **Auth** is typically ``Authorization: Bearer <key>`` but some providers
  (Azure, custom) use different header schemes. ``auth_header_name`` and
  ``auth_header_format`` make the adapter fit any of them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-model pricing in hundredths of a cent per million tokens."""

    input_hcents_per_mtok: int
    output_hcents_per_mtok: int


PricingMap = dict[str, Pricing]


def _strip_prefix(model: str, provider_key: str) -> str:
    prefix = f"{provider_key}/"
    return model.removeprefix(prefix)


async def _parse_sse_passthrough(
    resp: httpx.Response,
) -> AsyncIterator[tuple[bytes, dict[str, Any] | None]]:
    """Yield (raw_line, parsed_json_or_None) for each SSE line.

    The raw line is what we'd pass through to the client byte-for-byte.
    Parsed JSON is used by the gateway internals (cost accounting, metrics)
    without having to re-serialize.
    """
    async for line in resp.aiter_lines():
        raw = (line + "\n").encode()
        if not line.startswith("data:"):
            yield raw, None
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            yield raw, None
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            yield raw, None
            continue
        yield raw, parsed


class OpenAICompatibleProvider(Provider):
    """One adapter, many providers. Config-driven."""

    def __init__(
        self,
        *,
        provider_key: str,
        base_url: str,
        api_key: str,
        pricing: PricingMap,
        default_headers: dict[str, str] | None = None,
        auth_header_name: str = "Authorization",
        auth_header_format: str = "Bearer {key}",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise AuthError(f"{provider_key}: missing api key")
        self._provider_key = provider_key
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._pricing = pricing
        self._default_headers = dict(default_headers or {})
        self._auth_header_name = auth_header_name
        self._auth_header_format = auth_header_format
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._provider_key

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
        bare_model = _strip_prefix(model, self._provider_key)
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        input_cost = prompt_tokens * price.input_hcents_per_mtok // 1_000_000
        output_cost = completion_tokens * price.output_hcents_per_mtok // 1_000_000
        return input_cost + output_cost

    # ---- Non-streaming --------------------------------------------------------

    async def _chat_non_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        body = self._build_body(req, stream=False)
        headers = self._build_headers()

        try:
            resp = await self._http.post(self._url(), json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"{self._provider_key}: upstream timeout") from e

        self._raise_for_status(resp)

        data = resp.json()
        chunk = self._chunk_from_response(data)

        async def _single() -> AsyncIterator[ChatCompletionChunk]:
            yield chunk

        return _single()

    # ---- Streaming ------------------------------------------------------------

    async def _chat_streaming(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        body = self._build_body(req, stream=True)
        headers = self._build_headers()

        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = None
        content_buf: list[str] = []

        try:
            async with self._http.stream("POST", self._url(), json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for_status(resp)

                async for _raw, parsed in _parse_sse_passthrough(resp):
                    if parsed is None:
                        continue
                    choices = parsed.get("choices") or []
                    if choices:
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            content_buf.append(text)
                            yield ChatCompletionChunk(
                                content_delta=text,
                                finish_reason=None,
                            )
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice["finish_reason"]
                    usage = parsed.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or 0
                        completion_tokens = usage.get("completion_tokens", completion_tokens) or 0
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"{self._provider_key}: upstream timeout") from e

        yield ChatCompletionChunk(
            content_delta="",
            finish_reason=finish_reason or "stop",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # ---- Helpers --------------------------------------------------------------

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            **self._default_headers,
            self._auth_header_name: self._auth_header_format.format(key=self._api_key),
        }
        return headers

    def _build_body(self, req: ChatCompletionRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, self._provider_key),
            "messages": list(req.messages),
            "stream": stream,
        }
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.tools is not None:
            body["tools"] = req.tools
        if req.extra:
            for k, v in req.extra.items():
                body.setdefault(k, v)
        return body

    @staticmethod
    def _chunk_from_response(data: dict[str, Any]) -> ChatCompletionChunk:
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        usage = data.get("usage") or {}
        finish = choices[0].get("finish_reason") if choices else None
        return ChatCompletionChunk(
            content_delta=message.get("content") or "",
            finish_reason=finish,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw=data,
        )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                detail = err.get("message") or err.get("type")
            else:
                detail = str(err) if err is not None else resp.text[:200]
        except Exception:
            detail = resp.text[:200]

        status = resp.status_code
        tag = self._provider_key
        if status in (401, 403):
            raise AuthError(f"{tag}: auth failed: {detail}")
        if status == 429:
            raise RateLimitError(f"{tag}: rate limited: {detail}")
        if status >= 500:
            raise ProviderError(
                f"{tag}: upstream {status}: {detail}",
                status=502,
                retryable=True,
            )
        raise ProviderError(
            f"{tag}: {status}: {detail}",
            status=400,
            retryable=False,
        )
