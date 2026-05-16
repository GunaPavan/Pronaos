"""Unit tests for RedisRateLimiter using fakeredis.

The Lua-script logic is the part that's most likely to break (arithmetic,
units, edge cases), so these tests exercise the same scenarios as the
in-memory tests plus Redis-specific ones (TTL, key isolation, script reload).

We use ``fakeredis.aioredis.FakeRedis`` injected as the ``client`` arg —
no real Redis required, tests stay deterministic and fast.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis.aioredis as fakeredis_async
import pytest
import pytest_asyncio

from pronaos.core.ratelimit import RateLimitResult, make_rate_limiter
from pronaos.core.ratelimit_redis import RedisRateLimiter


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[fakeredis_async.FakeRedis]:
    """Fresh fakeredis instance per test — no state leakage."""
    server = fakeredis_async.FakeServer()
    client = fakeredis_async.FakeRedis(server=server, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def limiter(fake_redis: fakeredis_async.FakeRedis) -> AsyncIterator[RedisRateLimiter]:
    lim = RedisRateLimiter(redis_url="redis://unused", client=fake_redis)
    try:
        yield lim
    finally:
        # Don't aclose the underlying client (fake_redis fixture owns it),
        # but reset the limiter's lazy state.
        lim._client = None


# --------------------------------------------------------------------------- #
# Burst                                                                       #
# --------------------------------------------------------------------------- #


class TestBurst:
    @pytest.mark.asyncio
    async def test_full_bucket_allows_burst_up_to_size(self, limiter: RedisRateLimiter) -> None:
        results = []
        for _ in range(5):
            results.append(await limiter.check_and_consume("k", burst=5, refill_per_second=1.0))
        assert all(r.allowed for r in results)
        # Tolerance accounts for the ~ms of wall-time elapsed between
        # Redis round-trips refilling fractional tokens — correct behaviour,
        # so we just assert "less than one full token remaining."
        assert 0.0 <= results[-1].remaining < 1.0

    @pytest.mark.asyncio
    async def test_one_over_burst_is_denied(self, limiter: RedisRateLimiter) -> None:
        for _ in range(5):
            await limiter.check_and_consume("k", burst=5, refill_per_second=1.0)

        denied = await limiter.check_and_consume("k", burst=5, refill_per_second=1.0)
        assert not denied.allowed
        # Need ~1 more token at 1/s.  Lua math.ceil + ms precision → about 1s.
        assert 0.9 <= denied.retry_after_seconds <= 1.1


# --------------------------------------------------------------------------- #
# Refill                                                                      #
# --------------------------------------------------------------------------- #


class TestRefill:
    @pytest.mark.asyncio
    async def test_refills_with_real_time(self, limiter: RedisRateLimiter) -> None:
        """Unlike the in-memory test (which uses a fake clock), the Redis
        backend uses wall time inside Lua. We use a generous refill rate
        and a short sleep to keep the test fast but deterministic."""
        # Drain a 2-token bucket
        for _ in range(2):
            await limiter.check_and_consume("k", burst=2, refill_per_second=20.0)
        assert not (await limiter.check_and_consume("k", burst=2, refill_per_second=20.0)).allowed

        # Wait ~0.15s — at 20 tok/s that's 3 tokens but bucket caps at 2
        await asyncio.sleep(0.15)
        for _ in range(2):
            assert (await limiter.check_and_consume("k", burst=2, refill_per_second=20.0)).allowed
        # Third immediately after should fail (bucket empty)
        assert not (await limiter.check_and_consume("k", burst=2, refill_per_second=20.0)).allowed


# --------------------------------------------------------------------------- #
# Scope isolation                                                             #
# --------------------------------------------------------------------------- #


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_different_scopes_have_independent_buckets(
        self, limiter: RedisRateLimiter
    ) -> None:
        for _ in range(3):
            await limiter.check_and_consume("a", burst=3, refill_per_second=1.0)
        assert not (await limiter.check_and_consume("a", burst=3, refill_per_second=1.0)).allowed

        # Scope B still full
        for _ in range(3):
            assert (await limiter.check_and_consume("b", burst=3, refill_per_second=1.0)).allowed

    @pytest.mark.asyncio
    async def test_scope_keys_namespaced_in_redis(
        self,
        limiter: RedisRateLimiter,
        fake_redis: fakeredis_async.FakeRedis,
    ) -> None:
        await limiter.check_and_consume("hello", burst=5, refill_per_second=1.0)
        keys = await fake_redis.keys("*")
        # All keys must use the pronaos:rl: prefix so we never collide with
        # other consumers of the same Redis instance.
        assert any(k.startswith("pronaos:rl:") for k in keys)
        assert "pronaos:rl:hello" in keys


# --------------------------------------------------------------------------- #
# Atomicity (no double-spend under concurrent contention)                     #
# --------------------------------------------------------------------------- #


class TestAtomicity:
    @pytest.mark.asyncio
    async def test_concurrent_consume_does_not_double_spend(
        self, limiter: RedisRateLimiter
    ) -> None:
        # 30 concurrent attempts on a 7-token bucket. Refill is set near-zero
        # so any allows beyond 7 would prove a race.
        async def attempt() -> RateLimitResult:
            return await limiter.check_and_consume("hot", burst=7, refill_per_second=0.001)

        results = await asyncio.gather(*[attempt() for _ in range(30)])
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 7, f"expected 7 allowed (atomic), got {allowed}"


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_burst_denies_everything(self, limiter: RedisRateLimiter) -> None:
        r = await limiter.check_and_consume("k", burst=0, refill_per_second=1.0)
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_arbitrary_cost(self, limiter: RedisRateLimiter) -> None:
        # cost=3 against burst=5 leaves 2
        r = await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=3)
        assert r.allowed and r.remaining == pytest.approx(2.0, abs=0.001)
        # Next cost=2 should pass
        assert (
            await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=2)
        ).allowed
        # Then cost=1 should fail
        assert not (
            await limiter.check_and_consume("k", burst=5, refill_per_second=1.0, cost=1)
        ).allowed

    @pytest.mark.asyncio
    async def test_ttl_is_set(
        self,
        limiter: RedisRateLimiter,
        fake_redis: fakeredis_async.FakeRedis,
    ) -> None:
        """Inactive keys must expire so Redis memory doesn't grow unbounded."""
        await limiter.check_and_consume("ephemeral", burst=10, refill_per_second=1.0)
        ttl = await fake_redis.ttl("pronaos:rl:ephemeral")
        # TTL should be a positive number of seconds (no -1 == no expire)
        assert ttl > 0, f"expected positive TTL, got {ttl}"


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #


class TestFactory:
    def test_returns_redis_when_url_set(self) -> None:
        limiter = make_rate_limiter(redis_url="redis://localhost:6379/0")
        assert isinstance(limiter, RedisRateLimiter)
