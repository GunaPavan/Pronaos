"""Unit tests for AnthropicProvider.

Every HTTP call is mocked at the httpx layer with respx so these tests run
offline, deterministically, and in single-digit milliseconds.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.anthropic import (
    ANTHROPIC_API_URL,
    DEFAULT_MAX_TOKENS,
    AnthropicProvider,
    _finish_reason,
    _split_system,
)
from pronaos.providers.base import (
    AuthError,
    ChatCompletionRequest,
    ProviderError,
    RateLimitError,
    UpstreamTimeoutError,
)

# --------------------------------------------------------------------------- #
# Pure helpers — no network                                                   #
# --------------------------------------------------------------------------- #


class TestSplitSystem:
    def test_no_system(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        system, rest = _split_system(msgs)
        assert system is None
        assert rest == msgs

    def test_single_system(self) -> None:
        msgs = [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
        ]
        system, rest = _split_system(msgs)
        assert system == "be helpful"
        assert rest == [{"role": "user", "content": "hi"}]

    def test_multiple_system_concatenated(self) -> None:
        msgs = [
            {"role": "system", "content": "rule 1"},
            {"role": "system", "content": "rule 2"},
            {"role": "user", "content": "go"},
        ]
        system, rest = _split_system(msgs)
        assert system == "rule 1\n\nrule 2"
        assert rest == [{"role": "user", "content": "go"}]


class TestFinishReasonTranslation:
    @pytest.mark.parametrize(
        ("anthropic_reason", "expected"),
        [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
            (None, None),
        ],
    )
    def test_mapped(self, anthropic_reason: str | None, expected: str | None) -> None:
        assert _finish_reason(anthropic_reason) == expected

    def test_unknown_passes_through(self) -> None:
        assert _finish_reason("weird_new_reason") == "weird_new_reason"


# --------------------------------------------------------------------------- #
# Request translation + response path (mocked)                                #
# --------------------------------------------------------------------------- #


def _mock_body(content: str = "hello back") -> dict:
    return {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key")


class TestChatCompletion:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path_returns_single_chunk(self, provider: AnthropicProvider) -> None:
        route = respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body("hello back"))
        )

        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hello"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]

        assert route.called
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.content_delta == "hello back"
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 10
        assert chunk.completion_tokens == 20
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_model_prefix_is_stripped(self, provider: AnthropicProvider) -> None:
        route = respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body())
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
        )
        await provider.chat_completion(req)

        sent = route.calls[0].request
        body = _json(sent)
        assert body["model"] == "claude-opus-4-7"
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_hoisted_to_top_level(self, provider: AnthropicProvider) -> None:
        route = respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body())
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7",
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
        )
        await provider.chat_completion(req)

        body = _json(route.calls[0].request)
        assert body["system"] == "be terse"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_max_tokens_default_applied(self, provider: AnthropicProvider) -> None:
        route = respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body())
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
        )
        await provider.chat_completion(req)
        assert _json(route.calls[0].request)["max_tokens"] == DEFAULT_MAX_TOKENS
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_max_tokens_passed_through(self, provider: AnthropicProvider) -> None:
        route = respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body())
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=256,
        )
        await provider.chat_completion(req)
        assert _json(route.calls[0].request)["max_tokens"] == 256
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_yields_deltas_then_sentinel(self, provider: AnthropicProvider) -> None:
        sse_body = (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse_body,
            )
        )

        req = ChatCompletionRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]

        # Two content deltas + one sentinel.
        assert len(chunks) == 3
        assert chunks[0].content_delta == "Hello"
        assert chunks[0].finish_reason is None
        assert chunks[1].content_delta == " world"
        sentinel = chunks[-1]
        assert sentinel.content_delta == ""
        assert sentinel.finish_reason == "stop"
        assert sentinel.prompt_tokens == 7
        assert sentinel.completion_tokens == 3
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_ignores_malformed_data_lines(
        self, provider: AnthropicProvider
    ) -> None:
        sse_body = (
            "data: not-json\n\n"
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        )
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(200, text=sse_body))
        req = ChatCompletionRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
        deltas = [c.content_delta for c in chunks if c.content_delta]
        assert deltas == ["ok"]
        await provider.aclose()


# --------------------------------------------------------------------------- #
# Error handling                                                              #
# --------------------------------------------------------------------------- #


class TestErrors:
    @respx.mock
    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, provider: AnthropicProvider) -> None:
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "nope"},
                },
            )
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7", messages=[{"role": "user", "content": "hi"}]
        )
        with pytest.raises(AuthError):
            await provider.chat_completion(req)
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_raises_rate_limit(self, provider: AnthropicProvider) -> None:
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            )
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7", messages=[{"role": "user", "content": "hi"}]
        )
        with pytest.raises(RateLimitError):
            await provider.chat_completion(req)
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_5xx_raises_retryable_provider_error(self, provider: AnthropicProvider) -> None:
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                503, json={"type": "error", "error": {"type": "overloaded", "message": "busy"}}
            )
        )
        req = ChatCompletionRequest(
            model="claude-opus-4-7", messages=[{"role": "user", "content": "hi"}]
        )
        with pytest.raises(ProviderError) as excinfo:
            await provider.chat_completion(req)
        assert excinfo.value.retryable is True
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, provider: AnthropicProvider) -> None:
        respx.post(ANTHROPIC_API_URL).mock(side_effect=httpx.TimeoutException("slow"))
        req = ChatCompletionRequest(
            model="claude-opus-4-7", messages=[{"role": "user", "content": "hi"}]
        )
        with pytest.raises(UpstreamTimeoutError):
            await provider.chat_completion(req)
        await provider.aclose()

    def test_missing_api_key_refused_at_construction(self) -> None:
        with pytest.raises(AuthError):
            AnthropicProvider(api_key="")


# --------------------------------------------------------------------------- #
# Cost accounting                                                             #
# --------------------------------------------------------------------------- #


class TestCostCents:
    def test_opus_known_model(self, provider: AnthropicProvider) -> None:
        # 1M input + 1M output should equal (input_rate + output_rate) hundredths-of-a-cent.
        cost = provider.cost_cents(1_000_000, 1_000_000, "claude-opus-4-7")
        assert cost == 1_500_000 + 7_500_000

    def test_haiku_smaller_cost(self, provider: AnthropicProvider) -> None:
        opus = provider.cost_cents(1_000, 1_000, "claude-opus-4-7")
        haiku = provider.cost_cents(1_000, 1_000, "claude-haiku-4-5")
        assert haiku < opus

    def test_unknown_model_returns_zero(self, provider: AnthropicProvider) -> None:
        assert provider.cost_cents(1000, 1000, "unknown-model") == 0

    def test_prefix_stripped_for_pricing(self, provider: AnthropicProvider) -> None:
        a = provider.cost_cents(1000, 1000, "claude-opus-4-7")
        b = provider.cost_cents(1000, 1000, "anthropic/claude-opus-4-7")
        assert a == b


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _json(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content.decode())
