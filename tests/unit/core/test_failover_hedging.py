"""Hedging path through ``execute_with_failover`` (Phase 27).

The hedging feature speculatively starts a second provider after
``hedge_delay_ms`` if the primary hasn't returned. These tests use
controllable async sleeps inside scripted providers so the race is
deterministic — no real timing assertions, just "this provider returns
quickly, this one returns slowly, watch which wins."
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from pronaos.core.circuit import CircuitBreakerRegistry, CircuitConfig
from pronaos.core.failover import (
    HedgeOutcome,
    execute_with_failover,
    hedge_outcome_var,
)
from pronaos.core.router import RoutingPlan
from pronaos.providers.base import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
    ProviderError,
)


class _DelayedProvider(Provider):
    """Returns ``text`` after sleeping ``delay_ms``.

    Tracks ``call_count`` (how often ``chat_completion`` was called) and
    ``finished`` (whether it ran to completion vs was cancelled mid-await).
    The cancelled-mid-await case sets ``finished=False`` so tests can
    confirm the loser was actually torn down before yielding tokens.
    """

    def __init__(
        self,
        name: str,
        *,
        delay_ms: float = 0.0,
        text: str = "ok",
        raises: Exception | None = None,
    ) -> None:
        self.name = name  # type: ignore[misc]
        self._delay_s = delay_ms / 1000.0
        self._text = text
        self._raises = raises
        self.call_count = 0
        self.finished = False

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.call_count += 1
        try:
            await asyncio.sleep(self._delay_s)
        except asyncio.CancelledError:
            # Loser path — cancelled before the simulated upstream
            # finished. Re-raise so the failover layer's cancel logic
            # propagates normally.
            raise
        if self._raises is not None:
            raise self._raises
        self.finished = True

        text = self._text

        async def _iter() -> AsyncIterator[ChatCompletionChunk]:
            yield ChatCompletionChunk(
                content_delta=text,
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


# --------------------------------------------------------------------------- #
# Disabled / degenerate cases — verify hedging is OFF unless asked            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hedging_disabled_when_delay_is_none() -> None:
    """No ``hedge_delay_ms`` means no hedging; behaviour is identical
    to the pre-Phase-27 sequential failover."""
    primary = _DelayedProvider("primary", delay_ms=5, text="from-primary")
    fallback = _DelayedProvider("fallback", delay_ms=5, text="from-fallback")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    provider, _stream = await execute_with_failover(plan, _req(), hedge_delay_ms=None)

    assert provider.name == "primary"
    assert fallback.call_count == 0
    assert hedge_outcome_var.get().triggered is False


@pytest.mark.asyncio
async def test_hedging_disabled_when_max_count_zero() -> None:
    """``hedge_max_count=0`` explicitly disables hedging even if a
    delay is set. Useful for ops to disable hedging without losing the
    delay value."""
    primary = _DelayedProvider("primary", delay_ms=20, text="from-primary")
    fallback = _DelayedProvider("fallback", delay_ms=5, text="from-fallback")
    plan = RoutingPlan(primary=primary, fallbacks=(fallback,))

    provider, _stream = await execute_with_failover(
        plan, _req(), hedge_delay_ms=1.0, hedge_max_count=0
    )

    assert provider.name == "primary"
    assert fallback.call_count == 0


@pytest.mark.asyncio
async def test_hedging_no_op_when_chain_has_one_provider() -> None:
    """Single-provider chain has no candidate to hedge to. The hedge
    timer simply elapses and we keep waiting on the primary."""
    primary = _DelayedProvider("primary", delay_ms=5, text="solo")
    plan = RoutingPlan(primary=primary, fallbacks=())

    provider, _stream = await execute_with_failover(plan, _req(), hedge_delay_ms=1.0)

    assert provider.name == "primary"
    assert hedge_outcome_var.get().triggered is False


# --------------------------------------------------------------------------- #
# Happy paths — primary-wins and hedge-wins races                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hedge_wins_when_primary_is_slow() -> None:
    """Primary takes 100ms; hedge fires after 5ms; hedge returns in
    10ms. Hedge wins; primary is cancelled mid-await."""
    primary = _DelayedProvider("primary", delay_ms=100, text="slow")
    hedge = _DelayedProvider("hedge", delay_ms=10, text="fast")
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))

    provider, stream = await execute_with_failover(plan, _req(), hedge_delay_ms=5.0)
    chunks = [c async for c in stream]

    outcome = hedge_outcome_var.get()
    assert outcome.triggered is True
    assert outcome.winner_role == "hedge"
    assert outcome.winner_provider == "hedge"
    assert outcome.hedge_provider == "hedge"
    assert provider.name == "hedge"
    assert chunks[0].content_delta == "fast"
    # Primary was started but cancelled before its delay elapsed.
    assert primary.call_count == 1
    assert primary.finished is False


@pytest.mark.asyncio
async def test_primary_wins_when_it_is_fast() -> None:
    """Primary returns in 5ms; hedge timer is 20ms — primary returns
    before the hedge ever fires. No hedge triggered."""
    primary = _DelayedProvider("primary", delay_ms=5, text="primary-fast")
    hedge = _DelayedProvider("hedge", delay_ms=10, text="never-called")
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))

    provider, _stream = await execute_with_failover(plan, _req(), hedge_delay_ms=20.0)

    outcome = hedge_outcome_var.get()
    assert outcome.triggered is False
    assert provider.name == "primary"
    assert hedge.call_count == 0


@pytest.mark.asyncio
async def test_primary_wins_after_hedge_fires() -> None:
    """Primary is slow enough to trigger the hedge but still returns
    first. Both calls were started; primary won the race; hedge was
    cancelled mid-flight. ``triggered=True`` so headers reflect that
    hedging happened, but ``winner_role`` is ``"primary"``."""
    # Primary takes 30ms; hedge fires at 10ms; hedge would take 100ms
    # so primary finishes first even though hedge was started.
    primary = _DelayedProvider("primary", delay_ms=30, text="primary-won")
    hedge = _DelayedProvider("hedge", delay_ms=100, text="never-returned")
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))

    provider, _stream = await execute_with_failover(plan, _req(), hedge_delay_ms=10.0)

    outcome = hedge_outcome_var.get()
    assert outcome.triggered is True
    assert outcome.winner_role == "primary"
    assert provider.name == "primary"
    assert hedge.call_count == 1
    assert hedge.finished is False  # cancelled


# --------------------------------------------------------------------------- #
# Failure semantics — hedge rescues a failing primary; both fail              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hedge_rescues_when_primary_errors_after_hedge_fires() -> None:
    """Primary fails after 20ms; hedge fired at 5ms and succeeds at
    10ms. Hedge wins via the rescue path."""
    boom = ProviderError("down", status=502, retryable=True)
    primary = _DelayedProvider("primary", delay_ms=20, raises=boom)
    hedge = _DelayedProvider("hedge", delay_ms=10, text="rescued")
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))

    provider, stream = await execute_with_failover(plan, _req(), hedge_delay_ms=5.0)
    chunks = [c async for c in stream]

    assert provider.name == "hedge"
    assert chunks[0].content_delta == "rescued"
    outcome = hedge_outcome_var.get()
    assert outcome.triggered is True
    assert outcome.winner_role == "hedge"


@pytest.mark.asyncio
async def test_both_hedged_providers_fail_returns_terminal_error() -> None:
    """Both primary and the hedge candidate raise retryable errors.
    No further chain providers to fall through to → AllProvidersFailed."""
    boom = ProviderError("down", status=502, retryable=True)
    primary = _DelayedProvider("primary", delay_ms=20, raises=boom)
    hedge = _DelayedProvider("hedge", delay_ms=10, raises=boom)
    plan = RoutingPlan(primary=primary, fallbacks=(hedge,))

    from pronaos.core.failover import AllProvidersFailedError

    with pytest.raises(AllProvidersFailedError):
        await execute_with_failover(plan, _req(), hedge_delay_ms=5.0)

    assert primary.call_count == 1
    assert hedge.call_count == 1


# --------------------------------------------------------------------------- #
# Circuit breaker integration — hedge respects OPEN breakers                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hedge_skips_open_breaker_candidate() -> None:
    """If the would-be hedge candidate's breaker is OPEN, hedging falls
    back to "just wait for the primary" — no point hedging into a
    known-bad provider. The primary still wins; no hedge triggered."""
    primary = _DelayedProvider("primary", delay_ms=10, text="from-primary")
    # The hedge candidate's breaker is going to be OPEN before the call
    # starts. Use a fresh registry and trip it manually via a series of
    # forced failures, then re-run the race against a healthy primary.
    bad_hedge = _DelayedProvider(
        "bad_hedge",
        delay_ms=1,
        raises=ProviderError("dead", status=502, retryable=True),
    )
    registry = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=1))
    # One call to bad_hedge with itself as primary trips its breaker.
    boom_plan = RoutingPlan(primary=bad_hedge, fallbacks=())
    with pytest.raises(Exception):
        await execute_with_failover(boom_plan, _req(), circuit_registry=registry)
    bad_hedge.call_count = 0  # reset for the next assertion

    real_plan = RoutingPlan(primary=primary, fallbacks=(bad_hedge,))
    provider, _stream = await execute_with_failover(
        real_plan,
        _req(),
        circuit_registry=registry,
        hedge_delay_ms=5.0,
    )

    assert provider.name == "primary"
    # bad_hedge never even got called because its breaker was OPEN.
    assert bad_hedge.call_count == 0


# --------------------------------------------------------------------------- #
# Contextvar hygiene                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hedge_outcome_resets_between_requests() -> None:
    """A leftover outcome from a previous call in the same task must
    NOT leak into the next call. The failover layer resets the
    contextvar at the top of every call."""
    # Plant a fake outcome to simulate leftover state from a prior call.
    hedge_outcome_var.set(HedgeOutcome(triggered=True, winner_role="hedge", hedge_provider="ghost"))

    primary = _DelayedProvider("primary", delay_ms=1, text="ok")
    plan = RoutingPlan(primary=primary, fallbacks=())

    await execute_with_failover(plan, _req(), hedge_delay_ms=None)

    outcome = hedge_outcome_var.get()
    assert outcome.triggered is False
    assert outcome.winner_role is None
    assert outcome.hedge_provider is None
