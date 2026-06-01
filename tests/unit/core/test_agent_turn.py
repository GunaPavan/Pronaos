"""AgentTurnTracker unit tests (Phase 30).

Each test uses a fakeredis backend so the assertions touch real Redis
hash semantics — same code path as production. ``AgentTurnTracker(None)``
exercises the no-Redis fail-open path separately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest

from pronaos.core.agent_turn import AgentTurnTracker


@pytest.fixture
async def tracker() -> AsyncIterator[AgentTurnTracker]:
    r = fakeredis.aioredis.FakeRedis()
    yield AgentTurnTracker(r)
    await r.aclose()


@pytest.fixture
def no_redis() -> AgentTurnTracker:
    """Tracker without a Redis backend → fail-open everything."""
    return AgentTurnTracker(None)


# --------------------------------------------------------------------------- #
# No-Redis / no-budget / no-turn-id: gate is a no-op                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_redis_always_allows(no_redis: AgentTurnTracker) -> None:
    """When the tracker has no Redis client, every check is allowed
    (fail-open). Matches the gateway's posture when REDIS_URL is unset."""
    decision = await no_redis.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=10,
        budget_cost_hcents=10,
        next_estimate_tokens=100,
        next_estimate_cost_hcents=100,
    )
    assert decision.allowed


@pytest.mark.asyncio
async def test_no_turn_id_always_allows(tracker: AgentTurnTracker) -> None:
    """No turn-id header → client isn't opting into the feature →
    every call is allowed regardless of team budget."""
    decision = await tracker.check(
        team_id="t1",
        turn_id="",
        budget_tokens=10,
        budget_cost_hcents=10,
    )
    assert decision.allowed


@pytest.mark.asyncio
async def test_both_budgets_null_always_allows(tracker: AgentTurnTracker) -> None:
    """Team has no budgets set → gate is a no-op even with turn-id."""
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=None,
        budget_cost_hcents=None,
    )
    assert decision.allowed


# --------------------------------------------------------------------------- #
# Token-budget enforcement                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_budget_fresh_turn_allows(tracker: AgentTurnTracker) -> None:
    """First call under a fresh turn-id with budget room → allowed."""
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=100,
    )
    assert decision.allowed
    assert decision.remaining_tokens == 1000  # nothing used yet
    assert decision.used_tokens == 0


@pytest.mark.asyncio
async def test_token_budget_accumulates_across_calls(tracker: AgentTurnTracker) -> None:
    """After recording usage, subsequent checks see the running total."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=300, cost_hcents=0)
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=400, cost_hcents=0)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=100,
    )
    assert decision.allowed
    assert decision.used_tokens == 700
    assert decision.remaining_tokens == 300
    assert decision.used_calls == 2


@pytest.mark.asyncio
async def test_token_budget_denies_when_estimate_exceeds(
    tracker: AgentTurnTracker,
) -> None:
    """Used 800/1000, estimate is 300 → would land at 1100 → denied."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=800, cost_hcents=0)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=300,
    )
    assert not decision.allowed
    assert decision.reason == "agent_turn_token_budget_exhausted"
    assert decision.remaining_tokens == 200


@pytest.mark.asyncio
async def test_token_budget_at_exact_limit_allows(tracker: AgentTurnTracker) -> None:
    """Used 800/1000, estimate 200 → would land at exactly 1000 →
    allowed (we deny on STRICTLY > budget)."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=800, cost_hcents=0)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=200,
    )
    assert decision.allowed


# --------------------------------------------------------------------------- #
# Cost-budget enforcement                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cost_budget_independent_of_tokens(tracker: AgentTurnTracker) -> None:
    """Cost gate fires even when token budget would still be fine."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=10, cost_hcents=45)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=1000,  # plenty of token room
        budget_cost_hcents=50,
        next_estimate_tokens=10,
        next_estimate_cost_hcents=10,  # would land at 55 > 50
    )
    assert not decision.allowed
    assert decision.reason == "agent_turn_cost_budget_exhausted"
    assert decision.remaining_cost_hcents == 5


# --------------------------------------------------------------------------- #
# Turn isolation                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_separate_turn_ids_have_separate_budgets(
    tracker: AgentTurnTracker,
) -> None:
    """Burning turn-1's budget doesn't affect turn-2."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=2000, cost_hcents=0)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-2",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=500,
    )
    assert decision.allowed
    assert decision.used_tokens == 0


@pytest.mark.asyncio
async def test_separate_teams_have_separate_budgets(
    tracker: AgentTurnTracker,
) -> None:
    """Same turn-id under different team_ids → no cross-tenant leak."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=2000, cost_hcents=0)
    decision = await tracker.check(
        team_id="t2",
        turn_id="turn-1",
        budget_tokens=1000,
        budget_cost_hcents=None,
        next_estimate_tokens=500,
    )
    assert decision.allowed


# --------------------------------------------------------------------------- #
# Record semantics                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_record_increments_call_counter(tracker: AgentTurnTracker) -> None:
    """Each ``record`` bumps the calls counter by 1."""
    for _ in range(5):
        await tracker.record(team_id="t1", turn_id="turn-1", tokens=10, cost_hcents=1)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=10000,
        budget_cost_hcents=10000,
    )
    assert decision.used_calls == 5


@pytest.mark.asyncio
async def test_record_with_zero_values_is_noop(tracker: AgentTurnTracker) -> None:
    """A record of (0, 0) doesn't change the counters."""
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=100, cost_hcents=10)
    await tracker.record(team_id="t1", turn_id="turn-1", tokens=0, cost_hcents=0)
    decision = await tracker.check(
        team_id="t1",
        turn_id="turn-1",
        budget_tokens=10000,
        budget_cost_hcents=10000,
    )
    assert decision.used_tokens == 100
    assert decision.used_cost_hcents == 10
    assert decision.used_calls == 1  # only the non-zero record counted
