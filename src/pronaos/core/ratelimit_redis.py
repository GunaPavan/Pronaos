"""Redis-backed token bucket via atomic Lua script.

This is the production backend. Multiple gateway workers share quota state
through a single Redis instance; the Lua script makes check-and-decrement
atomic so two concurrent workers can never both observe ``tokens >= cost``
and both deduct.

Implementation notes
--------------------
- The bucket is stored as a Redis hash with two fields: ``tokens`` (float
  count) and ``ts`` (last refill timestamp in ms). One hash per scope_key.
- The Lua script computes refill from elapsed wall time, attempts the
  consume, and writes back the new state — all in one round-trip, executed
  atomically by Redis itself.
- TTL is set on every write so inactive scopes self-evict from Redis after
  ~2x full-refill time + 60s buffer. Bounds Redis memory without us writing
  a sweeper.
- ``redis-py``'s ``Script`` helper handles ``SCRIPT LOAD`` + ``EVALSHA``
  with a ``NOSCRIPT`` fallback to ``EVAL`` (e.g. after a Redis restart
  flushes the script cache). We don't have to manage the SHA ourselves.
- All Redis interactions use the async client. The library raises
  ``redis.exceptions.ConnectionError`` on network failures; the caller
  (middleware in 4.5) can fail-open or fail-closed per policy.

The fail-open vs. fail-closed decision is **not** made here — this module
only reports what Redis said. Middleware decides what to do on Redis
unavailability (current policy: fail-open on the limiter, fail-closed on
the budget tracker, per ARCHITECTURE.md).
"""

from __future__ import annotations

import time
from typing import Final

import redis.asyncio as aioredis

from pronaos.core.ratelimit import RateLimitResult

# --------------------------------------------------------------------------- #
# Lua script                                                                  #
# --------------------------------------------------------------------------- #
#
# KEYS[1] = bucket key (Redis hash)
# ARGV[1] = burst (max tokens)
# ARGV[2] = refill_per_second (tokens per second, can be fractional)
# ARGV[3] = cost (tokens to consume)
# ARGV[4] = now_ms (caller-provided to avoid Redis<>app clock skew issues
#           if the deployment runs without a shared NTP source)
#
# Returns: {allowed, retry_after_ms, remaining_tokens_microunits}
#   - allowed: 1 or 0
#   - retry_after_ms: 0 when allowed, else ms until enough tokens accrue
#   - remaining_tokens_microunits: tokens * 1e6, integer (Lua/Redis cannot
#     return floats; we encode to micro-units and decode app-side).
#
# Token state is persisted in micro-units too so we don't accumulate FP
# drift across many small refills.

_LUA: Final = """
local key = KEYS[1]
local burst        = tonumber(ARGV[1])
local refill_rate  = tonumber(ARGV[2])
local cost         = tonumber(ARGV[3])
local now_ms       = tonumber(ARGV[4])

if burst <= 0 or refill_rate <= 0 then
    return {0, 3600000, 0}
end

local data = redis.call('HMGET', key, 'tokens_u', 'ts')
local tokens_u = tonumber(data[1])
local last_ms  = tonumber(data[2])

if tokens_u == nil then
    -- First time we've seen this scope: start with a full bucket.
    tokens_u = burst * 1000000
    last_ms  = now_ms
end

-- Refill (work in micro-units to avoid floating-point drift).
local elapsed_ms = math.max(0, now_ms - last_ms)
local refill_u = math.floor(elapsed_ms * refill_rate * 1000)
tokens_u = math.min(burst * 1000000, tokens_u + refill_u)

local cost_u = math.floor(cost * 1000000)
local ttl_s  = math.ceil((burst / refill_rate) * 2) + 60

if tokens_u >= cost_u then
    tokens_u = tokens_u - cost_u
    redis.call('HMSET', key, 'tokens_u', tokens_u, 'ts', now_ms)
    redis.call('EXPIRE', key, ttl_s)
    return {1, 0, tokens_u}
else
    local deficit_u = cost_u - tokens_u
    local retry_ms  = math.ceil(deficit_u / (refill_rate * 1000))
    redis.call('HMSET', key, 'tokens_u', tokens_u, 'ts', now_ms)
    redis.call('EXPIRE', key, ttl_s)
    return {0, retry_ms, tokens_u}
end
"""


# --------------------------------------------------------------------------- #
# Redis limiter                                                               #
# --------------------------------------------------------------------------- #


class RedisRateLimiter:
    """Redis-backed token bucket.

    Construction is cheap (no IO); the first ``check_and_consume`` opens the
    connection pool lazily. This matches FastAPI's lifespan model where we
    want startup to succeed even if Redis is temporarily unreachable.
    """

    KEY_PREFIX: Final = "pronaos:rl:"

    def __init__(
        self,
        *,
        redis_url: str,
        client: aioredis.Redis | None = None,  # type: ignore[type-arg]
    ) -> None:
        self._redis_url = redis_url
        # injection point for tests (fakeredis)
        self._client: aioredis.Redis | None = client  # type: ignore[type-arg]

    async def _ensure_client(self) -> aioredis.Redis:  # type: ignore[type-arg]
        if self._client is None:
            self._client = aioredis.from_url(
                self._redis_url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def check_and_consume(
        self,
        scope_key: str,
        *,
        burst: int,
        refill_per_second: float,
        cost: float = 1.0,
    ) -> RateLimitResult:
        client = await self._ensure_client()

        full_key = f"{self.KEY_PREFIX}{scope_key}"
        now_ms = int(time.time() * 1000)

        # We send the script body on every call (~600 bytes) rather than
        # using EVALSHA. The savings of EVALSHA over EVAL on the wire are
        # negligible (~50 µs per call) and using EVAL keeps the limiter
        # portable across fakeredis/Redis-clones that don't implement the
        # script cache.
        result = await client.eval(  # type: ignore[no-untyped-call]
            _LUA,
            1,  # number of keys
            full_key,
            burst,
            refill_per_second,
            cost,
            now_ms,
        )
        allowed_int, retry_after_ms, remaining_microunits = result
        remaining = float(remaining_microunits) / 1_000_000

        if int(allowed_int) == 1:
            return RateLimitResult.allow(remaining=remaining)
        return RateLimitResult.deny(
            retry_after_seconds=float(retry_after_ms) / 1000.0,
            remaining=remaining,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            # redis-py exposes both ``aclose`` (newer) and ``close`` (older);
            # ``aclose`` is the async one, but type stubs lag.
            await self._client.aclose()  # type: ignore[attr-defined]
            self._client = None
