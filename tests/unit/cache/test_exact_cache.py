"""Unit tests for ``RedisExactCache`` and ``NullCache``.

Uses ``fakeredis`` so the tests are self-contained — no real Redis
required, no port collisions. Coverage:

- Round-trip: ``put`` followed by ``get`` returns the same dict and
  marks the lookup ``tier="exact"``.
- Tenant isolation: tenant A's key cannot be retrieved as tenant B,
  even with identical model + payload.
- Model isolation: same payload under different models is a miss.
- Canonical hashing: key-order differences in the payload still hit.
- TTL: a stored entry has the configured ttl, not the default.
- Decode failure: a corrupted entry is treated as miss AND swept.
- Fail-open: Redis errors return miss / drop the write silently.
- NullCache: always miss, ``put`` is a no-op.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from pronaos.cache.exact import RedisExactCache, _build_key
from pronaos.cache.null import NullCache


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def redis():  # type: ignore[no-untyped-def]
    """Fresh fakeredis client per test — no cross-test bleed-through."""
    return fakeredis.aioredis.FakeRedis()


def _payload(*, text: str = "hello", temp: float = 0.0, max_tokens: int = 64) -> dict:
    return {
        "messages": [{"role": "user", "content": text}],
        "temperature": temp,
        "max_tokens": max_tokens,
    }


def _response(content: str = "world") -> dict:
    return {
        "id": "chatcmpl-x",
        "model": "anthropic/claude-opus-4-7",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


# --------------------------------------------------------------------------- #
# Round-trip                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_put_then_get_round_trip(redis) -> None:  # type: ignore[no-untyped-def]
    """A put followed by a get returns the same response under tier=exact.
    This is the happy path that powers every other cache benefit."""
    cache = RedisExactCache(redis)
    await cache.put(
        tenant_id="t1", model="anthropic/claude-opus-4-7", key_payload=_payload(), response=_response()
    )
    result = await cache.get(
        tenant_id="t1", model="anthropic/claude-opus-4-7", key_payload=_payload()
    )
    assert result.hit is True
    assert result.tier == "exact"
    assert result.response == _response()


# --------------------------------------------------------------------------- #
# Tenant + model isolation                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenant_isolation_no_cross_lookup(redis) -> None:  # type: ignore[no-untyped-def]
    """Tenant A's cached response must NOT be retrievable as tenant B.
    Cross-tenant cache poisoning would be the worst kind of bug — quiet,
    leaks across customers. This is enforced by the key path, not by
    runtime check, so the test reads it back as a *miss* not an error."""
    cache = RedisExactCache(redis)
    await cache.put(
        tenant_id="tenant-a",
        model="anthropic/claude-opus-4-7",
        key_payload=_payload(),
        response=_response("for tenant a only"),
    )

    cross = await cache.get(
        tenant_id="tenant-b", model="anthropic/claude-opus-4-7", key_payload=_payload()
    )
    assert cross.hit is False


@pytest.mark.asyncio
async def test_model_isolation(redis) -> None:  # type: ignore[no-untyped-def]
    """Same payload under different models is a miss. Otherwise asking
    Opus would silently return a Haiku-shaped response, breaking
    downstream code that reads ``model`` from the response."""
    cache = RedisExactCache(redis)
    await cache.put(
        tenant_id="t1",
        model="anthropic/claude-opus-4-7",
        key_payload=_payload(),
        response=_response("opus"),
    )
    other = await cache.get(
        tenant_id="t1", model="anthropic/claude-haiku-4-5", key_payload=_payload()
    )
    assert other.hit is False


# --------------------------------------------------------------------------- #
# Canonical hashing                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_key_order_does_not_affect_hash(redis) -> None:  # type: ignore[no-untyped-def]
    """``messages`` and ``temperature`` etc. in different dict orderings
    must produce the same Redis key — otherwise an identical request
    submitted from different clients (or after a Python interpreter
    restart with different hash seed) would cache-miss against itself."""
    cache = RedisExactCache(redis)
    put_payload = {"temperature": 0.0, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    get_payload = {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}], "temperature": 0.0}

    await cache.put(
        tenant_id="t1", model="m", key_payload=put_payload, response=_response()
    )
    result = await cache.get(tenant_id="t1", model="m", key_payload=get_payload)
    assert result.hit is True


def test_build_key_is_deterministic() -> None:
    """The key builder must be a pure function — the same inputs always
    yield the same key, across runs and processes."""
    k1 = _build_key(tenant_id="t1", model="m", payload={"a": 1, "b": 2})
    k2 = _build_key(tenant_id="t1", model="m", payload={"b": 2, "a": 1})
    assert k1 == k2
    assert k1.startswith("cache:exact:t1:m:")


# --------------------------------------------------------------------------- #
# TTL                                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_put_sets_configured_ttl(redis) -> None:  # type: ignore[no-untyped-def]
    """The TTL passed at construction time must reach Redis. Without
    this guard, a misconfigured shorter TTL would silently halve the
    cache's effectiveness."""
    cache = RedisExactCache(redis, ttl_seconds=42)
    await cache.put(tenant_id="t1", model="m", key_payload=_payload(), response=_response())
    key = _build_key(tenant_id="t1", model="m", payload=_payload())
    ttl = await redis.ttl(key)
    # Allow ± a couple seconds in case fakeredis clock-rounds.
    assert 38 <= ttl <= 42


