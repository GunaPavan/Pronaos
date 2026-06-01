"""Per-provider circuit breaker.

Why this exists alongside failover
----------------------------------
``execute_with_failover`` handles a single request: try the primary
provider, on retryable error try the fallback, etc. It is *stateless*
across requests — every new request starts with the same chain order
and pays the same first-hop cost even if the primary has been failing
for the last 10 minutes.

The circuit breaker plugs that gap. It tracks per-provider failure
counts across requests; when one provider has racked up enough
consecutive failures, the breaker opens and the failover layer skips
that provider entirely until a recovery window has elapsed. The result:

- Under healthy conditions the breaker is invisible (CLOSED, no cost)
- Under a real provider outage, traffic routes around the dead provider
  within seconds rather than per-request
- A single probe attempt after the recovery window keeps the breaker
  honest — if the provider really came back, traffic resumes; if not,
  the circuit re-opens with a fresh timer.

This implements the classic three-state design (Hystrix /
Resilience4j) with one process-local registry of breakers keyed by
provider name. State is in-process — distributed sharing across
gateway replicas would need Redis-backed state, deferred for now;
each replica running its own breaker is correct (if it observed the
failures, its breaker tripped) and simple to reason about.

Threading note
--------------
We don't lock around state mutations. Failover runs inside an async
request handler so two simultaneous decisions can race. The worst-case
race is one extra probe through an open breaker — not a correctness
bug. Adding asyncio.Lock would serialise every provider call;
trading correctness-imperceptibly-different for measurable latency is
the wrong call here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class CircuitState(StrEnum):
    """Three-state breaker. Values are stable strings so they survive log
    serialisation / Prometheus label encoding without translation."""

    CLOSED = "closed"  # normal — calls allowed, failures tracked
    OPEN = "open"  # tripped — calls denied until recovery window
    HALF_OPEN = "half_open"  # probing — one call allowed to test recovery


# Default tuning. Picked for the LLM-gateway workload:
# - 5 consecutive failures means real degradation, not a single bad request
# - 30s recovery window is short enough that a recovered provider doesn't
#   spend minutes locked out, long enough that a thrashing provider doesn't
#   get re-probed on every request
DEFAULT_FAILURE_THRESHOLD: Final = 5
DEFAULT_RECOVERY_TIMEOUT_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    """Per-breaker tuning. Defaults are fine for most providers; bump
    ``failure_threshold`` for a flakier upstream you want to be more
    forgiving of."""

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_timeout_seconds: float = DEFAULT_RECOVERY_TIMEOUT_SECONDS


class CircuitBreaker:
    """Three-state circuit breaker for a single provider.

    Lifecycle
    ---------
    ::

        CLOSED ──(N consecutive failures)──► OPEN
           ▲                                    │
           │                                    │ (recovery_timeout elapses)
           │  (success)                         ▼
           └─────────────  HALF_OPEN  ◄─────────┘
                              │
                              └── (failure) ──► OPEN (fresh timer)

    The ``allow_request`` method is what callers ask before invoking
    the provider. It is timer-aware: an OPEN breaker whose recovery
    window has elapsed transitions to HALF_OPEN inside ``allow_request``
    and returns True. The probe's result determines the next state.
    """

    __slots__ = (
        "_clock",
        "_config",
        "_consecutive_failures",
        "_opened_at",
        "_state",
        "_trip_count",
    )

    def __init__(
        self,
        *,
        config: CircuitConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or CircuitConfig()
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        # Cumulative trip count — exposed for the trips metric so dashboards
        # can show "how often does this breaker fire" over time.
        self._trip_count = 0

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CircuitState:
        """Read the current state, advancing the OPEN→HALF_OPEN clock if due.

        Calling this is idempotent — exposing it as a property keeps the
        metric scrape path side-effect-light (transitioning to HALF_OPEN
        purely on read is correct: the next allow_request will simply
        observe HALF_OPEN, exactly as if the read had been an
        allow_request).
        """
        self._maybe_recover()
        return self._state

    @property
    def trip_count(self) -> int:
        return self._trip_count

    def allow_request(self) -> bool:
        """Return True if a request should be attempted on this provider.

        - CLOSED: always True.
        - OPEN: True iff the recovery timeout has elapsed (in which case
          the breaker transitions to HALF_OPEN on the way out).
        - HALF_OPEN: True, but the caller must promptly report success
          or failure to resolve the probe.
        """
        self._maybe_recover()
        return self._state is not CircuitState.OPEN

    def record_success(self) -> None:
        """Reset the failure counter and close the circuit.

        Called after a provider call returned successfully. Even in
        CLOSED state we reset the counter — a streak of successes
        between intermittent failures means the provider isn't really
        broken, and the breaker should not trip on intermittent
        background noise.
        """
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        """Record a failure; trip the breaker if the threshold is reached.

        In HALF_OPEN state, a single failure immediately re-opens with
        a fresh timer — the probe just told us the provider isn't
        really back yet.
        """
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN:
            # Probe failed → re-open immediately with a fresh recovery
            # window. Don't wait for the threshold a second time.
            self._open_now()
            return
        if self._consecutive_failures >= self._config.failure_threshold:
            self._open_now()

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _open_now(self) -> None:
        if self._state is not CircuitState.OPEN:
            self._trip_count += 1
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()

    def _maybe_recover(self) -> None:
        """Advance OPEN→HALF_OPEN if the recovery window has elapsed.

        Pure timer evaluation; no other side effects. Idempotent.
        """
        if self._state is not CircuitState.OPEN:
            return
        if self._opened_at is None:
            return
        elapsed = self._clock() - self._opened_at
        if elapsed >= self._config.recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN


class CircuitBreakerRegistry:
    """Per-provider-name lookup, lazily creating breakers on first use.

    The gateway has a fixed roster of providers (Groq, Anthropic, etc.)
    so the registry is short-lived and small. A single instance is
    installed on ``app.state.circuit_registry`` at startup; the
    failover layer reads it on every request.
    """

    __slots__ = ("_breakers", "_clock", "_config")

    def __init__(
        self,
        *,
        config: CircuitConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._config = config or CircuitConfig()
        self._clock = clock

    def get(self, provider_name: str) -> CircuitBreaker:
        breaker = self._breakers.get(provider_name)
        if breaker is None:
            breaker = CircuitBreaker(config=self._config, clock=self._clock)
            self._breakers[provider_name] = breaker
        return breaker

    def snapshot(self) -> dict[str, CircuitState]:
        """Read all breaker states. Used by the metrics exporter so the
        Prometheus scrape doesn't need to know which providers have been
        seen — it gets the live set."""
        return {name: br.state for name, br in self._breakers.items()}
