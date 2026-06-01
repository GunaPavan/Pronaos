"""Anthropic prompt-caching support tests (Phase 34).

The adapter must:
1. Extract ``usage.cache_creation_input_tokens`` and
   ``usage.cache_read_input_tokens`` from non-streaming responses.
2. Extract them from streaming ``message_start`` events too.
3. Compute weighted cost: cache writes = 1.25x input rate,
   cache reads = 0.10x input rate, regular input + output unchanged.
4. Default cache_creation_tokens / cache_read_tokens to 0 when the
   client did NOT use cache_control.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.anthropic import (
    ANTHROPIC_API_URL,
    AnthropicProvider,
)
from pronaos.providers.base import ChatCompletionRequest


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-anth-key")


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


class TestCostMath:
    """Anthropic's published pricing structure (May 2026):
    - Cache writes: 1.25x input rate (25% premium for the cache-creation hop)
    - Cache reads: 0.10x input rate (90% discount — the FinOps win)
    - Regular input + output unchanged.

    For Claude Opus 4.7: input is $15/Mtok = 1_500_000 hcents/Mtok.
    For 1,000,000 cache-read tokens: 1M x 1_500_000 x 0.10 / 1M = 150_000 hcents.
    For 1,000,000 cache-write tokens: 1M x 1_500_000 x 1.25 / 1M = 1_875_000 hcents.
    """

    def test_zero_cache_tokens_matches_legacy_cost(self, provider: AnthropicProvider) -> None:
        # Without cache, the new signature produces the same number as before.
        with_cache = provider.cost_cents(
            1_000_000,
            1_000,
            "claude-opus-4-7",
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        # Legacy signature equivalent.
        without_kwargs = provider.cost_cents(1_000_000, 1_000, "claude-opus-4-7")
        assert with_cache == without_kwargs

    def test_cache_read_billed_at_10_percent(self, provider: AnthropicProvider) -> None:
        # 1M cache-read tokens should cost EXACTLY 10% of what 1M regular
        # input tokens cost.
        regular = provider.cost_cents(1_000_000, 0, "claude-opus-4-7")
        cache_read_only = provider.cost_cents(
            0,
            0,
            "claude-opus-4-7",
            cache_creation_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cache_read_only == regular // 10

    def test_cache_write_billed_at_125_percent(self, provider: AnthropicProvider) -> None:
        # 1M cache-creation tokens should cost EXACTLY 125% of what 1M
        # regular input tokens cost.
        regular = provider.cost_cents(1_000_000, 0, "claude-opus-4-7")
        cache_write_only = provider.cost_cents(
            0,
            0,
            "claude-opus-4-7",
            cache_creation_tokens=1_000_000,
            cache_read_tokens=0,
        )
        assert cache_write_only == regular * 125 // 100

    def test_cost_components_sum_correctly(self, provider: AnthropicProvider) -> None:
        # Realistic mixed request: 1k cached prefix read, 500 new input,
        # 200 output. Total cost must equal sum of components.
        c_read = provider.cost_cents(
            0,
            0,
            "claude-opus-4-7",
            cache_creation_tokens=0,
            cache_read_tokens=1_000,
        )
        c_input = provider.cost_cents(500, 0, "claude-opus-4-7")
        c_output = provider.cost_cents(0, 200, "claude-opus-4-7")
        combined = provider.cost_cents(
            500,
            200,
            "claude-opus-4-7",
            cache_creation_tokens=0,
            cache_read_tokens=1_000,
        )
        assert combined == c_read + c_input + c_output

    def test_unknown_model_returns_zero_regardless_of_cache(
        self, provider: AnthropicProvider
    ) -> None:
        # Unknown model means we have no pricing — cache fields can't
        # rescue that.
        assert (
            provider.cost_cents(
                1_000_000,
                1_000,
                "claude-future-9000",
                cache_creation_tokens=10_000,
                cache_read_tokens=50_000,
            )
            == 0
        )


# --------------------------------------------------------------------------- #
# Non-streaming usage extraction                                              #
# --------------------------------------------------------------------------- #


def _anthropic_response_with_cache(
    *,
    input_tokens: int = 5,
    output_tokens: int = 3,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> dict:
    """Build an Anthropic non-streaming response with optional cache fields."""
    usage: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_creation_input_tokens is not None:
        usage["cache_creation_input_tokens"] = cache_creation_input_tokens
    if cache_read_input_tokens is not None:
        usage["cache_read_input_tokens"] = cache_read_input_tokens
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": usage,
    }


class TestNonStreamingUsage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_cache_fields_extracted_when_present(self, provider: AnthropicProvider) -> None:
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=_anthropic_response_with_cache(
                    input_tokens=10,
                    output_tokens=5,
                    cache_creation_input_tokens=200,
                    cache_read_input_tokens=300,
                ),
            )
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        assert len(chunks) == 1
        c = chunks[0]
        assert c.prompt_tokens == 10
        assert c.completion_tokens == 5
        assert c.cache_creation_tokens == 200
        assert c.cache_read_tokens == 300

    @respx.mock
    @pytest.mark.asyncio
    async def test_cache_fields_default_to_zero_when_absent(
        self, provider: AnthropicProvider
    ) -> None:
        # Client didn't use cache_control → Anthropic omits the cache
        # fields entirely. Adapter must default to 0, not crash.
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=_anthropic_response_with_cache(input_tokens=10, output_tokens=5),
            )
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        assert chunks[0].cache_creation_tokens == 0
        assert chunks[0].cache_read_tokens == 0


# --------------------------------------------------------------------------- #
# Streaming usage extraction                                                  #
# --------------------------------------------------------------------------- #


class TestStreamingUsage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_cache_fields_from_message_start(
        self, provider: AnthropicProvider
    ) -> None:
        # message_start carries the input + cache fields.
        # message_delta carries the output_tokens.
        # content_block_delta carries the text.
        sse = (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":12,'
            '"cache_creation_input_tokens":100,"cache_read_input_tokens":250}}}\n\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"hi"}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":3}}\n\n'
        )
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(200, text=sse))

        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "ping"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        # The last (sentinel) chunk carries the cache totals.
        sentinel = chunks[-1]
        assert sentinel.prompt_tokens == 12
        assert sentinel.completion_tokens == 3
        assert sentinel.cache_creation_tokens == 100
        assert sentinel.cache_read_tokens == 250

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_cache_absent_defaults_to_zero(
        self, provider: AnthropicProvider
    ) -> None:
        sse = (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"ok"}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":1}}\n\n'
        )
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(200, text=sse))
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        sentinel = chunks[-1]
        assert sentinel.cache_creation_tokens == 0
        assert sentinel.cache_read_tokens == 0
