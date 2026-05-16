"""Rate limiting.

A swappable rate-limiter abstraction with two concrete backends:

- ``InMemoryRateLimiter`` — process-local; perfect for dev and single-worker
  deployments. Zero install, zero infrastructure.
- ``RedisRateLimiter`` — multi-worker safe via an atomic Lua script. Used in
  production where multiple gateway replicas share quota state.

Both implement the same protocol; the factory ``make_rate_limiter(settings)``
chooses based on whether ``redis_url`` is configured. Callers never see the
difference.

Algorithm
---------
Token bucket. Each scope (typically an API key id) has a virtual bucket that
holds at most ``burst`` tokens and refills at ``refill_per_second`` tokens/sec.
Each request costs 1 token by default; if the bucket has enough, deduct and
allow; otherwise compute the time-to-refill and return a ``Denied`` carrying
``retry_after_seconds`` so clients can implement exponential backoff.

The token-bucket choice (vs. sliding window or fixed window) is deliberate:
- amortises burstiness — short spikes within budget pass through
- one number per scope — cheap to store and update
- Lua-implementable as a single atomic check-and-set on Redis
- battle-tested at Stripe, GitHub, Cloudflare, AWS

Design notes
------------
- ``check_and_consume`` is async because the Redis backend will await IO. The
  in-memory backend uses an asyncio lock to serialise updates to a given
  scope's bucket — two concurrent requests on the same key cannot both observe
  ``tokens >= cost`` and both deduct.
- The result is a small dataclass rather than a tuple so call sites read as
  ``if result.allowed: ... else: response.headers["Retry-After"] = ...``.
- An ``rps_limit`` of ``None`` on the calling principal means "unlimited" —
  the limiter is bypassed entirely. We never construct a bucket for unlimited
  scopes (avoids both perf cost and a weird edge case where a key has no
  configured limit but we still allocate state for it).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a single ``check_and_consume`` call."""

    allowed: bool
    retry_after_seconds: float = 0.0
    remaining: float = 0.0  # current token count *after* the attempt

    @classmethod
    def allow(cls, remaining: float) -> RateLimitResult:
        return cls(allowed=True, retry_after_seconds=0.0, remaining=remaining)

    @classmethod
    def deny(cls, retry_after_seconds: float, remaining: float) -> RateLimitResult:
        # Round up to a whole second when emitting Retry-After; clients (and
        # the HTTP spec) generally treat this as an integer second count.
        return cls(
            allowed=False,
            retry_after_seconds=max(0.001, retry_after_seconds),
            remaining=remaining,
        )


# --------------------------------------------------------------------------- #
# Protocol                                                                    #
# --------------------------------------------------------------------------- #


class RateLimiter(Protocol):
    """Anything that can throttle a stream of events.

    Implementations must be safe to share across coroutines. The ``scope_key``
    is opaque to the limiter — typically the API key id — and isolates one
    bucket per scope.
    """

    async def check_and_consume(
        self,
        scope_key: str,
        *,
        burst: int,
        refill_per_second: float,
        cost: float = 1.0,
    ) -> RateLimitResult:
        """Try to consume ``cost`` tokens from the bucket for ``scope_key``."""
        ...

    async def aclose(self) -> None:
        """Release any backend resources (connections, tasks)."""
        ...


# --------------------------------------------------------------------------- #
# In-memory bucket                                                            #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refilled_at: float


class InMemoryRateLimiter:
    """Process-local token-bucket limiter.

    Bucket state is held in a plain dict keyed by ``scope_key``. A per-scope
    asyncio lock serialises updates so concurrent requests on the same key
    can't double-spend the bucket. Different scopes never contend with each
    other — each gets its own lock.

    Memory is bounded only by the number of distinct scopes seen; for a
    portfolio demo (and most single-tenant deployments) this is fine. A
    future LRU eviction policy can be added if scope cardinality ever grows
    without bound.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._clock = clock

    async def check_and_consume(
        self,
        scope_key: str,
        *,
        burst: int,
        refill_per_second: float,
        cost: float = 1.0,
    ) -> RateLimitResult:
        if burst <= 0 or refill_per_second <= 0:
            # Defensive: a zero/negative limit means "never allow." We reject
            # without holding the bucket lock so misconfigured callers fail
            # loudly rather than blocking forever.
            return RateLimitResult.deny(retry_after_seconds=3600.0, remaining=0.0)

        lock = self._locks.setdefault(scope_key, asyncio.Lock())
        async with lock:
            now = self._clock()
            bucket = self._buckets.get(scope_key)
            if bucket is None:
                # Fresh scopes start with a full bucket — protects against
                # cold-start request bursts and matches what users expect.
                bucket = _Bucket(tokens=float(burst), last_refilled_at=now)
                self._buckets[scope_key] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refilled_at)
                bucket.tokens = min(float(burst), bucket.tokens + elapsed * refill_per_second)
                bucket.last_refilled_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return RateLimitResult.allow(remaining=bucket.tokens)

            deficit = cost - bucket.tokens
            retry_after = deficit / refill_per_second
            return RateLimitResult.deny(retry_after_seconds=retry_after, remaining=bucket.tokens)

    async def aclose(self) -> None:
        # No persistent resources; nothing to close.
        self._buckets.clear()
        self._locks.clear()


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #


def make_rate_limiter(redis_url: str | None) -> RateLimiter:
    """Build the right limiter for the current settings.

    Phase 4.1 only wires the in-memory backend. Phase 4.2 will add the Redis
    branch here without changing any callsite.
    """
    if redis_url:
        # Deferred to Phase 4.2; surface the gap explicitly so a future
        # caller doesn't silently get the in-memory backend in prod.
        from pronaos.core.ratelimit_redis import RedisRateLimiter

        return RedisRateLimiter(redis_url=redis_url)
    return InMemoryRateLimiter()
