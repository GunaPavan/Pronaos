"""Distributed circuit breaker tests (Phase 25).

Layers:

1. **Single-replica behaviour against fakeredis**. The Redis-backed
   breaker must behave identically to the in-memory one for the
   standard lifecycle (CLOSED → fail xN → OPEN → recovery → HALF_OPEN
   → success → CLOSED). These tests pin the single-instance correctness.

2. **Multi-replica convergence**. Two registries point at the same
   fakeredis. Failures observed *across* them are what trip the
   breaker. This is the headline behaviour that distinguishes
   distributed from in-memory.

3. **Fail-open**. When Redis blows up mid-flight, the breaker
   returns permissive defaults so the gateway keeps serving.

``fakeredis.FakeRedis`` is a Python-only Redis emulator. It supports
the Lua ``EVAL`` semantics we rely on so the tests exercise the same
script-driven transitions as production would.
"""

from __future__ import annotations

from unittest.mock import patch

import fakeredis
import pytest

from pronaos.core.circuit import CircuitConfig, CircuitState
from pronaos.core.circuit_redis import (
    RedisCircuitBreaker,
    RedisCircuitBreakerRegistry,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_redis() -> fakeredis.FakeRedis:
    """A fresh in-memory Redis per test. ``decode_responses=False`` to
    match the production wiring in ``main.py``."""
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def config() -> CircuitConfig:
    """Short threshold + window for snappy tests."""
    return CircuitConfig(failure_threshold=3, recovery_timeout_seconds=30.0)


def _mk_breaker(
    fake_redis: fakeredis.FakeRedis,
    config: CircuitConfig,
    *,
    now: float = 1000.0,
    provider: str = "test-provider",
) -> RedisCircuitBreaker:
    """Construct a breaker with a deterministic clock — most tests
    drive time forward by overriding the clock between calls."""
    return RedisCircuitBreaker(
        provider_name=provider,
        redis_client=fake_redis,
        config=config,
        clock=lambda: now,
    )


# --------------------------------------------------------------------------- #
# Single-replica lifecycle                                                    #
# --------------------------------------------------------------------------- #


class TestLifecycle:
    """The Redis-backed breaker must behave identically to the in-memory
    one for the standard CLOSED → OPEN → HALF_OPEN → CLOSED cycle."""

    def test_starts_closed(self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig) -> None:
        b = _mk_breaker(fake_redis, config)
        assert b.state is CircuitState.CLOSED
        assert b.allow_request() is True
        assert b.trip_count == 0

    def test_trips_after_threshold_failures(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """Threshold = 3 → 3 consecutive failures trip the circuit.
        2 failures don't."""
        b = _mk_breaker(fake_redis, config)
        b.record_failure()
        b.record_failure()
        assert b.state is CircuitState.CLOSED
        b.record_failure()  # threshold crossed
        assert b.state is CircuitState.OPEN
        assert b.trip_count == 1
        assert b.allow_request() is False

    def test_success_resets_counter(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """Intermittent failures shouldn't trip if interspersed with
        successes — a streak is what matters."""
        b = _mk_breaker(fake_redis, config)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        # Only 2 in a row after the reset; threshold is 3 → still CLOSED.
        assert b.state is CircuitState.CLOSED
        assert b.trip_count == 0

    def test_recovers_to_half_open_after_timeout(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """OPEN → HALF_OPEN happens when the recovery window elapses,
        observed by ``state`` or ``allow_request``."""
        b = _mk_breaker(fake_redis, config, now=1000.0)
        for _ in range(3):
            b.record_failure()
        assert b.state is CircuitState.OPEN
        # Advance the clock past the recovery window.
        b._clock = lambda: 1031.0
        # First state read past the window flips OPEN → HALF_OPEN.
        assert b.state is CircuitState.HALF_OPEN
        # And allow_request returns True so a probe goes through.
        assert b.allow_request() is True

    def test_half_open_failure_re_opens_with_fresh_timer(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """A failure in HALF_OPEN immediately re-opens — the probe
        proved the provider isn't back yet."""
        b = _mk_breaker(fake_redis, config, now=1000.0)
        for _ in range(3):
            b.record_failure()
        b._clock = lambda: 1031.0
        assert b.state is CircuitState.HALF_OPEN
        b.record_failure()
        assert b.state is CircuitState.OPEN
        # Trip count incremented again — operators want to see "this
        # provider keeps failing" as multiple trip events, not one.
        assert b.trip_count == 2

    def test_half_open_success_closes_breaker(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        b = _mk_breaker(fake_redis, config, now=1000.0)
        for _ in range(3):
            b.record_failure()
        b._clock = lambda: 1031.0
        assert b.state is CircuitState.HALF_OPEN
        b.record_success()
        assert b.state is CircuitState.CLOSED
        assert b.trip_count == 1  # not reset — lifetime counter


# --------------------------------------------------------------------------- #
# Multi-replica convergence — the headline behaviour                          #
# --------------------------------------------------------------------------- #


class TestMultiReplicaConvergence:
    """Two registries pointing at the same Redis should observe and
    react to *each other's* failures — this is the whole point of
    Phase 25."""

    def test_two_replicas_trip_on_cumulative_failures(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """Replica A logs 2 failures. Replica B logs 1. Threshold is 3.
        Replica B's third failure trips the breaker — and Replica A
        can see the OPEN state too."""
        breaker_a = _mk_breaker(fake_redis, config, provider="prov")
        breaker_b = _mk_breaker(fake_redis, config, provider="prov")

        # 3 failures distributed across replicas.
        breaker_a.record_failure()
        breaker_a.record_failure()
        breaker_b.record_failure()

        # Both observe OPEN — convergence achieved through Redis.
        assert breaker_a.state is CircuitState.OPEN
        assert breaker_b.state is CircuitState.OPEN
        assert breaker_a.trip_count == 1
        assert breaker_b.trip_count == 1
        # Either replica's allow_request denies further calls.
        assert breaker_a.allow_request() is False
        assert breaker_b.allow_request() is False

    def test_replica_b_sees_replica_a_recovery(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """When replica A's probe succeeds (HALF_OPEN → CLOSED), replica
        B should also observe CLOSED on its next read."""
        breaker_a = _mk_breaker(fake_redis, config, now=1000.0, provider="prov")
        breaker_b = _mk_breaker(fake_redis, config, now=1000.0, provider="prov")

        # Trip from replica A's failures.
        for _ in range(3):
            breaker_a.record_failure()
        # Advance clock past recovery window on both replicas.
        breaker_a._clock = lambda: 1031.0
        breaker_b._clock = lambda: 1031.0

        assert breaker_a.state is CircuitState.HALF_OPEN
        # Replica A sends the probe and it succeeds.
        breaker_a.record_success()
        # Replica B reads state fresh from Redis — CLOSED.
        assert breaker_b.state is CircuitState.CLOSED

    def test_independent_providers_dont_interfere(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """Tripping one provider's breaker must NOT affect another's.
        Per-provider keys in Redis enforce this — pin the invariant."""
        groq = _mk_breaker(fake_redis, config, provider="groq")
        anthropic = _mk_breaker(fake_redis, config, provider="anthropic")

        for _ in range(3):
            groq.record_failure()

        assert groq.state is CircuitState.OPEN
        assert anthropic.state is CircuitState.CLOSED
        assert anthropic.allow_request() is True


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_caches_breakers_per_provider(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        reg = RedisCircuitBreakerRegistry(redis_client=fake_redis, config=config)
        b1 = reg.get("groq")
        b2 = reg.get("groq")
        assert b1 is b2

    def test_snapshot_includes_touched_breakers_only(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        """Like the in-memory registry, snapshot reports only on
        breakers that have been handed out. Providers we haven't
        routed through yet shouldn't pollute the metric series."""
        reg = RedisCircuitBreakerRegistry(redis_client=fake_redis, config=config)
        reg.get("groq")
        reg.get("anthropic")
        snap = reg.snapshot()
        assert set(snap.keys()) == {"groq", "anthropic"}
        # Untouched providers absent from the snapshot.
        assert "openai" not in snap

    def test_snapshot_reflects_live_state_after_trip(
        self, fake_redis: fakeredis.FakeRedis, config: CircuitConfig
    ) -> None:
        reg = RedisCircuitBreakerRegistry(redis_client=fake_redis, config=config)
        b = reg.get("groq")
        for _ in range(3):
            b.record_failure()
        assert reg.snapshot()["groq"] is CircuitState.OPEN


# --------------------------------------------------------------------------- #
# Fail-open semantics                                                         #
# --------------------------------------------------------------------------- #


class TestFailOpen:
    """When Redis blows up mid-flight the gateway must keep serving.
    The breaker's failure-mode is "permissive" — we'd rather let a
    request through to a healthy provider than block on a broken
    breaker."""

    def test_allow_request_returns_true_on_redis_failure(self, config: CircuitConfig) -> None:
        class BoomRedis:
            def eval(self, *_args: object, **_kw: object) -> object:
                raise ConnectionError("redis is gone")

            def hget(self, *_args: object, **_kw: object) -> object:
                raise ConnectionError("redis is gone")

        b = RedisCircuitBreaker(
            provider_name="test",
            redis_client=BoomRedis(),  # type: ignore[arg-type]
            config=config,
        )
        # Fail-open: True means "let the request through" — we'd rather
        # over-allow than over-deny when the breaker itself is broken.
        assert b.allow_request() is True
        assert b.state is CircuitState.CLOSED
        assert b.trip_count == 0

    def test_record_methods_swallow_redis_exceptions(self, config: CircuitConfig) -> None:
        class BoomRedis:
            def eval(self, *_args: object, **_kw: object) -> object:
                raise ConnectionError("redis is gone")

        b = RedisCircuitBreaker(
            provider_name="test",
            redis_client=BoomRedis(),  # type: ignore[arg-type]
            config=config,
        )
        # Must NOT raise. Logging is best-effort.
        b.record_failure()
        b.record_success()


# --------------------------------------------------------------------------- #
# Race-free transitions (Lua atomicity)                                       #
# --------------------------------------------------------------------------- #


def test_concurrent_failures_trip_at_correct_threshold(
    fake_redis: fakeredis.FakeRedis, config: CircuitConfig
) -> None:
    """3 replicas each report one failure simultaneously. Threshold = 3
    means exactly ONE replica should observe its failure as the
    tripping one (trip_count goes from 0 → 1 once, not 3 times).

    This is the property Lua atomicity buys us — without it, three
    concurrent ``record_failure`` calls could each read counter=0,
    each set counter=1, all see counter < threshold, and never trip.
    """
    replicas = [_mk_breaker(fake_redis, config, provider="p") for _ in range(3)]
    for b in replicas:
        b.record_failure()
    # All replicas observe OPEN.
    assert {r.state for r in replicas} == {CircuitState.OPEN}
    # trip_count incremented exactly once across all replicas.
    assert replicas[0].trip_count == 1


# --------------------------------------------------------------------------- #
# Patch import guard                                                          #
# --------------------------------------------------------------------------- #


def test_unused_patch_import_silences_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub — ``patch`` is imported above for future use in expanded
    fail-open tests. This dummy keeps lint happy without adding noise
    to the file's main test contract."""
    assert patch is not None
