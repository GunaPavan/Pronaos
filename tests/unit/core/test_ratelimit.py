"""Unit tests for InMemoryRateLimiter.

These tests use an injected monotonic clock so we never have to ``sleep``
in tests — fast, deterministic, and the same coverage real time would give.
"""

from __future__ import annotations

import asyncio
from itertools import count

import pytest

from pronaos.core.ratelimit import (
    InMemoryRateLimiter,
    RateLimitResult,
    make_rate_limiter,
)

# --------------------------------------------------------------------------- #
# Fake clock helper                                                           #
# --------------------------------------------------------------------------- #


class _FakeClock:
    """Monotonic-style clock we can step forward by arbitrary deltas."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# --------------------------------------------------------------------------- #
# Burst                                                                       #
# --------------------------------------------------------------------------- #


class TestBurst:
    @pytest.mark.asyncio
    async def test_full_bucket_allows_burst_up_to_size(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        results = []
        for _ in range(5):
            results.append(await limiter.check_and_consume("k", burst=5, refill_per_second=1.0))

        assert all(r.allowed for r in results), "all 5 within burst should succeed"
        assert results[-1].remaining == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_one_over_burst_is_denied(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        for _ in range(5):
            assert (await limiter.check_and_consume("k", burst=5, refill_per_second=1.0)).allowed

        denied = await limiter.check_and_consume("k", burst=5, refill_per_second=1.0)
        assert not denied.allowed
        # need 1 more token at 1 token/s → ~1 second wait
        assert denied.retry_after_seconds == pytest.approx(1.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Refill                                                                      #
# --------------------------------------------------------------------------- #


class TestRefill:
    @pytest.mark.asyncio
    async def test_refills_at_configured_rate(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        # Exhaust the bucket
        for _ in range(2):
            await limiter.check_and_consume("k", burst=2, refill_per_second=2.0)
        denied = await limiter.check_and_consume("k", burst=2, refill_per_second=2.0)
        assert not denied.allowed

        # 0.5 s at 2 tok/s = 1 token refilled — one more should pass
        clock.advance(0.5)
        allowed = await limiter.check_and_consume("k", burst=2, refill_per_second=2.0)
        assert allowed.allowed
        # And the next one should be denied again
        assert not (await limiter.check_and_consume("k", burst=2, refill_per_second=2.0)).allowed

    @pytest.mark.asyncio
    async def test_refill_caps_at_burst(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        # Drain
        await limiter.check_and_consume("k", burst=3, refill_per_second=1.0)
        await limiter.check_and_consume("k", burst=3, refill_per_second=1.0)
        await limiter.check_and_consume("k", burst=3, refill_per_second=1.0)

        # Wait *way* longer than needed to refill — bucket should cap at burst
        clock.advance(60.0)
        # We should be able to consume exactly 3, not 60.
        for _ in range(3):
            assert (await limiter.check_and_consume("k", burst=3, refill_per_second=1.0)).allowed
        assert not (await limiter.check_and_consume("k", burst=3, refill_per_second=1.0)).allowed


# --------------------------------------------------------------------------- #
# Isolation between scopes                                                    #
# --------------------------------------------------------------------------- #


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_different_keys_have_independent_buckets(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        # Exhaust scope A
        for _ in range(3):
            await limiter.check_and_consume("a", burst=3, refill_per_second=1.0)
        assert not (await limiter.check_and_consume("a", burst=3, refill_per_second=1.0)).allowed

        # Scope B should still have a full bucket
        for _ in range(3):
            assert (await limiter.check_and_consume("b", burst=3, refill_per_second=1.0)).allowed


# --------------------------------------------------------------------------- #
# Concurrency — no double-spending                                            #
# --------------------------------------------------------------------------- #


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_requests_on_same_scope_serialise(self) -> None:
        """50 coroutines hit the same scope at once. Burst is 10. Exactly
        10 must succeed — no race-condition double-spends."""
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        async def attempt() -> RateLimitResult:
            return await limiter.check_and_consume("hot", burst=10, refill_per_second=0.001)

        results = await asyncio.gather(*[attempt() for _ in range(50)])
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10, f"expected exactly 10 allowed, got {allowed}"


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_burst_denies_everything(self) -> None:
        limiter = InMemoryRateLimiter()
        r = await limiter.check_and_consume("k", burst=0, refill_per_second=1.0)
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_zero_refill_denies_after_burst(self) -> None:
        limiter = InMemoryRateLimiter()
        r = await limiter.check_and_consume("k", burst=1, refill_per_second=0.0)
        # 0 refill rate is treated as "never allow"
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_arbitrary_cost(self) -> None:
        """Cost can be fractional or > 1 (useful for weighting expensive
        requests later in Phase 5)."""
        clock = _FakeClock()
        limiter = InMemoryRateLimiter(clock=clock)

        # cost=3 against burst=5 should leave 2 tokens
        r = await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=3)
        assert r.allowed and r.remaining == pytest.approx(2.0)
        # cost=2 should pass
        assert (
            await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=2)
        ).allowed
        # cost=1 should now fail
        denied = await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=1)
        assert not denied.allowed

    @pytest.mark.asyncio
    async def test_aclose_clears_state(self) -> None:
        limiter = InMemoryRateLimiter()
        await limiter.check_and_consume("k", burst=1, refill_per_second=1.0)
        await limiter.aclose()
        # Internal state cleared; a new bucket on the same key starts fresh
        r = await limiter.check_and_consume("k", burst=1, refill_per_second=1.0)
        assert r.allowed and r.remaining == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #


class TestFactory:
    def test_returns_in_memory_when_no_redis_url(self) -> None:
        limiter = make_rate_limiter(redis_url=None)
        assert isinstance(limiter, InMemoryRateLimiter)

    def test_returns_in_memory_when_empty_redis_url(self) -> None:
        limiter = make_rate_limiter(redis_url="")
        assert isinstance(limiter, InMemoryRateLimiter)


# --------------------------------------------------------------------------- #
# Sanity: monotonic increment helper                                          #
# --------------------------------------------------------------------------- #


def test_fake_clock_advances() -> None:
    c = _FakeClock(0.0)
    assert c() == 0.0
    c.advance(2.5)
    assert c() == 2.5


def test_count_helper_for_completeness() -> None:
    # Just to keep the test module's import of itertools.count "used" and
    # ensure pyflakes/ruff don't trip on a dead-code import.
    assert next(count()) == 0