# --------------------------------------------------------------------------- #
# Resilience                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_corrupted_entry_is_treated_as_miss_and_swept(redis) -> None:  # type: ignore[no-untyped-def]
    """A JSON-corrupt value at the key (e.g. left by an older code path)
    should not crash the gateway. The cache returns miss AND deletes the
    bad entry so a successful future write can replace it."""
    key = _build_key(tenant_id="t1", model="m", payload=_payload())
    await redis.set(key, b"not-valid-json{{{")

    cache = RedisExactCache(redis)
    result = await cache.get(tenant_id="t1", model="m", key_payload=_payload())
    assert result.hit is False
    # The bad key was swept so a later put isn't shadowed by it.
    assert await redis.get(key) is None


@pytest.mark.asyncio
async def test_get_fails_open_on_redis_error() -> None:
    """If Redis raises (network blip, password rotation mid-flight), the
    cache returns miss instead of propagating. The whole point of
    fail-open is that a cache outage stays an availability dent, not a
    correctness break."""
    broken = AsyncMock()
    broken.get.side_effect = ConnectionError("redis is down")
    cache = RedisExactCache(broken)
    result = await cache.get(tenant_id="t1", model="m", key_payload=_payload())
    assert result.hit is False


@pytest.mark.asyncio
async def test_put_fails_open_on_redis_error() -> None:
    """A put that fails to reach Redis is logged but doesn't raise — the
    chat response has already been built and we won't 5xx the client
    over a write-side cache problem."""
    broken = AsyncMock()
    broken.set.side_effect = ConnectionError("redis is down")
    cache = RedisExactCache(broken)
    # Must not raise:
    await cache.put(
        tenant_id="t1", model="m", key_payload=_payload(), response=_response()
    )


# --------------------------------------------------------------------------- #
# NullCache                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_null_cache_always_misses() -> None:
    """The disabled-cache backend must behave as a black hole: every
    get is a miss, every put is a no-op."""
    cache = NullCache()
    res = await cache.get(tenant_id="t1", model="m", key_payload={})
    assert res.hit is False

    # ``put`` returning without raising is the whole contract.
    await cache.put(tenant_id="t1", model="m", key_payload={}, response={"x": 1})

    # And get-after-put is still a miss.
    res2 = await cache.get(tenant_id="t1", model="m", key_payload={})
    assert res2.hit is False
