"""OpenAI prompt-caching support tests (Phase 35).

OpenAI auto-caches prompt prefixes >=1024 tokens on supported models
(gpt-4o, gpt-4o-mini, o1, gpt-4-turbo, etc.) since late 2024. The
response usage block carries ``prompt_tokens_details.cached_tokens``.
Cached tokens are billed at 0.5x the regular input rate (50% discount).

Adapter responsibilities:
1. Extract cached_tokens from non-streaming and streaming responses.
2. Normalize ``prompt_tokens`` on the emitted chunk to the NON-cached
   portion so the chat handler treats every provider uniformly
   (Anthropic already excludes cached from its input_tokens count).
3. Cost math: input + cache_read*0.5 + output, no cache-write premium.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.base import AuthError, ChatCompletionRequest
from pronaos.providers.openai_compat import OpenAICompatibleProvider, Pricing

# A representative OpenAI-compat URL. ``api.openai.com`` is OpenAI itself;
# the same adapter is used for Groq/DeepSeek/Together/etc. but they don't
# expose cached_tokens (which is fine - extraction falls through to 0).
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@pytest.fixture
def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_key="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        pricing={
            "gpt-4o": Pricing(input_hcents_per_mtok=250_000, output_hcents_per_mtok=1_000_000),
            "gpt-4o-mini": Pricing(input_hcents_per_mtok=15_000, output_hcents_per_mtok=60_000),
        },
    )


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


class TestCostMath:
    """OpenAI's published cached-token rate (May 2026):
    - Regular input: 1.0x input_hcents_per_mtok
    - Cache reads: 0.5x - half off
    - Output: unchanged

    For gpt-4o, input is $2.50/Mtok = 250_000 hcents/Mtok.
    For 1M cache-read tokens at 0.5x: 1M x 250_000 x 0.5 / 1M = 125_000 hcents.
    """

    def test_zero_cache_tokens_matches_legacy_cost(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        with_kwargs = provider.cost_cents(
            1_000_000,
            1_000,
            "openai/gpt-4o",
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        without_kwargs = provider.cost_cents(1_000_000, 1_000, "openai/gpt-4o")
        assert with_kwargs == without_kwargs

    def test_cache_read_billed_at_50_percent(self, provider: OpenAICompatibleProvider) -> None:
        # 1M cache-read tokens cost EXACTLY 50% of what 1M regular
        # input tokens cost.
        regular_input = provider.cost_cents(1_000_000, 0, "openai/gpt-4o")
        cache_read_only = provider.cost_cents(
            0,
            0,
            "openai/gpt-4o",
            cache_creation_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cache_read_only == regular_input // 2

    def test_cache_creation_unused_for_openai(self, provider: OpenAICompatibleProvider) -> None:
        # OpenAI doesn't bill cache writes separately; cache_creation_tokens
        # is ignored. Passing it should not change the cost.
        without = provider.cost_cents(
            100,
            10,
            "openai/gpt-4o",
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        with_phantom = provider.cost_cents(
            100,
            10,
            "openai/gpt-4o",
            cache_creation_tokens=999_999,
            cache_read_tokens=0,
        )
        assert without == with_phantom

    def test_cost_components_sum(self, provider: OpenAICompatibleProvider) -> None:
        # 500 non-cached + 1000 cached + 200 output = sum of individual costs.
        c_input = provider.cost_cents(500, 0, "openai/gpt-4o")
        c_read = provider.cost_cents(
            0,
            0,
            "openai/gpt-4o",
            cache_creation_tokens=0,
            cache_read_tokens=1_000,
        )
        c_output = provider.cost_cents(0, 200, "openai/gpt-4o")
        combined = provider.cost_cents(
            500,
            200,
            "openai/gpt-4o",
            cache_creation_tokens=0,
            cache_read_tokens=1_000,
        )
        assert combined == c_input + c_read + c_output

    def test_unknown_model_returns_zero(self, provider: OpenAICompatibleProvider) -> None:
        assert (
            provider.cost_cents(
                1_000,
                100,
                "openai/gpt-future-9000",
                cache_creation_tokens=0,
                cache_read_tokens=500,
            )
            == 0
        )


# --------------------------------------------------------------------------- #
# Non-streaming usage extraction                                              #
# --------------------------------------------------------------------------- #


def _openai_response(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
    cached_tokens: int | None = None,
) -> dict:
    """Build an OpenAI non-streaming response with optional cached_tokens."""
    usage: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


class TestNonStreamingUsage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_cached_tokens_extracted_and_prompt_tokens_normalised(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200,
                json=_openai_response(
                    prompt_tokens=1500,
                    completion_tokens=20,
                    cached_tokens=1024,
                ),
            )
        )
        req = ChatCompletionRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        c = chunks[0]
        # prompt_tokens normalised: 1500 - 1024 = 476.
        assert c.prompt_tokens == 476
        assert c.completion_tokens == 20
        assert c.cache_read_tokens == 1024
        assert c.cache_creation_tokens == 0  # OpenAI doesn't surface cache writes

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_cache_field_returns_zero_and_original_prompt_tokens(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        # Non-OpenAI upstreams (Groq etc.) don't include prompt_tokens_details
        # at all - extraction must fall through to 0 without breaking.
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200,
                json=_openai_response(prompt_tokens=100, completion_tokens=5),
            )
        )
        req = ChatCompletionRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        c = chunks[0]
        assert c.prompt_tokens == 100
        assert c.cache_read_tokens == 0
        assert c.cache_creation_tokens == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_zero_cached_tokens_field_present(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        # OpenAI sometimes returns prompt_tokens_details with cached=0
        # (e.g. when the prompt is below the 1024-token caching threshold).
        # The adapter must report 0 for cache_read_tokens.
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200,
                json=_openai_response(
                    prompt_tokens=200,
                    completion_tokens=10,
                    cached_tokens=0,
                ),
            )
        )
        req = ChatCompletionRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        c = chunks[0]
        assert c.prompt_tokens == 200  # 200 - 0 = 200
        assert c.cache_read_tokens == 0


# --------------------------------------------------------------------------- #
# Streaming usage extraction                                                  #
# --------------------------------------------------------------------------- #


class TestStreamingUsage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_cached_tokens_in_final_usage_chunk(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        # OpenAI's stream_options.include_usage=true emits the usage block
        # in the final chunk. Our adapter parses it and emits a sentinel
        # ChatCompletionChunk at end-of-stream with the totals.
        sse = (
            'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            '"content":"hi"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":1500,"completion_tokens":10,'
            '"total_tokens":1510,"prompt_tokens_details":{"cached_tokens":1024}}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=sse))

        req = ChatCompletionRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "ping"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        # The last chunk carries the usage totals.
        sentinel = chunks[-1]
        # 1500 raw - 1024 cached = 476 non-cached prompt tokens.
        assert sentinel.prompt_tokens == 476
        assert sentinel.completion_tokens == 10
        assert sentinel.cache_read_tokens == 1024
        assert sentinel.cache_creation_tokens == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_no_cache_field_defaults_to_zero(
        self, provider: OpenAICompatibleProvider
    ) -> None:
        sse = (
            'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            '"content":"ok"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":50,"completion_tokens":5,"total_tokens":55}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=sse))
        req = ChatCompletionRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        sentinel = chunks[-1]
        assert sentinel.prompt_tokens == 50
        assert sentinel.cache_read_tokens == 0


# --------------------------------------------------------------------------- #
# Construction                                                                #
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(AuthError):
            OpenAICompatibleProvider(
                provider_key="openai",
                base_url="https://api.openai.com/v1",
                api_key="",
                pricing={},
            )
