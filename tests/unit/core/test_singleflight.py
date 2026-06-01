"""SingleflightRegistry unit tests (Phase 33).

Asserts the core semantics:

- Single request: leader runs ``fn``, returns ``(result, False)``.
- Concurrent same key: only one ``fn`` invocation; all callers get
  the same result; only one is the leader (``was_follower=False``)
  and the rest are followers (``True``).
- Concurrent different keys: no dedup; each ``fn`` runs independently.
- Leader fails: every follower sees the same exception.
- Sequential same key (leader completes before next caller): the next
  caller is a fresh leader, not a follower of the dead future.
- ``in_flight_count`` reflects live leaders.

We use ``asyncio.Event`` to gate the leader's ``fn`` so the race
window is deterministic — no sleep-based timing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pronaos.core.singleflight import SingleflightRegistry


@pytest.mark.asyncio
async def test_single_call_runs_fn_once() -> None:
    sf = SingleflightRegistry[str]()
    call_count = 0

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        return "hello"

    result, was_follower = await sf.share("k", fn)
    assert result == "hello"
    assert was_follower is False
    assert call_count == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_collapses_to_one_call() -> None:
    """N concurrent calls with the same key → fn runs ONCE.

    Uses an asyncio.Event to hold the leader's fn open until all
    followers have arrived. This makes the dedup window deterministic.
    """
    sf = SingleflightRegistry[str]()
    call_count = 0
    leader_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        leader_ready.set()
        # Hold the leader open until we say go — gives followers a
        # chance to arrive at the share() call.
        await release.wait()
        return "shared-result"

    # Launch the leader.
    leader_task = asyncio.create_task(sf.share("k", fn))
    # Wait until the leader's fn has actually entered.
    await leader_ready.wait()

    # Launch N followers — they should attach to the leader's future.
    follower_tasks = [asyncio.create_task(sf.share("k", fn)) for _ in range(20)]

    # Give all followers a tick to enter share().
    await asyncio.sleep(0)
    # In_flight_count is 1 (one leader future).
    assert sf.in_flight_count() == 1

    # Release the leader.
    release.set()

    # Gather all.
    results = await asyncio.gather(leader_task, *follower_tasks)

    # fn ran exactly once.
    assert call_count == 1
    # All results identical.
    assert all(r == "shared-result" for r, _ in results)
    # Exactly ONE caller saw was_follower=False (the leader).
    leader_count = sum(1 for _, wf in results if wf is False)
    follower_count = sum(1 for _, wf in results if wf is True)
    assert leader_count == 1
    assert follower_count == 20
    # In-flight registry empty after completion.
    assert sf.in_flight_count() == 0


@pytest.mark.asyncio
async def test_concurrent_different_keys_no_dedup() -> None:
    """Distinct keys run independently — no false dedup."""
    sf = SingleflightRegistry[str]()
    call_count = 0

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        return f"r{call_count}"

    results = await asyncio.gather(
        sf.share("k1", fn),
        sf.share("k2", fn),
        sf.share("k3", fn),
    )

    assert call_count == 3
    # Each call was its own leader.
    assert all(wf is False for _, wf in results)
    # Each got a distinct result.
    assert {r for r, _ in results} == {"r1", "r2", "r3"}


@pytest.mark.asyncio
async def test_leader_failure_propagates_to_followers() -> None:
    """If the leader's fn raises, every follower sees the same exception."""
    sf = SingleflightRegistry[Any]()
    call_count = 0
    leader_ready = asyncio.Event()
    release = asyncio.Event()

    class _MyError(Exception):
        pass

    async def fn() -> Any:
        nonlocal call_count
        call_count += 1
        leader_ready.set()
        await release.wait()
        raise _MyError("upstream blew up")

    leader_task = asyncio.create_task(sf.share("k", fn))
    await leader_ready.wait()

    follower_tasks = [asyncio.create_task(sf.share("k", fn)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()

    # Both leader and followers must raise _MyError.
    with pytest.raises(_MyError):
        await leader_task
    for ft in follower_tasks:
        with pytest.raises(_MyError):
            await ft

    # fn was called exactly once (the leader).
    assert call_count == 1
    # Registry cleaned up.
    assert sf.in_flight_count() == 0


@pytest.mark.asyncio
async def test_sequential_calls_each_become_leader() -> None:
    """After the leader completes, the next caller starts fresh."""
    sf = SingleflightRegistry[str]()
    call_count = 0

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        return f"r{call_count}"

    r1, wf1 = await sf.share("k", fn)
    r2, wf2 = await sf.share("k", fn)
    r3, wf3 = await sf.share("k", fn)

    # All three independent invocations.
    assert call_count == 3
    assert r1 == "r1" and wf1 is False
    assert r2 == "r2" and wf2 is False
    assert r3 == "r3" and wf3 is False


@pytest.mark.asyncio
async def test_after_leader_failure_next_caller_retries() -> None:
    """After a leader's fn raised, the next caller for the same key
    becomes a fresh leader (not a follower of the dead future)."""
    sf = SingleflightRegistry[str]()
    call_count = 0

    async def fn_raise() -> str:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    async def fn_succeed() -> str:
        nonlocal call_count
        call_count += 1
        return "recovered"

    with pytest.raises(RuntimeError):
        await sf.share("k", fn_raise)

    # Next caller (after the leader's error propagated) takes a fresh slot.
    result, was_follower = await sf.share("k", fn_succeed)
    assert result == "recovered"
    assert was_follower is False
    assert call_count == 2


@pytest.mark.asyncio
async def test_singleflight_returns_complex_dict() -> None:
    """Real-world payload: a dict shared via the future. Followers
    must get the exact same dict (identity may or may not match — we
    only assert value equality)."""
    sf = SingleflightRegistry[dict[str, Any]]()

    async def fn() -> dict[str, Any]:
        return {"response": "ok", "tokens": 42, "vectors": [0.1, 0.2]}

    leader_ready = asyncio.Event()
    release = asyncio.Event()

    async def gated_fn() -> dict[str, Any]:
        leader_ready.set()
        await release.wait()
        return await fn()

    leader_task = asyncio.create_task(sf.share("k", gated_fn))
    await leader_ready.wait()
    follower_tasks = [asyncio.create_task(sf.share("k", gated_fn)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()

    leader_result, _ = await leader_task
    for ft in follower_tasks:
        follower_result, was_follower = await ft
        assert was_follower is True
        assert follower_result == leader_result


@pytest.mark.asyncio
async def test_in_flight_count_reflects_pending_leaders() -> None:
    """Diagnostic counter."""
    sf = SingleflightRegistry[str]()
    leader_ready = asyncio.Event()
    release = asyncio.Event()

    async def fn() -> str:
        leader_ready.set()
        await release.wait()
        return "done"

    assert sf.in_flight_count() == 0
    task = asyncio.create_task(sf.share("k1", fn))
    await leader_ready.wait()
    assert sf.in_flight_count() == 1
    release.set()
    await task
    assert sf.in_flight_count() == 0
