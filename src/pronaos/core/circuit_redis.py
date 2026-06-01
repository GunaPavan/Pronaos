"""Redis-backed circuit breaker for multi-replica deployments (Phase 25).

Why this exists
---------------
The in-memory ``CircuitBreaker`` in ``circuit.py`` is correct and fast
for a single-process gateway. At multi-replica scale (production
deploys behind a load balancer) it has one weakness: every replica
runs its own breaker, so a broken provider needs ``failure_threshold``
hits **per replica** before convergence. With 5 replicas and a default
threshold of 5, that's **25 wasted upstream calls** before the
gateway as a whole starts skipping the dead provider.

This module shares the failure counter + state + recovery timer
through Redis. Five replicas at threshold 5 trip after 5 failures
observed **across all of them**, not 25.

Design
------
- One hash per provider: ``pronaos:circuit:{provider_name}`` with
  fields ``state`` / ``consecutive_failures`` / ``opened_at`` /
  ``trip_count``.
- Every state transition is an **atomic Lua script** — eliminates
  the read-then-write race two replicas would otherwise have on
  concurrent failures. Lua executes server-side, single-threaded
  per Redis instance; race-free by construction.
- **Sync** redis client (``redis.Redis``, not ``redis.asyncio.Redis``)
  so the breaker presents the same blocking-call interface as the
  in-memory version. Failover + metrics endpoint don't need to
  change. Redis-on-localhost HGET/HSET is ~50 µs — invisible against
  the multi-hundred-ms upstream LLM call latency.

Fail-open
---------
Any Redis exception during a breaker call → log a warning, return a
permissive result (allow_request → True; record_* → no-op). The
gateway must keep serving when Redis is unavailable; the worst case
is "degrades to no breaker," which is the same behaviour as a
brand-new deployment.

Threading
---------
The sync redis client is thread-safe by virtue of its connection
pool. Failover runs inside the asyncio event loop; the brief
event-loop block during the Redis call is intentional and bounded
(see "Design" above).

Why not always Redis
--------------------
The in-memory breaker is enabled by default — it's the right answer
for a single-process gateway (lower latency, no extra dep). Operators
turn on the Redis path with ``PRONAOS_CIRCUIT_BREAKER_DISTRIBUTED=true``
when they're running multiple gateway replicas.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

import redis

from pronaos.core.circuit import (
    CircuitConfig,
    CircuitState,
)
from pronaos.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Redis key shape                                                             #
# --------------------------------------------------------------------------- #


_KEY_PREFIX: Final = "pronaos:circuit"


def _key(provider_name: str) -> str:
    """Per-provider hash key. Provider names are catalog-controlled
    strings, no escaping needed."""
    return f"{_KEY_PREFIX}:{provider_name}"


# --------------------------------------------------------------------------- #
# Lua scripts                                                                 #
# --------------------------------------------------------------------------- #
#
# Each script does one logical state transition atomically. Using
# Lua (vs MULTI/EXEC) is important here because we have read-decide-
# write sequences: "read state; if condition; write new state." A
# WATCH-based transaction would retry under contention; Lua just runs
# server-side, single-threaded.

# allow_request: returns 1 if a call should be tried, 0 if the breaker
# is OPEN and the recovery window hasn't elapsed. Side effect: when an
# OPEN breaker's window has elapsed, advance to HALF_OPEN.
#
# KEYS[1] = circuit key, ARGV[1] = now (unix s, float), ARGV[2] = recovery_timeout (s)
_LUA_ALLOW_REQUEST: Final = """
local state = redis.call('HGET', KEYS[1], 'state')
if state == false or state == nil then
    state = 'closed'
end
if state == 'open' then
    local opened_at = redis.call('HGET', KEYS[1], 'opened_at')
    if opened_at then
        local elapsed = tonumber(ARGV[1]) - tonumber(opened_at)
        if elapsed >= tonumber(ARGV[2]) then
            redis.call('HSET', KEYS[1], 'state', 'half_open')
            return 1
        end
    end
    return 0
