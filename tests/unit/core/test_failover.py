"""Failover executor unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pronaos.core.failover import (
    AllProvidersFailedError,
    execute_with_failover,
)
from pronaos.core.router import RoutingPlan
from pronaos.providers.base import (
    AuthError,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
)


class _Scripted(Provider):
    """Provider whose behaviour on chat_completion is scripted in the test."""

    def __init__(self, name: str, *, raises: Exception | None = None, text: str = "ok") -> None:
        self.name = name  # type: ignore[misc]
        self._raises = raises
        self._text = text
        self.call_count = 0

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises

        async def _iter() -> AsyncIterator[ChatCompletionChunk]:
            yield ChatCompletionChunk(
                content_delta=self._text,
                finish_reason="stop",
                prompt_tokens=1,
                completion_tokens=1,
            )

        return _iter()

    def cost_cents(self, p: int, c: int, m: str) -> int:
        return 0


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="anthropic/claude-opus-4-7",
        messages=[{"role": "user", "content": "hi"}],
    )


@pytest.mark.asyncio
async def test_primary_succeeds() -> None:
    primary = _Scripted("primary", text="from-primary")
    fallback = _Scripted("fallback", text="from-fallback")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    provider, stream = await execute_with_failover(plan, _req())
    chunks = [c async for c in stream]

    assert provider.name == "primary"
    assert fallback.call_count == 0
    assert chunks[0].content_delta == "from-primary"


@pytest.mark.asyncio
async def test_retryable_error_falls_over() -> None:
    retryable = ProviderError("upstream boom", status=502, retryable=True)
    primary = _Scripted("primary", raises=retryable)
    fallback = _Scripted("fallback", text="saved")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    provider, stream = await execute_with_failover(plan, _req())
    chunks = [c async for c in stream]

    assert provider.name == "fallback"
    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert chunks[0].content_delta == "saved"


@pytest.mark.asyncio
async def test_non_retryable_error_does_not_fallback() -> None:
    primary = _Scripted("primary", raises=AuthError("bad key"))
    fallback = _Scripted("fallback", text="unused")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    with pytest.raises(AuthError):
        await execute_with_failover(plan, _req())

    assert primary.call_count == 1
    assert fallback.call_count == 0


@pytest.mark.asyncio
async def test_all_providers_fail_raises_terminal() -> None:
    boom = ProviderError("always down", status=502, retryable=True)
    primary = _Scripted("primary", raises=boom)
    fallback = _Scripted("fallback", raises=boom)
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    with pytest.raises(AllProvidersFailedError):
        await execute_with_failover(plan, _req())

    assert primary.call_count == 1
    assert fallback.call_count == 1


@pytest.mark.asyncio
async def test_no_fallbacks_returns_primary_error() -> None:
    boom = ProviderError("down", status=502, retryable=True)
    primary = _Scripted("primary", raises=boom)
    plan = RoutingPlan(primary=primary, fallbacks=())

    with pytest.raises(AllProvidersFailedError):
        await execute_with_failover(plan, _req())
    assert primary.call_count == 1
