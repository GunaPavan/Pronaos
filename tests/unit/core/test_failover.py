"""Failover executor unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pronaos.core.circuit import (
    CircuitBreakerRegistry,
    CircuitConfig,
    CircuitState,
)
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


# --------------------------------------------------------------------------- #
# Circuit breaker × failover integration (Phase 15)                            #
# --------------------------------------------------------------------------- #
#
# These tests exercise the wire-up between the failover loop and the breaker
# registry: failures accumulate on a per-provider breaker, OPEN breakers
# cause the primary to be SKIPPED (not even called), and auth errors are
# correctly excluded from health accounting.


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_consecutive_failures() -> None:
    """After K consecutive failures the primary's breaker must be OPEN.
    This is the core SRE story: persistent degradation → breaker
    trips → fallback owns the traffic instead of the dead primary."""
    boom = ProviderError("down", status=502, retryable=True)
    primary = _Scripted("primary", raises=boom)
    fallback = _Scripted("fallback", text="ok")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))
    registry = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=3))

    # Three requests, each one fails the primary and succeeds via fallback.
    for _ in range(3):
        provider, _ = await execute_with_failover(
            plan, _req(), circuit_registry=registry
        )
        assert provider.name == "fallback"

    # Primary's breaker is OPEN; fallback's is CLOSED.
    assert registry.get("primary").state is CircuitState.OPEN
    assert registry.get("fallback").state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_open_breaker_skips_primary_entirely() -> None:
    """Once the primary's breaker is OPEN, the next request must NOT
    even call the primary's chat_completion — it should jump straight
    to the fallback. This is the perf payoff: skip the dead provider's
    HTTP timeout on every subsequent request, not just the first."""
    boom = ProviderError("down", status=502, retryable=True)
    primary = _Scripted("primary", raises=boom)
    fallback = _Scripted("fallback", text="from-fallback")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))
    registry = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=2))

    # Trip the breaker with two failures.
    for _ in range(2):
        await execute_with_failover(plan, _req(), circuit_registry=registry)
    assert registry.get("primary").state is CircuitState.OPEN
    primary_calls_before = primary.call_count  # 2

    # Next request: primary should be skipped entirely.
    provider, stream = await execute_with_failover(
        plan, _req(), circuit_registry=registry
    )
    chunks = [c async for c in stream]

    assert provider.name == "fallback"
    assert chunks[0].content_delta == "from-fallback"
    # Primary's call_count did NOT increase — proof of the skip.
    assert primary.call_count == primary_calls_before


@pytest.mark.asyncio
async def test_auth_error_does_not_trip_breaker() -> None:
    """Auth errors are misconfiguration, not provider health signals.
    Bad keys won't get healthier in 30s, and we don't want the breaker
    to lock out a provider whose key the operator is currently fixing.
    Non-retryable error types must skip the breaker accounting entirely."""
    primary = _Scripted("primary", raises=AuthError("bad key"))
    fallback = _Scripted("fallback", text="ok")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))
    registry = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=1))

    with pytest.raises(AuthError):
        await execute_with_failover(plan, _req(), circuit_registry=registry)

    # Even with threshold=1, an auth error must not trip the breaker.
    assert registry.get("primary").state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_success_resets_failure_streak() -> None:
    """Intermittent failures between successes must not trip the
    breaker. The streak counter resets on every success."""
    boom = ProviderError("transient", status=502, retryable=True)
    # A provider that fails-fails-succeeds-fails-fails. Threshold=3.
    # The streak never hits 3 because the success in the middle resets it.

    class _Flaky(Provider):
        name = "flaky"  # type: ignore[misc]

        def __init__(self) -> None:
            self._script = [True, True, False, True, True]  # True == fail
            self._idx = 0

        async def chat_completion(
            self, req: ChatCompletionRequest
        ) -> AsyncIterator[ChatCompletionChunk]:
            fail = self._script[self._idx]
            self._idx += 1
            if fail:
                raise boom

            async def _iter() -> AsyncIterator[ChatCompletionChunk]:
                yield ChatCompletionChunk(
                    content_delta="ok",
                    finish_reason="stop",
                    prompt_tokens=1,
                    completion_tokens=1,
                )
            return _iter()

        def cost_cents(self, p: int, c: int, m: str) -> int:
            return 0

    flaky = _Flaky()
    fallback = _Scripted("fallback", text="fallback")
    plan = RoutingPlan(primary=flaky, fallbacks=(fallback,))
    registry = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=3))

    for _ in range(5):
        await execute_with_failover(plan, _req(), circuit_registry=registry)

    # Streak was at 2 (after fails 1+2), reset by success at 3, then
    # back to 2 after fails 4+5 — never hit 3.
    assert registry.get("flaky").state is CircuitState.CLOSED
    assert registry.get("flaky").trip_count == 0
