"""Unit tests for OpenAICompatibleProvider.

We test against a fake Groq endpoint. The adapter is provider-agnostic, so
pinning to "groq" here is just sugar — the same wire shape applies to OpenAI,
DeepSeek, OpenRouter, xAI, Cerebras, Together, Fireworks, and every other
OpenAI-compat endpoint.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from pronaos.providers.base import (
    AuthError,
    ChatCompletionRequest,
    ProviderError,
    RateLimitError,
    UpstreamTimeoutError,
)
from pronaos.providers.openai_compat import (
    OpenAICompatibleProvider,
    Pricing,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


@pytest.fixture
def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_key="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="test-groq-key",
        pricing={
            "llama-3.3-70b-versatile": Pricing(
                input_hcents_per_mtok=59_000,
                output_hcents_per_mtok=79_000,
            ),
        },
    )


def _mock_body(content: str = "pong") -> dict:
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "llama-3.3-70b-versatile",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


# --------------------------------------------------------------------------- #
# Non-streaming                                                               #
# --------------------------------------------------------------------------- #


class TestNonStreaming:
    @respx.mock
    @pytest.mark.asyncio
    async def test_happy_path(self, provider: OpenAICompatibleProvider) -> None:
        route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_mock_body("pong")))

        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "ping"}],
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]

        assert route.called
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.content_delta == "pong"
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 5
        assert chunk.completion_tokens == 2
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_prefix_stripped_on_model(self, provider: OpenAICompatibleProvider) -> None:
        route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_mock_body()))
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        await provider.chat_completion(req)

        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "llama-3.3-70b-versatile"
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_header_is_bearer(self, provider: OpenAICompatibleProvider) -> None:
        route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_mock_body()))
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        await provider.chat_completion(req)
        assert route.calls[0].request.headers["authorization"] == "Bearer test-groq-key"
        await provider.aclose()


# --------------------------------------------------------------------------- #
# Custom auth header scheme (e.g. Azure)                                      #
# --------------------------------------------------------------------------- #


class TestCustomAuth:
    @respx.mock
    @pytest.mark.asyncio
    async def test_azure_style_api_key_header(self) -> None:
        provider = OpenAICompatibleProvider(
            provider_key="azure",
            base_url="https://example.openai.azure.com/openai/deployments/gpt-4o",
            api_key="azure-key",
            pricing={},
            auth_header_name="api-key",
            auth_header_format="{key}",
        )
        route = respx.post(
            "https://example.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
        ).mock(return_value=httpx.Response(200, json=_mock_body()))

        req = ChatCompletionRequest(
            model="azure/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        await provider.chat_completion(req)

        headers = route.calls[0].request.headers
        assert headers["api-key"] == "azure-key"
        assert "authorization" not in headers
        await provider.aclose()


# --------------------------------------------------------------------------- #
# Streaming                                                                   #
# --------------------------------------------------------------------------- #


class TestStreaming:
    @respx.mock
    @pytest.mark.asyncio
    async def test_sse_deltas_and_sentinel(self, provider: OpenAICompatibleProvider) -> None:
        sse = (
            'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(GROQ_URL).mock(return_value=httpx.Response(200, text=sse))

        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]

        deltas = [c.content_delta for c in chunks if c.content_delta]
        assert deltas == ["Hel", "lo"]
        sentinel = chunks[-1]
        assert sentinel.finish_reason == "stop"
        assert sentinel.prompt_tokens == 3
        assert sentinel.completion_tokens == 2
        await provider.aclose()


# --------------------------------------------------------------------------- #
# Error handling                                                              #
# --------------------------------------------------------------------------- #


class TestErrors:
    @respx.mock
    @pytest.mark.asyncio
    async def test_401_auth_error(self, provider: OpenAICompatibleProvider) -> None:
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "bad key", "type": "invalid_api_key"}}
            )
        )
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        with pytest.raises(AuthError):
            await provider.chat_completion(req)
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_rate_limit(self, provider: OpenAICompatibleProvider) -> None:
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(
                429, json={"error": {"message": "slow down", "type": "rate_limit_error"}}
            )
        )
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        with pytest.raises(RateLimitError):
            await provider.chat_completion(req)
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_503_retryable(self, provider: OpenAICompatibleProvider) -> None:
        respx.post(GROQ_URL).mock(return_value=httpx.Response(503, text="service unavailable"))
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        with pytest.raises(ProviderError) as ei:
            await provider.chat_completion(req)
        assert ei.value.retryable is True
        await provider.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self, provider: OpenAICompatibleProvider) -> None:
        respx.post(GROQ_URL).mock(side_effect=httpx.TimeoutException("slow"))
        req = ChatCompletionRequest(
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "hi"}],
        )
        with pytest.raises(UpstreamTimeoutError):
            await provider.chat_completion(req)
        await provider.aclose()

    def test_missing_key_refused(self) -> None:
        with pytest.raises(AuthError):
            OpenAICompatibleProvider(
                provider_key="groq",
                base_url="https://x",
                api_key="",
                pricing={},
            )


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


class TestCost:
    def test_known_model(self, provider: OpenAICompatibleProvider) -> None:
        # 1M input + 1M output on configured pricing.
        cost = provider.cost_cents(1_000_000, 1_000_000, "groq/llama-3.3-70b-versatile")
        assert cost == 59_000 + 79_000

    def test_unknown_model_returns_zero(self, provider: OpenAICompatibleProvider) -> None:
        assert provider.cost_cents(1000, 1000, "groq/nonexistent") == 0

    def test_prefix_stripped_for_lookup(self, provider: OpenAICompatibleProvider) -> None:
        bare = provider.cost_cents(1000, 1000, "llama-3.3-70b-versatile")
        prefixed = provider.cost_cents(1000, 1000, "groq/llama-3.3-70b-versatile")
        assert bare == prefixed
