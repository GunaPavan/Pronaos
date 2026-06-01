"""Unit tests for the Phase 40 quality-monitor core.

Three surfaces under test:

1. **Pure helpers** — ``is_degraded`` and ``degraded_models`` are simple
   dict lookups but the contracts matter (None safety, malformed
   entries, sorted output).
2. **``record_sample``** — Redis-free DB round trip: write a row,
   verify it lands in quality_samples with score clipped to [0, 1].
3. **``check_degradation``** — the t-test driven state machine.
   Three cases: baseline + healthy recent → no_change; baseline +
   significantly worse recent → detected; previously-degraded +
   recovered recent → recovered. Plus the min-sample guard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.core.quality_monitor import (
    DEFAULT_MIN_RECENT_SAMPLES,
    TransitionKind,
    check_degradation,
    degraded_models,
    is_degraded,
    record_sample,
)
from pronaos.db.models import Base, QualitySample, Team, Tenant

# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


class TestIsDegraded:
    def test_none_state_is_not_degraded(self) -> None:
        assert is_degraded(None, "groq/llama-3.1-8b-instant") is False

    def test_empty_state_is_not_degraded(self) -> None:
        assert is_degraded({}, "groq/llama-3.1-8b-instant") is False

    def test_missing_entry_is_not_degraded(self) -> None:
        state = {"other/model": {"degraded": True}}
        assert is_degraded(state, "groq/llama-3.1-8b-instant") is False

    def test_degraded_false_is_not_degraded(self) -> None:
        state = {"groq/llama-3.1-8b-instant": {"degraded": False}}
        assert is_degraded(state, "groq/llama-3.1-8b-instant") is False

    def test_degraded_true_is_degraded(self) -> None:
        state = {"groq/llama-3.1-8b-instant": {"degraded": True}}
        assert is_degraded(state, "groq/llama-3.1-8b-instant") is True

    def test_malformed_entry_is_not_degraded(self) -> None:
        # Defensive: a non-dict entry shouldn't crash the lookup.
        state = {"groq/llama-3.1-8b-instant": "garbage"}
        assert is_degraded(state, "groq/llama-3.1-8b-instant") is False  # type: ignore[arg-type]


class TestDegradedModels:
    def test_none_returns_empty(self) -> None:
        assert degraded_models(None) == []

    def test_only_degraded_models_returned(self) -> None:
        state = {
            "a/m1": {"degraded": True},
            "a/m2": {"degraded": False},
            "b/m3": {"degraded": True},
        }
        assert degraded_models(state) == ["a/m1", "b/m3"]

    def test_output_is_sorted(self) -> None:
        state = {
            "z/m": {"degraded": True},
            "a/m": {"degraded": True},
            "m/m": {"degraded": True},
        }
        assert degraded_models(state) == ["a/m", "m/m", "z/m"]


# --------------------------------------------------------------------------- #
# DB-backed: record_sample + check_degradation                                #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def sessionmaker_() -> AsyncIterator[async_sessionmaker]:
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
    quality_scores: dict[str, dict[str, object]] | None,
    degradation_state: dict[str, object] | None = None,
) -> tuple[str, str]:
    async with sm() as session:
        tenant = Tenant(name="acme-q")
        session.add(tenant)
        await session.flush()
        team = Team(
            tenant_id=tenant.id,
            name="eng",
            quality_scores=quality_scores,
            model_degradation_state=degradation_state,
            period_resets_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        session.add(team)
        await session.commit()
        return tenant.id, team.id


async def _seed_samples(
    sm: async_sessionmaker,
    *,
    tenant_id: str,
    team_id: str,
    model: str,
    scores: list[float],
) -> None:
    async with sm() as session:
        for s in scores:
            session.add(
                QualitySample(
                    tenant_id=tenant_id,
                    team_id=team_id,
                    model=model,
                    score=s,
                    judge_model="openai/gpt-4o-mini",
                )
            )
        await session.commit()


class TestRecordSample:
    @pytest.mark.asyncio
    async def test_basic_write(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        tenant_id, team_id = await _seed_team(sessionmaker_, quality_scores=None)
        async with sessionmaker_() as session:
            row = await record_sample(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                model="groq/llama-3.1-8b-instant",
                score=0.85,
                judge_model="openai/gpt-4o-mini",
            )
            await session.commit()
            assert row is not None
        # Verify it landed.
        async with sessionmaker_() as session:
            rows = (
                (
                    await session.execute(
                        select(QualitySample).where(QualitySample.team_id == team_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].score == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_score_clipped_to_unit_interval(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        tenant_id, team_id = await _seed_team(sessionmaker_, quality_scores=None)
        async with sessionmaker_() as session:
            await record_sample(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                model="m",
                score=1.5,  # judge mis-fired
                judge_model="j",
            )
            await record_sample(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                model="m",
                score=-0.2,
                judge_model="j",
            )
            await session.commit()
        async with sessionmaker_() as session:
            rows = (
                await session.execute(
                    select(QualitySample.score).where(QualitySample.team_id == team_id)
                )
            ).all()
            values = sorted(float(r[0]) for r in rows)
            assert values[0] == pytest.approx(0.0)
            assert values[1] == pytest.approx(1.0)


class TestCheckDegradation:
    @pytest.mark.asyncio
    async def test_no_baseline_returns_none(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        _tenant_id, team_id = await _seed_team(sessionmaker_, quality_scores=None)
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            assert result is None

    @pytest.mark.asyncio
    async def test_too_few_recent_samples_no_change(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        tenant_id, team_id = await _seed_team(
            sessionmaker_,
            quality_scores={"m": {"score": 0.9}},
        )
        # Seed fewer than the min_recent threshold.
        await _seed_samples(
            sessionmaker_,
            tenant_id=tenant_id,
            team_id=team_id,
            model="m",
            scores=[0.5, 0.4, 0.6],
        )
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            assert result is not None
            assert result.transition == TransitionKind.NO_CHANGE
            assert result.n_recent == 3
            assert result.p_value is None

    @pytest.mark.asyncio
    async def test_degradation_detected(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        tenant_id, team_id = await _seed_team(
            sessionmaker_,
            quality_scores={
                "m": {
                    "score": 0.9,
                    "samples": [0.88, 0.92, 0.91, 0.89, 0.93, 0.87, 0.90, 0.94, 0.86, 0.91],
                }
            },
        )
        # Seed enough recent samples that are SIGNIFICANTLY worse.
        await _seed_samples(
            sessionmaker_,
            tenant_id=tenant_id,
            team_id=team_id,
            model="m",
            scores=[0.4, 0.3, 0.5, 0.4, 0.45, 0.35, 0.42, 0.38, 0.41, 0.39, 0.36],
        )
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            await session.commit()
            assert result is not None
            assert result.transition == TransitionKind.DETECTED
            assert result.p_value is not None
            assert result.p_value < 0.05
            assert result.recent_mean < result.baseline_mean
        # State persisted.
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            assert team.model_degradation_state is not None
            entry = team.model_degradation_state["m"]
            assert entry["degraded"] is True

    @pytest.mark.asyncio
    async def test_already_healthy_recent_no_change(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        """Recent samples close to baseline → no degradation flagged."""
        tenant_id, team_id = await _seed_team(
            sessionmaker_,
            quality_scores={
                "m": {
                    "score": 0.9,
                    "samples": [0.88, 0.92, 0.91, 0.89, 0.93, 0.87, 0.90, 0.94, 0.86, 0.91],
                }
            },
        )
        await _seed_samples(
            sessionmaker_,
            tenant_id=tenant_id,
            team_id=team_id,
            model="m",
            scores=[0.89, 0.91, 0.90, 0.88, 0.92, 0.93, 0.87, 0.90, 0.91, 0.89, 0.92],
        )
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            assert result is not None
            assert result.transition == TransitionKind.NO_CHANGE

    @pytest.mark.asyncio
    async def test_recovery_detected(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        """Previously degraded → recent samples back to baseline → recovered."""
        tenant_id, team_id = await _seed_team(
            sessionmaker_,
            quality_scores={
                "m": {
                    "score": 0.9,
                    "samples": [0.88, 0.92, 0.91, 0.89, 0.93, 0.87, 0.90, 0.94, 0.86, 0.91],
                }
            },
            degradation_state={
                "m": {
                    "degraded": True,
                    "since_ts": "2026-05-20T00:00:00Z",
                    "baseline_mean": 0.9,
                    "recent_mean": 0.4,
                    "n_recent": 11,
                    "p_value": 0.001,
                }
            },
        )
        # Recent samples back at baseline.
        await _seed_samples(
            sessionmaker_,
            tenant_id=tenant_id,
            team_id=team_id,
            model="m",
            scores=[0.91, 0.89, 0.92, 0.88, 0.90, 0.93, 0.87, 0.91, 0.92, 0.89, 0.90],
        )
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            await session.commit()
            assert result is not None
            assert result.transition == TransitionKind.RECOVERED
        async with sessionmaker_() as session:
            team = await session.get(Team, team_id)
            assert team is not None
            entry = (team.model_degradation_state or {})["m"]
            assert entry["degraded"] is False

    @pytest.mark.asyncio
    async def test_min_recent_default_respected(self, sessionmaker_) -> None:  # type: ignore[no-untyped-def]
        """Confirm the default min_recent threshold guards small samples."""
        tenant_id, team_id = await _seed_team(sessionmaker_, quality_scores={"m": {"score": 0.9}})
        # Seed exactly min_recent - 1 samples.
        await _seed_samples(
            sessionmaker_,
            tenant_id=tenant_id,
            team_id=team_id,
            model="m",
            scores=[0.1] * (DEFAULT_MIN_RECENT_SAMPLES - 1),
        )
        async with sessionmaker_() as session:
            result = await check_degradation(session, team_id=team_id, model="m")
            assert result is not None
            assert result.transition == TransitionKind.NO_CHANGE
