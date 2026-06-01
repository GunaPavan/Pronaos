"""Multi-replica circuit-breaker convergence demo (Phase 25).

Simulates N gateway replicas all pointing at the same Redis instance.
Each replica logs ONE failure for the same broken provider. With the
in-memory breaker, N replicas would need N × threshold failures
before any of them tripped. With the Redis-backed breaker, the
*cumulative* failure count is what trips — N failures at threshold N
trip every replica at the same logical moment.

Usage
-----
The default backend is fakeredis (no docker needed). Pass
``--redis-url redis://localhost:6379/0`` to demonstrate against a
real Redis instance from ``docker compose up redis``.

Both backends run the SAME Lua scripts — fakeredis is a Python-port
Redis-protocol emulator, not a mock. The convergence property is the
same in both cases; using fakeredis just removes the network hop.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from pronaos.core.circuit import CircuitConfig, CircuitState
from pronaos.core.circuit_redis import RedisCircuitBreakerRegistry


def _build_redis(redis_url: str | None) -> Any:
    """Pick the Redis backend. Real ``redis://`` URL → production
    client; otherwise a fakeredis instance for laptop-friendly demos."""
    if redis_url is None:
        import fakeredis

        print("(no --redis-url given → using fakeredis in-process emulator)")
        return fakeredis.FakeRedis(decode_responses=False)
    import redis

    client = redis.Redis.from_url(redis_url, decode_responses=False)
    client.ping()  # fail fast if the URL is wrong
    print(f"connected to {redis_url}")
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replicas",
        type=int,
        default=5,
        help="Number of simulated gateway replicas (default: 5).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Circuit breaker failure threshold (default: 5 — matches production).",
    )
    parser.add_argument(
        "--redis-url",
        default=None,
        help="If set, use a real Redis. Otherwise uses fakeredis in-process.",
    )
    args = parser.parse_args()

    if args.replicas < 2:
        print("error: need at least 2 replicas to show convergence", file=sys.stderr)
        return 2

    redis_client = _build_redis(args.redis_url)
    config = CircuitConfig(
        failure_threshold=args.threshold, recovery_timeout_seconds=30.0
    )

    # Build one registry per "replica." All point at the same Redis,
    # so they share the breaker state for the same provider name.
    registries = [
        RedisCircuitBreakerRegistry(redis_client=redis_client, config=config)
        for _ in range(args.replicas)
    ]
    breakers = [reg.get("broken-provider") for reg in registries]

    print()
    print(f"replicas:             {args.replicas}")
    print(f"failure threshold:    {args.threshold}")
    print(f"in-memory equivalent: {args.replicas * args.threshold} "
          "failures needed before any replica trips")
    print()

    # Each replica records one failure. With Redis-backed convergence,
    # the breaker should trip on the THRESHOLDth failure, regardless
    # of which replica saw it.
    print(f"distributing {args.threshold} failures across replicas:")
    start = time.monotonic()
    for i in range(args.threshold):
        # Pick the next replica in round-robin order.
        replica_idx = i % args.replicas
        breakers[replica_idx].record_failure()
        # Read state from a DIFFERENT replica to prove cross-replica
        # visibility. Replica 0 always observes — even when replica 1
        # logged the failure, replica 0 should see it.
        observer = breakers[0]
        state = observer.state.value
        print(
            f"  failure #{i + 1}: logged on replica {replica_idx} → "
            f"replica 0 observes state = {state}"
        )
    elapsed_ms = (time.monotonic() - start) * 1000.0

    print()
    print("final state per replica (all should be OPEN):")
    for i, b in enumerate(breakers):
        print(f"  replica {i}: state={b.state.value} trip_count={b.trip_count}")

    all_open = all(b.state is CircuitState.OPEN for b in breakers)
    same_trip = all(b.trip_count == 1 for b in breakers)

    print()
    print("=" * 64)
    print(f"convergence: {args.threshold} cumulative failures → {args.replicas} replicas tripped")
    print(f"wall clock for the trip sequence: {elapsed_ms:.1f} ms")
    print(f"in-memory equivalent would have needed: "
          f"{args.replicas * args.threshold} failures")
    print()

    if all_open and same_trip:
        print(
            "✅ VERDICT: claim holds — Redis-backed breaker converges across "
            f"{args.replicas} replicas after {args.threshold} cumulative "
            f"failures (vs {args.replicas * args.threshold} for in-memory)."
        )
        return 0
    print(
        "❌ VERDICT: convergence failed — replicas disagree on state. "
        "Inspect the Redis connection and/or the Lua script behavior."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
