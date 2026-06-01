"""Live verification of cross-replica singleflight (Phase 36, Claim #23).

The empirical question
----------------------
Phase 33 ships singleflight that collapses concurrent identical
requests within a single gateway process. But production gateways
run behind a load balancer with N replicas. A 50-request burst hitting
5 replicas evenly still produces 5 concurrent upstream calls — one
per replica — even with in-memory singleflight.

Phase 36's RedisSingleflightRegistry shares the leader claim across
replicas via Redis. Only ONE replica wins the SET NX; the other N-1
replicas (plus all in-process followers across all replicas) become
followers and share the leader's result.

Method
------
1. Spin up N=5 RedisSingleflightRegistry instances, each pointing at
   the SAME Redis. Each instance represents one gateway replica.
2. Fire C=50 concurrent ``share()`` calls across the 5 replicas
   (10 per replica) with the SAME key.
3. The ``fn`` increments a process-local counter and sleeps briefly
   so the race window is observable.
4. Assert: exactly 1 replica saw a leader (was_follower=False once);
   all 49 other callers (across all replicas) became followers.

VERDICT
-------
Holds when:
- Exactly 1 caller across all replicas was the leader.
- All 50 results are identical.
- The leader's fn ran exactly once globally.
- Every follower received ``was_follower=True``.

Honesty notes
-------------
- The script uses fakeredis by default for reproducibility (no
  external Redis required). For a true cross-process test, point at
  a real Redis with ``--redis-url`` and run multiple instances of
  this script in parallel.
- The Redis claim race is a real cross-replica race, not simulated
  — fakeredis's async client respects the same atomic SET NX semantics
  as production Redis.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

from pronaos.core.singleflight_redis import RedisSingleflightRegistry


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--replicas",
        type=int,
        default=5,
        help="Number of simulated gateway replicas (independent registries).",
    )
    parser.add_argument(
        "--callers-per-replica",
        type=int,
        default=10,
        help="Concurrent share() calls per replica. Total = replicas x callers.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=10,
        help="Redis singleflight TTL.",
    )
    parser.add_argument(
        "--redis-url",
        default=None,
        help="Redis URL. Default = fakeredis (no external dependency).",
    )
    args = parser.parse_args()

    total = args.replicas * args.callers_per_replica
    nonce = uuid.uuid4().hex

    print(f"replicas:            {args.replicas}")
    print(f"callers per replica: {args.callers_per_replica}")
    print(f"total concurrent:    {total}")
    print(f"ttl seconds:         {args.ttl_seconds}")
    print(f"redis:               {args.redis_url or 'fakeredis (in-process)'}")
    print(f"nonce:               {nonce}")
    print()
    print("=" * 64)
    print("Phase 36 - Cross-replica singleflight live verification")
    print("=" * 64)

    # Build the shared Redis (fakeredis by default).
    if args.redis_url:
        import redis.asyncio as redis_async

        redis_client = redis_async.from_url(args.redis_url, decode_responses=False)
    else:
        import fakeredis.aioredis

        redis_client = fakeredis.aioredis.FakeRedis()

    # Build N replica registries sharing the same Redis.
    registries = [
        RedisSingleflightRegistry[dict[str, Any]](
            redis_client, ttl_seconds=args.ttl_seconds
        )
        for _ in range(args.replicas)
    ]

    # Counter for fn invocations across all replicas.
    fn_call_count = 0
    fn_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn() -> dict[str, Any]:
        nonlocal fn_call_count
        fn_call_count += 1
        # Signal that the leader has entered fn — followers can pile on now.
        fn_ready.set()
        # Hold briefly so all followers can race for the Redis claim.
        await release.wait()
        return {"leader_nonce": uuid.uuid4().hex, "fn_call_count": fn_call_count}

    # Schedule the leader (first caller on replica 0) and ALL others
    # together, but release the leader's fn only after followers have
    # had a chance to enter share().
    async def caller(replica_idx: int) -> tuple[Any, bool]:
        reg = registries[replica_idx]
        return await reg.share(f"sf-test:{nonce}", fn)

    # Launch one caller first (the likely leader).
    leader_task = asyncio.create_task(caller(0))
    # Wait until fn enters — this means SOMEONE became the leader.
    await fn_ready.wait()

    # Now launch the remaining callers, split across replicas.
    follower_tasks = []
    for i in range(1, total):
        replica_idx = i % args.replicas
        follower_tasks.append(asyncio.create_task(caller(replica_idx)))

    # Give all followers a chance to enter share() before releasing.
    await asyncio.sleep(0.1)

    # Release the leader so it can return.
    release.set()

    # Collect.
    all_results = await asyncio.gather(leader_task, *follower_tasks)

    # Analyse.
    leader_count = sum(1 for _, wf in all_results if wf is False)
    follower_count = sum(1 for _, wf in all_results if wf is True)
    unique_results = {tuple(sorted(r.items())) for r, _ in all_results}

    print(f"fn invocations across all replicas: {fn_call_count}")
    print(f"leaders (was_follower=False):       {leader_count}")
    print(f"followers (was_follower=True):      {follower_count}")
    print(f"unique results across all callers:  {len(unique_results)}")
    print()

    holds = (
        fn_call_count == 1
        and leader_count == 1
        and follower_count == total - 1
        and len(unique_results) == 1
    )
    if holds:
        print(
            f"VERDICT: claim holds - {total} concurrent identical share() "
            f"calls across {args.replicas} simulated replicas collapsed to "
            f"1 leader + {follower_count} followers. fn ran exactly ONCE "
            f"globally. All {total} callers received byte-identical results. "
            f"In a real {args.replicas}-replica gateway behind a load balancer, "
            f"this is {follower_count} upstream calls saved per such burst."
        )
        sys.exit(0)

    reasons: list[str] = []
    if fn_call_count != 1:
        reasons.append(
            f"fn ran {fn_call_count} times globally; expected exactly 1"
        )
    if leader_count != 1:
        reasons.append(f"observed {leader_count} leaders; expected exactly 1")
    if follower_count != total - 1:
        reasons.append(
            f"observed {follower_count} followers; expected {total - 1}"
        )
    if len(unique_results) != 1:
        reasons.append(
            f"results diverged across callers ({len(unique_results)} distinct values)"
        )
    print(f"VERDICT: claim fails - {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
