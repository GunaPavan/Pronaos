"""Unit tests for the Phase-4 quota schema.

Covers:
- ``next_period_reset`` helper edge cases (year-end rollover)
- New ORM column defaults populate correctly
- Migration 0002 round-trips up + down without losing data
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from pronaos.db.models import ApiKey, Base, Team, Tenant, next_period_reset

# --------------------------------------------------------------------------- #
# Helper                                                                      #
# --------------------------------------------------------------------------- #


class TestNextPeriodReset:
    def test_basic_month_increment(self) -> None:
        now = datetime(2026, 3, 15, 12, 30, 0, tzinfo=UTC)
        nxt = next_period_reset(now)
        assert nxt == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

    def test_year_end_rollover(self) -> None:
        now = datetime(2026, 12, 31, 23, 59, 0, tzinfo=UTC)
        nxt = next_period_reset(now)
        assert nxt == datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_naive_datetime_treated_as_utc(self) -> None:
        # Defensive: accept naive datetimes and treat them as UTC, never
        # silently apply server-local time.
        naive = datetime(2026, 5, 10, 9, 0, 0)
        nxt = next_period_reset(naive)
        assert nxt.tzinfo is UTC
        assert nxt == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# ORM column defaults                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_team_quota_defaults_are_unlimited(tmp_path: Path) -> None:
    """A freshly created Team must default to unlimited budget + 0 consumption
    + a future period_resets_at. Anything else means we accidentally enabled
    a quota on every new team."""
    db = tmp_path / "test_quota_defaults.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        tenant = Tenant(name="acme-qd")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.commit()
        await session.refresh(team)

        assert team.monthly_token_budget is None, "budget must default to unlimited"
        assert team.current_period_tokens == 0
        # SQLite drops tz info on read-back; normalise both sides to compare.
        period_reset = team.period_resets_at
        if period_reset.tzinfo is None:
            period_reset = period_reset.replace(tzinfo=UTC)
        assert period_reset > datetime.now(tz=UTC), "period_resets_at must be in the future"

    await engine.dispose()


@pytest.mark.asyncio
async def test_api_key_rps_limit_defaults_to_unlimited(tmp_path: Path) -> None:
    db = tmp_path / "test_rps_default.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        tenant = Tenant(name="acme-rps")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.flush()
        key = ApiKey(
            team_id=team.id,
            prefix="abc123def456",
            key_hash="$argon2id$v=19$placeholder",
        )
        session.add(key)
        await session.commit()
        await session.refresh(key)

        assert key.rps_limit is None, "rps_limit must default to unlimited"

    await engine.dispose()


# --------------------------------------------------------------------------- #
# Migration round-trip                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_migrations_up_then_down_round_trip(tmp_path: Path, monkeypatch) -> None:
    """Apply 0001 → 0002, inspect schema has new columns; then 0002 → 0001
    and confirm new columns are gone. This catches misnamed columns, bad
    server_defaults, and downgrade bugs all in one test."""
    db = tmp_path / "round_trip.db"
    db_url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("PRONAOS_DATABASE_URL", db_url)

    from pronaos.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[3] / "alembic.ini"  # noqa: ASYNC240 — one-time setup
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Upgrade to 0002 specifically (not head) so this test stays correct
    # as future migrations stack on top.
    await asyncio.to_thread(command.upgrade, cfg, "0002")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        team_cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("teams")]
        )
        key_cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("api_keys")]
        )
    await engine.dispose()

    assert "monthly_token_budget" in team_cols
    assert "current_period_tokens" in team_cols
    assert "period_resets_at" in team_cols
    assert "rps_limit" in key_cols

    # Downgrade to 0001 — Phase-4 columns must be gone.
    await asyncio.to_thread(command.downgrade, cfg, "0001")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        team_cols_after = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("teams")]
        )
        key_cols_after = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("api_keys")]
        )
    await engine.dispose()

    assert "monthly_token_budget" not in team_cols_after
    assert "current_period_tokens" not in team_cols_after
    assert "period_resets_at" not in team_cols_after
    assert "rps_limit" not in key_cols_after

    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Phase 5.7 — cost-budget columns                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_team_cost_budget_defaults_are_unlimited(tmp_path: Path) -> None:
    """A freshly created Team must default to unlimited cost budget + 0 cost
    consumption. Same fail-safe posture as the token budget."""
    db = tmp_path / "test_cost_defaults.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        tenant = Tenant(name="acme-cd")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.commit()
        await session.refresh(team)

        assert team.monthly_cost_hcents_budget is None, "cost budget must default to unlimited"
        assert team.current_period_cost_hcents == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0004_round_trip(tmp_path: Path, monkeypatch) -> None:
    """0003 → 0004 adds the cost-budget columns; 0004 → 0003 removes them.
    Catches misnamed columns, bad server_defaults, and downgrade bugs."""
    db = tmp_path / "round_trip_0004.db"
    db_url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("PRONAOS_DATABASE_URL", db_url)

    from pronaos.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[3] / "alembic.ini"  # noqa: ASYNC240
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)

    await asyncio.to_thread(command.upgrade, cfg, "0004")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        team_cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("teams")]
        )
    await engine.dispose()

    assert "monthly_cost_hcents_budget" in team_cols
    assert "current_period_cost_hcents" in team_cols

    await asyncio.to_thread(command.downgrade, cfg, "0003")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        team_cols_after = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("teams")]
        )
    await engine.dispose()

    assert "monthly_cost_hcents_budget" not in team_cols_after
    assert "current_period_cost_hcents" not in team_cols_after
    # Phase-4 columns must still be present after the 0004 downgrade
    assert "monthly_token_budget" in team_cols_after

    get_settings.cache_clear()
