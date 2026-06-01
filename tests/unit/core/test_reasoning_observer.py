"""Tests for the Phase 57 ReasoningObserver.

Uses fakeredis to give the observer a real Redis backend without
needing a live one. Covers:

- record() + snapshot() round-trip
- ratio property computed correctly
- empty snapshot when no observations
- fail-open semantics (None redis)
- reset() wipes the team's state
- multiple fqmns per team
- multiple teams namespaced separately
- zero-completion calls are NOT recorded (prevents denominator pollution)
- reasoning_tokens=0 IS recorded (preserves accurate workload ratio)
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from pronaos.core.reasoning_observer import (
    ReasoningObserver,
    ReasoningStat,
)


@pytest.fixture
async def redis() -> FakeRedis:  # type: ignore[type-arg]
    client = FakeRedis()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest.fixture
async def observer(redis: FakeRedis) -> ReasoningObserver:  # type: ignore[type-arg]
    return ReasoningObserver(redis)


class TestReasoningStat:
    def test_ratio_normal(self) -> None:
        stat = ReasoningStat(
            fqmn="anthropic/claude-opus-4-7",
            n_samples=5,
            completion_tokens=1000,
            reasoning_tokens=500,
        )
        # 500 reasoning / 1000 completion = 0.5
        assert stat.ratio == pytest.approx(0.5)

    def test_ratio_zero_when_no_completion(self) -> None:
        stat = ReasoningStat(
            fqmn="groq/llama-3.1-8b",
            n_samples=0,
            completion_tokens=0,
            reasoning_tokens=0,
        )
        assert stat.ratio == 0.0

    def test_ratio_zero_when_no_reasoning(self) -> None:
        """A non-reasoning workload: completion_tokens > 0 but
        reasoning_tokens == 0. Ratio is exactly 0.0 — the scorer's
        ``1 + ratio`` multiplier becomes 1.0 (no penalty)."""
        stat = ReasoningStat(
            fqmn="groq/llama-3.1-8b",
            n_samples=10,
            completion_tokens=5000,
            reasoning_tokens=0,
        )
        assert stat.ratio == 0.0

    def test_ratio_can_exceed_one(self) -> None:
        """Extreme math problems: reasoning > visible output. No clamp."""
        stat = ReasoningStat(
            fqmn="anthropic/claude-opus-4-7",
            n_samples=3,
            completion_tokens=100,
            reasoning_tokens=500,
        )
        assert stat.ratio == pytest.approx(5.0)


class TestReasoningObserverRecordAndSnapshot:
    @pytest.mark.asyncio
    async def test_single_record_visible_in_snapshot(self, observer: ReasoningObserver) -> None:
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=300,
            reasoning_tokens=200,
        )
        snap = await observer.snapshot("team-1")
        assert "anthropic/claude-opus-4-7" in snap
        stat = snap["anthropic/claude-opus-4-7"]
        assert stat.n_samples == 1
        assert stat.completion_tokens == 300
        assert stat.reasoning_tokens == 200
        # 200 / 300 ≈ 0.667
        assert stat.ratio == pytest.approx(200 / 300)

    @pytest.mark.asyncio
    async def test_multiple_records_accumulate(self, observer: ReasoningObserver) -> None:
        for _ in range(5):
            await observer.record(
                team_id="team-1",
                fqmn="anthropic/claude-opus-4-7",
                completion_tokens=100,
                reasoning_tokens=40,
            )
        snap = await observer.snapshot("team-1")
        stat = snap["anthropic/claude-opus-4-7"]
        assert stat.n_samples == 5
        assert stat.completion_tokens == 500
        assert stat.reasoning_tokens == 200
        assert stat.ratio == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_zero_completion_is_not_recorded(self, observer: ReasoningObserver) -> None:
        """A call with zero billable output (auth error, content
        filtered) should NOT pollute the ratio denominator."""
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=0,
            reasoning_tokens=0,
        )
        snap = await observer.snapshot("team-1")
        assert snap == {}

    @pytest.mark.asyncio
    async def test_zero_reasoning_is_recorded(self, observer: ReasoningObserver) -> None:
        """A non-reasoning call with completion_tokens > 0 IS recorded
        — this is the signal that 'this team uses this model for plain
        chat,' which the rolling ratio needs to reflect accurately.

        Otherwise a team that runs Claude 90% for chat and 10% for
        thinking would see Claude artificially flagged as reasoning-
        heavy from the 10% of samples.
        """
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=200,
            reasoning_tokens=0,
        )
        snap = await observer.snapshot("team-1")
        stat = snap["anthropic/claude-opus-4-7"]
        assert stat.n_samples == 1
        assert stat.completion_tokens == 200
        assert stat.reasoning_tokens == 0
        assert stat.ratio == 0.0

    @pytest.mark.asyncio
    async def test_multiple_fqmns_isolated(self, observer: ReasoningObserver) -> None:
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=80,
        )
        await observer.record(
            team_id="team-1",
            fqmn="groq/llama-3.3-70b",
            completion_tokens=100,
            reasoning_tokens=0,
        )
        snap = await observer.snapshot("team-1")
        assert len(snap) == 2
        assert snap["anthropic/claude-opus-4-7"].ratio == pytest.approx(0.8)
        assert snap["groq/llama-3.3-70b"].ratio == 0.0

    @pytest.mark.asyncio
    async def test_multiple_teams_namespaced(self, observer: ReasoningObserver) -> None:
        await observer.record(
            team_id="team-A",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=50,
        )
        await observer.record(
            team_id="team-B",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=10,
        )
        snap_a = await observer.snapshot("team-A")
        snap_b = await observer.snapshot("team-B")
        # Same model, different workloads → different ratios.
        assert snap_a["anthropic/claude-opus-4-7"].ratio == pytest.approx(0.5)
        assert snap_b["anthropic/claude-opus-4-7"].ratio == pytest.approx(0.1)


class TestReasoningObserverFailOpen:
    @pytest.mark.asyncio
    async def test_none_redis_is_noop(self) -> None:
        """When Redis isn't configured, both methods are no-ops.
        The scorer then sees an empty snapshot and degrades to plain
        cheapest — that's the documented contract."""
        observer = ReasoningObserver(None)
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=50,
        )
        snap = await observer.snapshot("team-1")
        assert snap == {}

    @pytest.mark.asyncio
    async def test_empty_snapshot_for_unknown_team(self, observer: ReasoningObserver) -> None:
        snap = await observer.snapshot("team-never-observed")
        assert snap == {}


class TestReasoningObserverReset:
    @pytest.mark.asyncio
    async def test_reset_drops_team_state(self, observer: ReasoningObserver) -> None:
        await observer.record(
            team_id="team-1",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=50,
        )
        assert (await observer.snapshot("team-1")) != {}
        await observer.reset("team-1")
        assert (await observer.snapshot("team-1")) == {}

    @pytest.mark.asyncio
    async def test_reset_does_not_affect_other_teams(self, observer: ReasoningObserver) -> None:
        await observer.record(
            team_id="team-A",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=50,
        )
        await observer.record(
            team_id="team-B",
            fqmn="anthropic/claude-opus-4-7",
            completion_tokens=100,
            reasoning_tokens=10,
        )
        await observer.reset("team-A")
        assert (await observer.snapshot("team-A")) == {}
        assert (await observer.snapshot("team-B")) != {}
