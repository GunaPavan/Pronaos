"""Unit tests for QuotaTracker — token-budget logic with rollover.

These run against an in-memory SQLite DB so we test the real SQL paths
(atomic increment, etc.) without external infra.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.core.quota import QuotaResult, QuotaTracker
from pronaos.db.models import Base, Team, Tenant

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def sessionmaker_() -> AsyncIterator[async_sessionmaker]:
    """Fresh in-memory SQLite per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed_team(
    sm: async_sessionmaker,
    *,
    budget: int | None,
    used: int = 0,
    period_resets_at: datetime | None = None,
) -> str:
    period_resets_at = period_resets_at or datetime(2099, 1, 1, tzinfo=UTC)
    async with sm() as session:
        tenant = Tenant(name=f"t-{used}-{budget}")
        session.add(tenant)
        await session.flush()
        team = Team(
            tenant_id=tenant.id,
            name="eng",
            monthly_token_budget=budget,
            current_period_tokens=used,
            period_resets_at=period_resets_at,
        )
        session.add(team)
        await session.commit()
        return team.id


# --------------------------------------------------------------------------- #
# check_budget                                                                #
# --------------------------------------------------------------------------- #


class TestCheckBudget:
    @pytest.mark.asyncio
    async def test_unlimited_budget_always_allows(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=None, used=10_000_000)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
        assert r.allowed
        assert r.tokens_remaining is None

    @pytest.mark.asyncio
    async def test_under_budget_allows(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=100, used=42)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
        assert r.allowed
        assert r.tokens_remaining == 58

    @pytest.mark.asyncio
    async def test_at_budget_denies(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=100, used=100)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
        assert not r.allowed
        # Phase 5.7 split this into per-budget reason codes.
        assert r.reason == "monthly_token_budget_exhausted"
        assert r.tokens_remaining == 0

    @pytest.mark.asyncio
    async def test_over_budget_denies(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=100, used=150)
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_unknown_team_fails_closed(self, sessionmaker_: async_sessionmaker) -> None:
        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, "ghost-team-id")
        assert not r.allowed
        assert r.reason == "team_not_found"


# --------------------------------------------------------------------------- #
# Lazy rollover                                                               #
# --------------------------------------------------------------------------- #


class TestRollover:
    @pytest.mark.asyncio
    async def test_rollover_resets_counter_when_period_passes(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        # Team's period ended yesterday and has 99/100 consumed.
        past = datetime.now(tz=UTC) - timedelta(days=1)
        team_id = await _seed_team(sessionmaker_, budget=100, used=99, period_resets_at=past)

        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
            await session.commit()
        assert r.allowed, "rollover must reset the counter so the team can be served"
        assert r.tokens_remaining == 100  # fresh period

        # Confirm DB now has the new reset timestamp + zeroed counter.
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.current_period_tokens == 0
            resets_at = team.period_resets_at
            if resets_at.tzinfo is None:
                resets_at = resets_at.replace(tzinfo=UTC)
            assert resets_at > datetime.now(tz=UTC)

    @pytest.mark.asyncio
    async def test_no_rollover_inside_period(self, sessionmaker_: async_sessionmaker) -> None:
        future = datetime.now(tz=UTC) + timedelta(days=5)
        team_id = await _seed_team(sessionmaker_, budget=100, used=99, period_resets_at=future)

        tracker = QuotaTracker()
        async with sessionmaker_() as session:
            r = await tracker.check_budget(session, team_id)
        # Same period — used stays at 99, NOT reset to 0.
        assert r.allowed
        assert r.tokens_remaining == 1


# --------------------------------------------------------------------------- #
# record_usage                                                                #
# --------------------------------------------------------------------------- #


class TestRecordUsage:
    @pytest.mark.asyncio
    async def test_increments_counter(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=1000, used=10)
        tracker = QuotaTracker()

        async with sessionmaker_() as session:
            await tracker.record_usage(session, team_id, tokens=25)
            await session.commit()

        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.current_period_tokens == 35

    @pytest.mark.asyncio
    async def test_negative_or_zero_tokens_is_noop(self, sessionmaker_: async_sessionmaker) -> None:
        team_id = await _seed_team(sessionmaker_, budget=1000, used=10)
        tracker = QuotaTracker()

        async with sessionmaker_() as session:
            await tracker.record_usage(session, team_id, tokens=0)
            await tracker.record_usage(session, team_id, tokens=-5)
            await session.commit()

        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.current_period_tokens == 10  # unchanged

    @pytest.mark.asyncio
    async def test_concurrent_increments_are_atomic(
        self, sessionmaker_: async_sessionmaker
    ) -> None:
        """50 parallel record_usage calls of 1 token each → final = +50.
        Proves the SQL increment is atomic (no SELECT-then-UPDATE race)."""
        team_id = await _seed_team(sessionmaker_, budget=1000, used=0)
        tracker = QuotaTracker()

        async def increment_once() -> None:
            async with sessionmaker_() as session:
                await tracker.record_usage(session, team_id, tokens=1)
                await session.commit()

        await asyncio.gather(*[increment_once() for _ in range(50)])

        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.current_period_tokens == 50

    @pytest.mark.asyncio
    async def test_record_usage_failure_is_swallowed(
        self, sessionmaker_: async_sessionmaker, monkeypatch
    ) -> None:
        """If the UPDATE fails, record_usage must not raise — the response
        already shipped and we'd rather log than 5xx the client."""
        team_id = await _seed_team(sessionmaker_, budget=1000, used=0)
        tracker = QuotaTracker()

        async with sessionmaker_() as session:
            # Monkey-patch session.execute to blow up
            async def boom(*a, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("simulated DB hiccup")

            monkeypatch.setattr(session, "execute", boom)
            # Must not raise
            await tracker.record_usage(session, team_id, tokens=5)


# --------------------------------------------------------------------------- #
# Result helpers                                                              #
# --------------------------------------------------------------------------- #


class TestQuotaResult:
    def test_allow_unlimited(self) -> None:
        r = QuotaResult.allow_unlimited()
        assert r.allowed
        assert r.tokens_remaining is None

    def test_allow_bounded(self) -> None:
        r = QuotaResult.allow_bounded(remaining=42)
        assert r.allowed
        assert r.tokens_remaining == 42

    def test_deny_exhausted(self) -> None:
        # ``deny_exhausted`` is kept as a back-compat alias for the
        # token-exhausted variant after the Phase 5.7 reason-code split.
        r = QuotaResult.deny_exhausted(retry_after_seconds=120.0)
        assert not r.allowed
        assert r.reason == "monthly_token_budget_exhausted"
        assert r.retry_after_seconds == 120.0

    def test_deny_cost_exhausted(self) -> None:
        r = QuotaResult.deny_cost_exhausted(retry_after_seconds=60.0)
        assert not r.allowed
        assert r.reason == "monthly_cost_budget_exhausted"
        assert r.retry_after_seconds == 60.0
