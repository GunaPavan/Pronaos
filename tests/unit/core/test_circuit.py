"""Circuit breaker tests.

Two distinct threads:

1. **State machine in isolation** — drive a CircuitBreaker through every
   transition with a fake clock. The clock is the only non-deterministic
   input; mocking it gives us the timer-recovery transition without
   ``sleep`` calls slowing the suite.

2. **Registry semantics** — lazy creation, isolation between provider
   names, snapshot read for the metrics exporter.

Failover *integration* lives in test_failover.py — these tests stay
focused on the breaker primitive itself.
"""

from __future__ import annotations

from pronaos.core.circuit import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitConfig,
    CircuitState,
)

# --------------------------------------------------------------------------- #
# Fake clock helper                                                            #
# --------------------------------------------------------------------------- #


class FakeClock:
    """Mutable monotonic clock. Lets tests advance time without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- #
# State machine                                                                #
# --------------------------------------------------------------------------- #


def test_breaker_starts_closed_and_allows_requests() -> None:
    """Fresh breaker should be in the happy-path state — letting every
    request through and showing no trips."""
    br = CircuitBreaker()
    assert br.state is CircuitState.CLOSED
    assert br.allow_request() is True
    assert br.trip_count == 0


def test_consecutive_failures_trip_breaker_at_threshold() -> None:
    """Exactly ``failure_threshold`` consecutive failures must flip the
    state to OPEN. Off-by-one is the bug to catch here."""
    br = CircuitBreaker(config=CircuitConfig(failure_threshold=3))

    br.record_failure()
    assert br.state is CircuitState.CLOSED  # 1 < 3
    br.record_failure()
    assert br.state is CircuitState.CLOSED  # 2 < 3
    br.record_failure()
    assert br.state is CircuitState.OPEN  # 3 >= 3
    assert br.trip_count == 1


def test_intermittent_success_resets_failure_counter() -> None:
    """Two failures then a success then two more failures must NOT trip
    a threshold-3 breaker — the streak was broken. Intermittent noise is
    not provider degradation; the counter must reset on every success."""
    br = CircuitBreaker(config=CircuitConfig(failure_threshold=3))

    br.record_failure()
    br.record_failure()
    br.record_success()  # resets counter
    br.record_failure()
    br.record_failure()
    assert br.state is CircuitState.CLOSED  # only 2 consecutive now
    assert br.trip_count == 0


def test_open_breaker_denies_requests_during_recovery_window() -> None:
    """During the recovery window, an open breaker must refuse to admit
    any request. The whole point is to spare the dead provider further
    traffic until it has a chance to recover."""
    clock = FakeClock()
    br = CircuitBreaker(
        config=CircuitConfig(failure_threshold=2, recovery_timeout_seconds=10.0),
        clock=clock,
    )
    br.record_failure()
    br.record_failure()
    assert br.state is CircuitState.OPEN
    assert br.allow_request() is False

    # Advance partway — still denied.
    clock.advance(5.0)
    assert br.allow_request() is False


def test_open_breaker_transitions_to_half_open_after_recovery() -> None:
    """When the recovery window elapses, the breaker must enter HALF_OPEN
    and admit a single probe request. Reading ``state`` should also
    advance the transition — symmetric with allow_request — so the
    Prometheus exporter sees the live state."""
    clock = FakeClock()
    br = CircuitBreaker(
        config=CircuitConfig(failure_threshold=2, recovery_timeout_seconds=10.0),
        clock=clock,
    )
    br.record_failure()
    br.record_failure()
    clock.advance(10.0)
    assert br.allow_request() is True
    assert br.state is CircuitState.HALF_OPEN


def test_half_open_success_closes_breaker() -> None:
    """A successful probe in HALF_OPEN is the signal the provider is
    healthy again — fully reset the breaker, including the trip
    counter's lifecycle (count goes up by one for the original trip,
    NOT for the recovery)."""
    clock = FakeClock()
    br = CircuitBreaker(
        config=CircuitConfig(failure_threshold=2, recovery_timeout_seconds=10.0),
        clock=clock,
    )
    br.record_failure()
    br.record_failure()
    clock.advance(10.0)
    br.allow_request()  # transitions to HALF_OPEN
    br.record_success()
    assert br.state is CircuitState.CLOSED
    # Trip counter records only the original trip — a successful
    # recovery is not a new trip.
    assert br.trip_count == 1


def test_half_open_failure_reopens_with_fresh_timer() -> None:
    """A failed probe means the provider is still down — re-open
    immediately (don't wait for the threshold a second time) with a
    fresh recovery window. The trip counter increments again because
    this is a new trip event."""
    clock = FakeClock()
    br = CircuitBreaker(
        config=CircuitConfig(failure_threshold=2, recovery_timeout_seconds=10.0),
        clock=clock,
    )
    br.record_failure()
    br.record_failure()  # OPEN, t=0
    clock.advance(10.0)
    br.allow_request()  # transitions to HALF_OPEN at t=10
    br.record_failure()  # probe failed → OPEN at t=10
    assert br.state is CircuitState.OPEN
    assert br.trip_count == 2

    # Fresh timer: at t=15 (5s after re-open) still OPEN.
    clock.advance(5.0)
    assert br.allow_request() is False

    # At t=20 (10s after re-open) recovery window elapses again.
    clock.advance(5.0)
    assert br.allow_request() is True
    assert br.state is CircuitState.HALF_OPEN


def test_threshold_of_one_trips_on_first_failure() -> None:
    """Defensive edge case — a degenerate threshold=1 breaker should
    trip on the first failure. Useful for the most-critical providers
    where you'd rather be conservative than ride out one error."""
    br = CircuitBreaker(config=CircuitConfig(failure_threshold=1))
    br.record_failure()
    assert br.state is CircuitState.OPEN


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #


def test_registry_creates_breakers_lazily() -> None:
    """Asking for a never-seen provider must create the breaker on
    demand, and subsequent ``get`` calls must return the SAME instance
    (so failure counts accumulate across requests)."""
    reg = CircuitBreakerRegistry()
    br1 = reg.get("groq")
    br2 = reg.get("groq")
    assert br1 is br2


def test_registry_isolates_breakers_per_provider() -> None:
    """Tripping the breaker for one provider must not affect another."""
    reg = CircuitBreakerRegistry(config=CircuitConfig(failure_threshold=1))
    reg.get("groq").record_failure()
    assert reg.get("groq").state is CircuitState.OPEN
    assert reg.get("anthropic").state is CircuitState.CLOSED


def test_registry_snapshot_includes_only_seen_providers() -> None:
    """``snapshot`` is consumed by the Prometheus exporter. It must
    include every breaker that's been touched — and only those, so
    the metric doesn't claim a state for a provider the gateway has
    never invoked."""
    reg = CircuitBreakerRegistry()
    reg.get("groq")  # touch one
    snap = reg.snapshot()
    assert "groq" in snap
    assert "anthropic" not in snap
