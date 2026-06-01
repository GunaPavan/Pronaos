"""RedisSingleflightRegistry unit tests (Phase 36).

Same semantics as the in-memory registry (Phase 33):

- Single call: leader runs ``fn``, returns ``(result, False)``.
- Concurrent same key (same replica): only one ``fn`` invocation;
  followers attach to the leader's local future.
- Two REGISTRIES sharing one fakeredis (simulates two replicas):
  only ONE registry runs ``fn``; the other becomes a cross-replica
  follower.
- Leader fails: every follower sees a CrossReplicaLeaderError
  carrying the original class name + message.
- TTL recovery: a "dead" leader (entry expired) lets the next caller
  become a fresh leader.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest

from pronaos.core.singleflight_redis import (
    CrossReplicaLeaderError,
    RedisSingleflightRegistry,
)


@pytest.fixture
async def redis_client() -> Any:
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.fixture
async def registry(redis_client: Any) -> RedisSingleflightRegistry[dict[str, Any]]:
    return RedisSingleflightRegistry[dict[str, Any]](
        redis_client,
        ttl_seconds=10,
    )


# --------------------------------------------------------------------------- #
# Single-replica semantics                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_call_runs_fn_once(
    registry: RedisSingleflightRegistry[dict[str, Any]],
) -> None:
    call_count = 0

    async def fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"value": "hello"}

    result, was_follower = await registry.share("k1", fn)
    assert result == {"value": "hello"}
    assert was_follower is False
    assert call_count == 1


@pytest.mark.asyncio
async def test_local_fast_path_concurrent_same_key(
    registry: RedisSingleflightRegistry[dict[str, Any]],
) -> None:
    """N concurrent same-process calls share a single Redis claim.

    The local lock + futures dict catches them BEFORE Redis even sees
    them. fn runs exactly once even though we never touch Redis after
    the first call.
    """
    call_count = 0
    leader_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        leader_ready.set()
        await release.wait()
        return {"value": "shared"}

    leader_task = asyncio.create_task(registry.share("k", fn))
    await leader_ready.wait()
    follower_tasks = [asyncio.create_task(registry.share("k", fn)) for _ in range(10)]
    await asyncio.sleep(0)
    assert registry.in_flight_count() == 1
    release.set()

    results = await asyncio.gather(leader_task, *follower_tasks)
    assert call_count == 1
    assert all(r == {"value": "shared"} for r, _ in results)
    leader_count = sum(1 for _, wf in results if wf is False)
    follower_count = sum(1 for _, wf in results if wf is True)
    assert leader_count == 1
    assert follower_count == 10
    assert registry.in_flight_count() == 0


# --------------------------------------------------------------------------- #
# Cross-replica semantics                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_replicas_share_one_leader(redis_client: Any) -> None:
    """Two RedisSingleflightRegistry instances sharing one Redis backend
    behave as two replicas of the same gateway: only ONE runs fn.
    """
    rep_a = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)
    rep_b = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)

    call_count_a = 0
    call_count_b = 0
    a_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn_a() -> dict[str, Any]:
        nonlocal call_count_a
        call_count_a += 1
        a_ready.set()
        await release.wait()
        return {"leader": "a"}

    async def fn_b() -> dict[str, Any]:
        nonlocal call_count_b
        call_count_b += 1
        return {"leader": "b"}

    # Replica A starts first — wins the Redis claim.
    a_task = asyncio.create_task(rep_a.share("k", fn_a))
    await a_ready.wait()  # A has the claim

    # Replica B arrives. Should become a cross-replica follower,
    # NOT run fn_b.
    b_task = asyncio.create_task(rep_b.share("k", fn_b))
    await asyncio.sleep(0.01)  # give B a chance to enter share()

    release.set()
    result_a, wf_a = await a_task
    result_b, wf_b = await b_task

    assert call_count_a == 1
    assert call_count_b == 0  # B never ran its own fn
    assert result_a == {"leader": "a"}
    assert result_b == {"leader": "a"}  # B got A's result
    assert wf_a is False
    assert wf_b is True


@pytest.mark.asyncio
async def test_distinct_keys_do_not_collide(redis_client: Any) -> None:
    """Different keys → different leaders → independent execution."""
    rep_a = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)
    rep_b = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)

    calls: list[str] = []

    async def fn_for(label: str):  # type: ignore[no-untyped-def]
        async def _fn() -> dict[str, Any]:
            calls.append(label)
            return {"who": label}

        return _fn

    a_task = asyncio.create_task(rep_a.share("k1", await fn_for("A")))
    b_task = asyncio.create_task(rep_b.share("k2", await fn_for("B")))

    result_a, wf_a = await a_task
    result_b, wf_b = await b_task

    assert {"A", "B"} <= set(calls)
    assert wf_a is False and wf_b is False  # both are leaders, distinct keys


# --------------------------------------------------------------------------- #
# Failure semantics                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leader_failure_propagates_cross_replica(redis_client: Any) -> None:
    """When the leader's fn raises, followers across replicas see a
    CrossReplicaLeaderError carrying the original message."""
    rep_a = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)
    rep_b = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=10)

    a_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn_a() -> dict[str, Any]:
        a_ready.set()
        await release.wait()
        raise ValueError("upstream blew up")

    async def fn_b() -> dict[str, Any]:
        return {"never": "runs"}

    a_task = asyncio.create_task(rep_a.share("k", fn_a))
    await a_ready.wait()
    b_task = asyncio.create_task(rep_b.share("k", fn_b))
    await asyncio.sleep(0.01)
    release.set()

    # Replica A raises its original exception.
    with pytest.raises(ValueError, match="upstream blew up"):
        await a_task
    # Replica B raises a CrossReplicaLeaderError mirroring A's failure.
    with pytest.raises(CrossReplicaLeaderError) as exc_info:
        await b_task
    assert exc_info.value.leader_exc_class == "ValueError"
    assert "upstream blew up" in str(exc_info.value)


@pytest.mark.asyncio
async def test_after_leader_completes_new_caller_runs_fresh(
    registry: RedisSingleflightRegistry[dict[str, Any]],
) -> None:
    """Sequential calls (one completes before the next starts) each
    become their own leader. No stuck "follower" semantics."""
    call_count = 0

    async def fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"n": call_count}

    r1, wf1 = await registry.share("k", fn)
    # TTL-evict the entry by deleting it directly — simulates either
    # explicit cleanup or natural TTL expiry between calls.
    await registry._redis.delete("pronaos:singleflight:k")
    r2, wf2 = await registry.share("k", fn)

    assert call_count == 2
    assert r1 == {"n": 1}
    assert r2 == {"n": 2}
    assert wf1 is False
    assert wf2 is False


@pytest.mark.asyncio
async def test_completed_entry_is_followed_not_re_run(
    registry: RedisSingleflightRegistry[dict[str, Any]],
) -> None:
    """If a previous call's "done" envelope is still in Redis (within
    TTL), the next caller becomes a follower and gets the cached
    result. This is fine — it means the cache effectively absorbed the
    second call."""
    call_count = 0

    async def fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"first_call_result": True}

    r1, wf1 = await registry.share("k", fn)
    assert wf1 is False
    # Second call before the entry expires: hits the existing "done"
    # envelope and becomes a follower.
    r2, wf2 = await registry.share("k", fn)
    assert call_count == 1  # fn ran exactly once
    assert wf2 is True
    assert r1 == r2


# --------------------------------------------------------------------------- #
# TTL recovery                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dead_leader_entry_expires_and_next_caller_recovers(
    redis_client: Any,
) -> None:
    """If a leader writes a 'pending' envelope and then dies, the TTL
    expires and the next caller takes over as a fresh leader."""
    # Use a very short TTL so we can test the recovery path quickly.
    registry = RedisSingleflightRegistry[dict[str, Any]](redis_client, ttl_seconds=1)

    # Manually plant a stale 'pending' envelope as if a previous leader
    # crashed mid-call.
    import json

    await redis_client.set("pronaos:singleflight:k", json.dumps({"state": "pending"}), ex=1)
    # Wait past the TTL.
    await asyncio.sleep(1.2)

    # Next caller should become a fresh leader.
    call_count = 0

    async def fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"recovered": True}

    result, was_follower = await registry.share("k", fn)
    assert call_count == 1
    assert was_follower is False
    assert result == {"recovered": True}
