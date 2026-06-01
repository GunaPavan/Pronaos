"""Singleflight — concurrent request dedup (Phase 33).

The problem
-----------
A common production pattern: 100 concurrent identical requests arrive
on a cold cache. Each one independently:

1. Checks the cache → MISS
2. Makes the upstream call
3. Writes the cache

If they arrive within the upstream's latency window (the typical case
for bursty workloads — RAG ingestion firing parallel embeddings,
agent loops chaining identical tool calls, retry storms), all 100
make the upstream call. 99 of them are wasted.

The solution
------------
Singleflight collapses concurrent identical work. The first caller
becomes the **leader** — does the upstream call + cache write. Other
callers arriving in the window become **followers** — they register
on the leader's future and wake up when it resolves, sharing the
leader's result with zero additional upstream cost.

Implementation
--------------
- One process-local registry: a dict keyed by an opaque string,
  guarded by an asyncio.Lock for atomic check-and-insert.
- Each entry is an asyncio.Future. The leader sets its result (or
  exception); followers await it.
- The leader removes its own entry on completion so the next arrival
  for the same key starts fresh.

Failure semantics
-----------------
**Followers see the same outcome as the leader, including exceptions.**
This is standard Go singleflight semantics. Rationale: if the leader's
upstream call failed, the cache isn't warm and followers retrying
would just multiply the same failure. The right move is to fail all
N requests together, let the circuit breaker / rate limiter notice,
and let clients retry per their normal policy.

If you genuinely need follower-independent retry on leader failure,
don't use singleflight for that work — but for cache-warming work
(every singleflight use site in Pronaos), shared failure is correct.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from pronaos.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


# Note: PEP 695 `class SingleflightRegistry[T]` syntax produces a
# different runtime type that breaks `SingleflightRegistry[T]()`
# subscription at construction time on Python 3.12. Stay with the
# explicit Generic[T] form — ruff's UP046 is suppressed below.
class SingleflightRegistry(Generic[T]):  # noqa: UP046
    """Per-key future registry that collapses concurrent identical work.

    Generic over the result type so the call sites are type-safe.
    Each endpoint instantiates its own registry (or shares one — the
    type parameter just means "what fn returns").

    A single registry handles arbitrarily many distinct keys
    concurrently; only requests with the *same* key dedupe.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, asyncio.Future[T]] = {}
        self._lock = asyncio.Lock()

    async def share(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Run ``fn()`` exactly once per ``key`` across concurrent callers.

        Returns ``(result, was_follower)``. ``was_follower=True`` means
        this call joined an in-flight leader rather than firing its own
        upstream — useful for metrics so dashboards can quantify the
        dedup rate.

        If the leader's ``fn()`` raises, the exception propagates to
        every follower as well as the leader. Followers do NOT get a
        retry slot — the next arrival AFTER the leader's exception has
        propagated becomes a fresh leader and may succeed.
        """
        async with self._lock:
            existing = self._in_flight.get(key)
            if existing is not None:
                future = existing
                is_leader = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._in_flight[key] = future
                is_leader = True

        if is_leader:
            try:
                result = await fn()
            except BaseException as e:
                # Propagate to any followers waiting on this future.
                future.set_exception(e)
                # Mark the exception as "retrieved" so asyncio doesn't warn
                # when no follower happened to await it. Followers that
                # DO await will still see the exception — set_exception
                # already stored it on the future.
                with contextlib.suppress(
                    asyncio.CancelledError,
                    asyncio.InvalidStateError,
                ):
                    future.exception()
                # Remove the dead entry so the next arrival is fresh.
                async with self._lock:
                    self._in_flight.pop(key, None)
                raise
            future.set_result(result)
            async with self._lock:
                self._in_flight.pop(key, None)
            return result, False
        else:
            result = await future
            return result, True

    def in_flight_count(self) -> int:
        """Diagnostic — current number of in-flight leaders.

        Useful for tests and a future Grafana panel ("how many
        concurrent leaders is the gateway holding?"). Read-only; no
        guarantee that the count is consistent with any particular
        moment in a racy context.
        """
        return len(self._in_flight)
