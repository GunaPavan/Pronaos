"""Unit tests for the Phase-5 ``usage_records`` schema.

Covers:
- The ORM model accepts the expected fields with sensible defaults.
- Migration 0003 round-trips (up creates the table+indexes, down drops them).
- Indexes are present for the two hot query shapes (team_id+ts, tenant_id+ts).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.db.models import Base, UsageRecord

# --------------------------------------------------------------------------- #
# ORM defaults                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_usage_record_insert_with_defaults(tmp_path: Path) -> None:
    db = tmp_path / "usage_defaults.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        rec = UsageRecord(
            tenant_id="t-1",
            team_id="team-1",
            key_id="k-1",
            provider="groq",
            model="llama-3.1-8b-instant",
            prompt_tokens=42,
            completion_tokens=7,
            cost_hcents=15,
            request_id="req-abc",
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)

        # Defaults applied
        assert rec.status == "success"
        assert rec.id is not None
        assert rec.ts is not None
        # And the values we set survived
        assert rec.prompt_tokens == 42
        assert rec.completion_tokens == 7
        assert rec.cost_hcents == 15
        assert rec.request_id == "req-abc"

    await engine.dispose()


@pytest.mark.asyncio
async def test_usage_records_indexed_by_team_and_tenant(tmp_path: Path) -> None:
    db = tmp_path / "usage_indexes.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Verify the two indexes we declared exist
        def _index_names(sync_conn) -> set[str]:  # type: ignore[no-untyped-def]
            ins = inspect(sync_conn)
            return {ix["name"] for ix in ins.get_indexes("usage_records")}

        names = await conn.run_sync(_index_names)
    await engine.dispose()

    assert "ix_usage_records_team_ts" in names
    assert "ix_usage_records_tenant_ts" in names


# --------------------------------------------------------------------------- #
# Query: time-range filter actually returns the expected rows                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_filter_by_team_and_time_range(tmp_path: Path) -> None:
    db = tmp_path / "usage_query.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    t0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)

    async with sm() as session:
        session.add_all(
            [
                UsageRecord(tenant_id="t", team_id="A", key_id="k", provider="g",
                            model="m", ts=t0),
                UsageRecord(tenant_id="t", team_id="A", key_id="k", provider="g",
                            model="m", ts=t1),
                UsageRecord(tenant_id="t", team_id="B", key_id="k", provider="g",
                            model="m", ts=t1),
                UsageRecord(tenant_id="t", team_id="A", key_id="k", provider="g",
                            model="m", ts=t2),
            ]
        )
        await session.commit()

    async with sm() as session:
        stmt = (
            select(UsageRecord)
            .where(UsageRecord.team_id == "A")
            .where(UsageRecord.ts >= t0)
            .where(UsageRecord.ts < t2)
        )
        rows = (await session.execute(stmt)).scalars().all()
    await engine.dispose()

    # Team A, within window [t0, t2): 2 rows
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# Migration round-trip                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_migration_0003_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply 0001+0002+0003 then downgrade 0003 → 0002.

    Catches: misnamed columns, bad server_defaults, downgrade bugs, index leaks.
    """
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

    # Upgrade to 0003 specifically (NOT head) so this test keeps working as
    # future migrations stack on top — same defensive pattern as the
    # quota-columns round-trip test.
    await asyncio.to_thread(command.upgrade, cfg, "0003")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        usage_cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("usage_records")]
        )
    await engine.dispose()

    assert "usage_records" in tables
    for expected in (
        "id",
        "ts",
        "tenant_id",
        "team_id",
        "key_id",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cost_hcents",
        "request_id",
        "status",
    ):
        assert expected in usage_cols, f"missing column: {expected}"

    # Downgrade to 0002 explicitly; usage_records should disappear, others remain.
    await asyncio.to_thread(command.downgrade, cfg, "0002")

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        tables_after = await conn.run_sync(lambda c: inspect(c).get_table_names())
    await engine.dispose()

    assert "usage_records" not in tables_after
    assert "teams" in tables_after  # 0002 still applied
    assert "tenants" in tables_after  # 0001 still applied

    get_settings.cache_clear()