end
return 1
"""

# state (read-only with timer advance): same OPEN→HALF_OPEN advancement
# as allow_request, but doesn't change the meaning of the return for
# closed/half_open. Used by the metrics scrape path.
#
# KEYS[1] = circuit key, ARGV[1] = now, ARGV[2] = recovery_timeout
_LUA_READ_STATE: Final = """
local state = redis.call('HGET', KEYS[1], 'state')
if state == false or state == nil then
    state = 'closed'
end
if state == 'open' then
    local opened_at = redis.call('HGET', KEYS[1], 'opened_at')
    if opened_at then
        local elapsed = tonumber(ARGV[1]) - tonumber(opened_at)
        if elapsed >= tonumber(ARGV[2]) then
            redis.call('HSET', KEYS[1], 'state', 'half_open')
            return 'half_open'
        end
    end
end
return state
"""

# record_success: reset counter, close circuit, clear opened_at.
#
# KEYS[1] = circuit key
_LUA_RECORD_SUCCESS: Final = """
redis.call('HSET', KEYS[1], 'state', 'closed', 'consecutive_failures', 0)
redis.call('HDEL', KEYS[1], 'opened_at')
return 1
"""

# record_failure: increment failure counter; if HALF_OPEN or threshold
# crossed, trip to OPEN with a fresh timer and increment trip_count.
#
# Returns one of:
#   "tripped"   — fresh trip from CLOSED → OPEN (caller increments trip metric)
#   "reopened"  — fresh trip from HALF_OPEN → OPEN (caller increments trip metric)
#   "noted"     — failure recorded but no state change
#
# KEYS[1] = circuit key, ARGV[1] = now, ARGV[2] = threshold
_LUA_RECORD_FAILURE: Final = """
local state = redis.call('HGET', KEYS[1], 'state')
if state == false or state == nil then
    state = 'closed'
end
local n = redis.call('HINCRBY', KEYS[1], 'consecutive_failures', 1)
if state == 'half_open' then
    redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', ARGV[1])
    redis.call('HINCRBY', KEYS[1], 'trip_count', 1)
    return 'reopened'
end
if tonumber(n) >= tonumber(ARGV[2]) and state == 'closed' then
    redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', ARGV[1])
    redis.call('HINCRBY', KEYS[1], 'trip_count', 1)
    return 'tripped'
