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

        Phase 35: OpenAI's auto-prompt-caching (≥1024-token prefixes on
        supported models) gives a 50% discount on cached tokens. Pricing:

        - Regular (non-cached) input: 1.0x input_hcents_per_mtok
        - Cache reads (auto-detected by OpenAI): 0.5x — the FinOps win
        - Output: unchanged

        ``cache_creation_tokens`` is unused for OpenAI — the upstream
        doesn't bill cache writes separately (caching is "free to
        enable" unlike Anthropic's 1.25x write premium). We accept the
        kwarg to satisfy the Provider ABC.

        Other OpenAI-compat providers (Groq, DeepSeek, Together, etc.)
        don't expose a cached_tokens field in their usage blocks, so
        ``cache_read_tokens=0`` is the common path and pricing reduces
        to the legacy input+output sum.
        """
        del cache_creation_tokens  # OpenAI: no cache-write premium
        bare_model = _strip_prefix(model, self._provider_key)
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        # prompt_tokens here is the NON-cached portion (adapter normalises
        # in _chunk_from_response / _chat_streaming). Cache reads are
        # priced at 0.5x via integer math: tokens * rate / 1M * 0.5 =
        # tokens * rate / 2_000_000.
        input_cost = prompt_tokens * price.input_hcents_per_mtok // 1_000_000
        cache_read_cost = cache_read_tokens * price.input_hcents_per_mtok // 2_000_000
        output_cost = completion_tokens * price.output_hcents_per_mtok // 1_000_000
        return input_cost + cache_read_cost + output_cost

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
        except httpx.RequestError as e:
            # Connection-level failures: DNS lookup failed, connection
            # refused, TLS handshake error, broken pipe, etc. None of
            # these are bugs in the gateway — they're upstream-network
            # signals. Wrap as a retryable ProviderError so failover +
            # the circuit breaker can react instead of 500-ing the
            # client with an uncaught httpx exception.
            raise ProviderError(
                f"{self._provider_key}: network error: {e!s}",
                status=502,
                retryable=True,
            ) from e

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
        # Phase 35: OpenAI prompt cache. Auto-cached prefixes (≥1024 tokens)
        # are reported in usage.prompt_tokens_details.cached_tokens. The
        # streaming chunk that carries usage shows up at the end of the
        # stream (when stream_options.include_usage is set; otherwise the
        # field stays 0 and we report no cache savings — same as if the
        # request was below the caching threshold).
        cache_read_tokens = 0
        # Phase 56: reasoning-token surfacing for OpenAI o1/o3 + DeepSeek R1.
        # Reasoning count arrives in the final usage block (same chunk
        # as totals); CoT text from DeepSeek arrives as
        # delta.reasoning_content fragments interleaved with content
        # deltas — accumulate per-stream.
        reasoning_tokens = 0
        reasoning_content_buf: list[str] = []
        finish_reason: str | None = None
        content_buf: list[str] = []
        # OpenAI streaming-tools encode each tool call as a series of
        # deltas indexed by ``tool_calls[].index``. The first delta for
        # an index typically carries ``id`` + ``function.name``; subsequent
        # deltas append fragments to ``function.arguments`` (often
        # character-by-character). We accumulate by index here and emit
        # the assembled tool_calls on the final chunk.
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}

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
                        # Phase 56: DeepSeek R1 streams CoT as
                        # delta.reasoning_content fragments INTERLEAVED
                        # with normal content deltas. Accumulate here;
                        # the terminal chunk carries the assembled text.
                        # We deliberately don't emit reasoning content
                        # as content_delta — clients SSE-decoding the
                        # response expect content_delta to be the user-
                        # visible text only.
                        rc_frag = delta.get("reasoning_content")
                        if isinstance(rc_frag, str) and rc_frag:
                            reasoning_content_buf.append(rc_frag)
                        # Merge any tool_calls fragments into the accumulator.
                        tc_fragments = delta.get("tool_calls") or []
                        for frag in tc_fragments:
                            _merge_tool_call_fragment(accumulated_tool_calls, frag)
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice["finish_reason"]
                    usage = parsed.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or 0
                        completion_tokens = usage.get("completion_tokens", completion_tokens) or 0
                        # Phase 35: extract OpenAI's cached_tokens (when
                        # the upstream auto-cached part of the prompt).
                        details = usage.get("prompt_tokens_details") or {}
                        if isinstance(details, dict):
                            cache_read_tokens = int(details.get("cached_tokens") or 0)
                        # Phase 56: extract reasoning_tokens from
                        # completion_tokens_details. Already INCLUDED in
                        # completion_tokens — no cost-math change.
                        comp_details = usage.get("completion_tokens_details") or {}
                        if isinstance(comp_details, dict):
                            reasoning_tokens = int(comp_details.get("reasoning_tokens") or 0)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"{self._provider_key}: upstream timeout") from e
        except httpx.RequestError as e:
            # Same network-error hardening as the non-streaming path.
            # On a streaming POST this fires before any bytes leave
            # the wire, so wrapping as retryable is safe — failover
            # can still pick the fallback.
            raise ProviderError(
                f"{self._provider_key}: network error: {e!s}",
                status=502,
                retryable=True,
            ) from e

        # Assemble the final tool_calls list (indexed → ordered list).
        assembled_tool_calls: list[dict[str, Any]] | None = None
        if accumulated_tool_calls:
            assembled_tool_calls = [
                accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())
            ]

        # Phase 35: normalize prompt_tokens to the NON-cached portion so
        # the chat handler's math is uniform across providers. OpenAI
        # reports the total prompt_tokens (including cached); Anthropic
        # reports the non-cached portion natively. Subtracting here makes
        # both adapters speak the same chunk shape downstream.
        non_cached_prompt = max(0, prompt_tokens - cache_read_tokens)

        # Phase 56: assemble accumulated reasoning_content (DeepSeek R1).
        reasoning_content = "".join(reasoning_content_buf) if reasoning_content_buf else None

        yield ChatCompletionChunk(
            content_delta="",
            finish_reason=finish_reason or ("tool_calls" if assembled_tool_calls else "stop"),
            prompt_tokens=non_cached_prompt,
            completion_tokens=completion_tokens,
            tool_calls=assembled_tool_calls,
            cache_creation_tokens=0,  # OpenAI doesn't expose a cache-write counter
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
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
            # Pass-through: every OpenAI-compatible upstream (Groq, OpenAI,
            # Together, Fireworks, etc.) accepts the OpenAI tool shape
            # verbatim. Anthropic native has its own adapter that does
            # the translation in ``providers/anthropic.py``.
            body["tools"] = req.tools
        if req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
        if req.response_format is not None:
            # Phase 39 — OpenAI structured outputs. Forward verbatim;
            # providers that don't recognise the field tend to either
            # ignore it gracefully (Groq, Together) or reject the call
            # at validation time. We document the latter as a known
            # incompatibility per provider in the catalog.
            body["response_format"] = req.response_format
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
        # ``tool_calls`` is already in OpenAI shape coming back from every
        # OpenAI-compat provider — no translation needed. We surface it
        # on the chunk so the chat handler can include it in the response.
        tool_calls = message.get("tool_calls")

        # Phase 35: OpenAI prompt cache. Auto-cached prefixes (≥1024
        # tokens on supported models) are reported in
        # usage.prompt_tokens_details.cached_tokens. Non-OpenAI upstreams
        # (Groq, DeepSeek, Together, Fireworks, Perplexity, xAI, Cerebras,
        # Mistral, OpenRouter) leave the nested field absent — extraction
        # falls through to 0 with no behavioural change.
        details = usage.get("prompt_tokens_details")
        cache_read_tokens = 0
        if isinstance(details, dict):
            cache_read_tokens = int(details.get("cached_tokens") or 0)
        # Normalise prompt_tokens to the NON-cached portion so the chat
        # handler treats every provider uniformly. Anthropic already
        # excludes cache fields from its input_tokens; OpenAI includes
        # them — subtract here.
        raw_prompt_tokens = usage.get("prompt_tokens")
        non_cached_prompt = (
            max(0, int(raw_prompt_tokens) - cache_read_tokens)
            if raw_prompt_tokens is not None
            else None
        )

        # Phase 56: reasoning tokens (OpenAI o1/o3, DeepSeek R1, anyone
        # following the OpenAI usage.completion_tokens_details shape).
        # Already INCLUDED in completion_tokens — cost math unchanged.
        # Non-reasoning models leave the nested field absent → 0.
        completion_details = usage.get("completion_tokens_details")
        reasoning_tokens = 0
        if isinstance(completion_details, dict):
            reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
        # DeepSeek R1 also ships the CoT text as message.reasoning_content.
        # OpenAI o-series does NOT expose the CoT text (intentional —
        # only the count). Preserve whichever the upstream sends.
        raw_reasoning_content = message.get("reasoning_content")
        reasoning_content = (
            raw_reasoning_content
            if isinstance(raw_reasoning_content, str) and raw_reasoning_content
            else None
        )

        return ChatCompletionChunk(
            content_delta=message.get("content") or "",
            finish_reason=finish,
            prompt_tokens=non_cached_prompt,
            completion_tokens=usage.get("completion_tokens"),
            tool_calls=tool_calls if tool_calls else None,
            raw=data,
            cache_creation_tokens=0,  # OpenAI doesn't expose a cache-write counter
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
            reasoning_content=reasoning_content,
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


# --------------------------------------------------------------------------- #
# Streaming tool_calls accumulator                                            #
# --------------------------------------------------------------------------- #


def _merge_tool_call_fragment(acc: dict[int, dict[str, Any]], frag: dict[str, Any]) -> None:
    """Merge one ``delta.tool_calls[i]`` fragment into the accumulator.

    OpenAI's streaming protocol for tools sends each tool call as a series
    of deltas keyed by ``index``. The first delta usually carries the
    ``id`` and ``function.name``; subsequent deltas append to
    ``function.arguments`` (often character-by-character, sometimes
    larger chunks). This helper merges idempotently — calling it with
    any prefix of the deltas produces a partial-but-consistent state,
    and the final call assembles the complete OpenAI tool_call shape:
        {"id": ..., "type": "function",
         "function": {"name": ..., "arguments": <json-string>}}
    """
    idx = frag.get("index")
    if idx is None:
        # Defensive: a fragment without an index is malformed in the
        # OpenAI spec but shouldn't crash the stream. Treat as index 0.
        idx = 0

    if idx not in acc:
        acc[idx] = {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        }
    entry = acc[idx]

    if frag.get("id"):
        entry["id"] = frag["id"]
    if frag.get("type"):
        entry["type"] = frag["type"]

    func_frag = frag.get("function") or {}
    if func_frag.get("name"):
        # Function name typically arrives in a single delta; if multiple
        # deltas carry it (unusual), the later one wins.
        entry["function"]["name"] = func_frag["name"]
    if "arguments" in func_frag and func_frag["arguments"] is not None:
        # Arguments are the streaming part — append, don't replace.
        entry["function"]["arguments"] += func_frag["arguments"]
