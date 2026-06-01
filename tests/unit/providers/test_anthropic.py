"""Unit tests for AnthropicProvider.

Every HTTP call is mocked at the httpx layer with respx so these tests run
offline, deterministically, and in single-digit milliseconds.
"""

from __future__ import annotations

import json

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


class TestExtendedThinking:
    """Phase 56: Anthropic extended-thinking surface.

    Anthropic emits thinking-mode CoT as a separate ``type: "thinking"``
    content block before any ``text`` block. The thinking tokens are
    ALREADY counted in ``usage.output_tokens`` — Pronaos surfaces the
    block text on ``reasoning_content`` and estimates the token count
    (~4 chars/token) on ``reasoning_tokens``. Non-thinking responses
    leave both fields at 0/None (no behavioural change).
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_thinking_block_surfaces_on_non_streaming(
        self, provider: AnthropicProvider
    ) -> None:
        body = {
            "id": "msg_thinking_01",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": (
                        "Let me think about this carefully. The user is asking "
                        "for a sum. 2+2 is a basic arithmetic question."
                    ),
                    "signature": "sig_opaque",
                },
                {"type": "text", "text": "The answer is 4."},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 50},
        }
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=body)
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "what's 2+2?"}],
        )
        chunks = [c async for c in await provider.chat_completion(req)]
        assert len(chunks) == 1
        chunk = chunks[0]
        # content_delta carries the text block only (NOT thinking).
        assert chunk.content_delta == "The answer is 4."
        # reasoning_content carries the thinking text.
        assert chunk.reasoning_content is not None
        assert "2+2 is a basic arithmetic question" in chunk.reasoning_content
        # reasoning_tokens estimated from char-length (ceil-divide by 4).
        # The thinking text is 100 chars → ceil(100/4) = 25 tokens.
        assert chunk.reasoning_tokens == 25
        # completion_tokens unchanged — Anthropic already includes
        # thinking in output_tokens.
        assert chunk.completion_tokens == 50
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_thinking_response_has_no_reasoning(
        self, provider: AnthropicProvider
    ) -> None:
        """Regression: a plain text-only response (the common case)
        must not invent reasoning_tokens or reasoning_content."""
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(200, json=_mock_body("just text"))
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
        )
        chunks = [c async for c in await provider.chat_completion(req)]
        chunk = chunks[0]
        assert chunk.reasoning_tokens == 0
        assert chunk.reasoning_content is None
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_streaming_thinking_block_assembled_on_terminal(
        self, provider: AnthropicProvider
    ) -> None:
        """Anthropic streams thinking via ``content_block_start`` (type=
        thinking) + a series of ``content_block_delta`` events with
        ``delta.type=thinking_delta``. The adapter accumulates the
        fragments and emits them on the terminal chunk. Text deltas
        flow normally as content_delta — thinking is body-only."""
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream_thinking",
                    "usage": {"input_tokens": 8, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "First, "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "I consider X."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Final answer."},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 45},
            },
            {"type": "message_stop"},
        ]
        sse_body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        respx.post(ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )
        req = ChatCompletionRequest(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        chunks = [c async for c in await provider.chat_completion(req)]
        # Text-delta chunks flow normally; thinking is body-only.
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["Final answer."]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        # Thinking content assembled from the two thinking_delta events.
        assert terminal.reasoning_content == "First, I consider X."
        # Char-length / 4 ceil: 20 chars → 5 tokens.
        assert terminal.reasoning_tokens == 5
        await provider.aclose()


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