end
return 'noted'
"""


# --------------------------------------------------------------------------- #
# Breaker                                                                     #
# --------------------------------------------------------------------------- #


class RedisCircuitBreaker:
    """Distributed circuit breaker for one provider, backed by Redis.

    Same public interface as :class:`pronaos.core.circuit.CircuitBreaker`
    (sync methods, ``state`` / ``trip_count`` properties) so the
    failover layer doesn't need to branch on impl.

    State is shared across every replica pointing at the same Redis
    instance — five replicas observe five failures collectively before
    the breaker trips, not five failures each.
    """

    __slots__ = ("_clock", "_config", "_key", "_provider", "_redis")

    def __init__(
        self,
        *,
        provider_name: str,
        redis_client: redis.Redis[bytes],
        config: CircuitConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Note: ``time.time`` (wall clock) rather than ``time.monotonic``
        # because Redis stores ``opened_at`` and we want different
        # replicas to compare timestamps against each other. Monotonic
        # clocks aren't comparable across processes.
        self._provider = provider_name
        self._redis = redis_client
        self._config = config or CircuitConfig()
        self._clock = clock
        self._key = _key(provider_name)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CircuitState:
        try:
            raw = self._redis.eval(  # type: ignore[no-untyped-call]
                _LUA_READ_STATE,
                1,
                self._key,
                str(self._clock()),
                str(self._config.recovery_timeout_seconds),
            )
        except Exception as e:
            log.warning(
                "circuit.redis.read_state_failed",
                provider=self._provider,
                error=str(e),
            )
            # Fail-open on the read: report CLOSED so the failover
            # layer doesn't artificially keep skipping a provider
            # whose breaker we can no longer read.
            return CircuitState.CLOSED
        if isinstance(raw, bytes):
            return CircuitState(raw.decode("ascii"))
        return CircuitState(str(raw))

    @property
    def trip_count(self) -> int:
        try:
            raw = self._redis.hget(self._key, "trip_count")
        except Exception as e:
            log.warning(
                "circuit.redis.trip_count_failed",
                provider=self._provider,
                error=str(e),
            )
            return 0
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def allow_request(self) -> bool:
        try:
            raw = self._redis.eval(  # type: ignore[no-untyped-call]
                _LUA_ALLOW_REQUEST,
                1,
                self._key,
                str(self._clock()),
                str(self._config.recovery_timeout_seconds),
            )
        except Exception as e:
            log.warning(
                "circuit.redis.allow_request_failed",
                provider=self._provider,
                error=str(e),
            )
            # Fail-open: if Redis is down, don't block the request.
            # The single-replica per-process behaviour the operator
            # would otherwise see is the better fallback than denying
            # everyone.
            return True
        return int(raw) == 1

    def record_success(self) -> None:
        try:
            self._redis.eval(_LUA_RECORD_SUCCESS, 1, self._key)  # type: ignore[no-untyped-call]
        except Exception as e:
            log.warning(
                "circuit.redis.record_success_failed",
                provider=self._provider,
                error=str(e),
            )

    def record_failure(self) -> None:
        try:
            self._redis.eval(  # type: ignore[no-untyped-call]
                _LUA_RECORD_FAILURE,
                1,
                self._key,
                str(self._clock()),
                str(self._config.failure_threshold),
            )
        except Exception as e:
            log.warning(
                "circuit.redis.record_failure_failed",
                provider=self._provider,
                error=str(e),
            )


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #


class RedisCircuitBreakerRegistry:
    """Per-provider lookup that hands out :class:`RedisCircuitBreaker`
    instances. All breakers share the same Redis client.

    Same interface as :class:`pronaos.core.circuit.CircuitBreakerRegistry`:
    ``get(name)`` returns a breaker; ``snapshot()`` returns a state
    map keyed by provider name.

    Implementation note: the registry is just a thin wrapper. The
    in-memory registry caches breaker instances because the in-memory
    breaker holds mutable state; the Redis registry caches breaker
    instances purely to avoid recreating the Lua-script reference, but
    each call into a breaker hits Redis fresh — there's no local
    state to worry about.
    """

    __slots__ = ("_breakers", "_clock", "_config", "_redis")

    def __init__(
        self,
        *,
        redis_client: redis.Redis[bytes],
        config: CircuitConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis_client
        self._config = config or CircuitConfig()
        self._clock = clock
        self._breakers: dict[str, RedisCircuitBreaker] = {}

    def get(self, provider_name: str) -> RedisCircuitBreaker:
        breaker = self._breakers.get(provider_name)
        if breaker is None:
            breaker = RedisCircuitBreaker(
                provider_name=provider_name,
                redis_client=self._redis,
                config=self._config,
                clock=self._clock,
            )
            self._breakers[provider_name] = breaker
        return breaker

    def snapshot(self) -> dict[str, CircuitState]:
        """Return the current state of every breaker the registry has
        handed out. Used by the metrics scrape path.

        We deliberately only report on breakers that have been *touched*
        on this replica — that way the metric series for a provider we
        haven't routed through is omitted (consistent with the in-memory
        registry's behaviour). State value itself comes from Redis,
        which is the authoritative cross-replica view.
        """
        out: dict[str, CircuitState] = {}
        for name, breaker in self._breakers.items():
            out[name] = breaker.state
        return out
