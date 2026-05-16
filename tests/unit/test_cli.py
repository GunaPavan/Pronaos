"""CLI tests using Typer's CliRunner.

Each test spins a fresh SQLite file, runs the schema migration via
``Base.metadata.create_all``, then invokes the CLI commands against it.
Covers the full operator path: tenant → team → key issuance, plus the
Phase-4 quota commands.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from pronaos.cli import app as cli_app
from pronaos.db.models import ApiKey, Base, Team, Tenant

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    # In Click 8.2+, stdout/stderr are separated by default; the
    # ``mix_stderr`` kwarg was removed. ``result.stderr`` returns stderr.
    return CliRunner()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh SQLite DB per test, pre-seeded with schema. Settings cache is
    cleared so each test reads the fresh PRONAOS_DATABASE_URL."""
    db = tmp_path / f"cli_{os.getpid()}.db"
    monkeypatch.setenv("PRONAOS_DATABASE_URL", f"sqlite+aiosqlite:///{db.as_posix()}")

    from pronaos.config import get_settings

    get_settings.cache_clear()

    async def _create_schema() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_schema())
    return db


async def _seed_tenant_team(db: Path, *, team_name: str = "eng") -> tuple[str, str]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name=team_name)
        session.add(team)
        await session.commit()
        ids = (tenant.id, team.id)
    await engine.dispose()
    return ids


async def _seed_api_key(db: Path, team_id: str) -> str:
    from pronaos.auth.api_keys import generate_api_key, hash_key

    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    full, prefix = generate_api_key("test")
    async with sm() as session:
        key = ApiKey(
            team_id=team_id,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write",
            label="t",
        )
        session.add(key)
        await session.commit()
        kid = key.id
    await engine.dispose()
    return kid


async def _fetch_team(db: Path, team_id: str) -> Team:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        team = await session.get(Team, team_id)
        assert team is not None
    await engine.dispose()
    return team


async def _fetch_key(db: Path, key_id: str) -> ApiKey:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        key = await session.get(ApiKey, key_id)
        assert key is not None
    await engine.dispose()
    return key


# --------------------------------------------------------------------------- #
# tenant + team + key CRUD                                                    #
# --------------------------------------------------------------------------- #


class TestTenantCommands:
    def test_create_then_list(self, runner: CliRunner, db_path: Path) -> None:
        r = runner.invoke(cli_app, ["tenant", "create", "acme-cli"])
        assert r.exit_code == 0, r.output
        assert "acme-cli" in r.stdout

        r = runner.invoke(cli_app, ["tenant", "list"])
        assert r.exit_code == 0
        assert "acme-cli" in r.stdout


class TestTeamCommands:
    def test_create_and_list(self, runner: CliRunner, db_path: Path) -> None:
        tenant_id, _ = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(cli_app, ["team", "create", "marketing", "--tenant", tenant_id])
        assert r.exit_code == 0, r.output
        assert "marketing" in r.stdout

        r = runner.invoke(cli_app, ["team", "list", "--tenant", tenant_id])
        assert r.exit_code == 0
        assert "marketing" in r.stdout


class TestKeyIssue:
    def test_issue_then_revoke(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(cli_app, ["key", "issue", "--team", team_id, "--label", "ci"])
        assert r.exit_code == 0, r.output
        assert "pn_live_" in r.stdout, "key issuance must show the full key once"

        # Find the issued key id from `key list` output
        r2 = runner.invoke(cli_app, ["key", "list", "--team", team_id])
        assert r2.exit_code == 0
        key_id = r2.stdout.strip().split()[0]

        r3 = runner.invoke(cli_app, ["key", "revoke", key_id])
        assert r3.exit_code == 0
        assert "revoked" in r3.stdout


# --------------------------------------------------------------------------- #
# Phase-4 quota commands                                                      #
# --------------------------------------------------------------------------- #


class TestKeySetRps:
    def test_set_rps(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        key_id = asyncio.run(_seed_api_key(db_path, team_id))

        r = runner.invoke(cli_app, ["key", "set-rps", key_id, "--rps", "15"])
        assert r.exit_code == 0, r.output
        assert "rps=15" in r.stdout

        key = asyncio.run(_fetch_key(db_path, key_id))
        assert key.rps_limit == 15

    def test_set_unlimited_clears_rps(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        key_id = asyncio.run(_seed_api_key(db_path, team_id))

        runner.invoke(cli_app, ["key", "set-rps", key_id, "--rps", "20"])
        r = runner.invoke(cli_app, ["key", "set-rps", key_id, "--unlimited"])
        assert r.exit_code == 0, r.output
        assert "rps=unlimited" in r.stdout

        key = asyncio.run(_fetch_key(db_path, key_id))
        assert key.rps_limit is None

    def test_negative_rps_rejected(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        key_id = asyncio.run(_seed_api_key(db_path, team_id))

        r = runner.invoke(cli_app, ["key", "set-rps", key_id, "--rps", "0"])
        assert r.exit_code != 0
        assert "must be > 0" in r.stderr

    def test_conflicting_flags_rejected(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        key_id = asyncio.run(_seed_api_key(db_path, team_id))

        r = runner.invoke(cli_app, ["key", "set-rps", key_id, "--rps", "10", "--unlimited"])
        assert r.exit_code != 0
        assert "exactly one of" in r.stderr

    def test_unknown_key(self, runner: CliRunner, db_path: Path) -> None:
        r = runner.invoke(cli_app, ["key", "set-rps", "ghost-id", "--rps", "5"])
        assert r.exit_code != 0
        assert "not found" in r.stderr


class TestTeamSetBudget:
    def test_set_budget(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(cli_app, ["team", "set-budget", team_id, "--tokens", "1000000"])
        assert r.exit_code == 0, r.output
        assert "budget=1,000,000" in r.stdout

        team = asyncio.run(_fetch_team(db_path, team_id))
        assert team.monthly_token_budget == 1_000_000

    def test_set_unlimited_clears_budget(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        runner.invoke(cli_app, ["team", "set-budget", team_id, "--tokens", "500"])
        r = runner.invoke(cli_app, ["team", "set-budget", team_id, "--unlimited"])
        assert r.exit_code == 0
        assert "budget=unlimited" in r.stdout

        team = asyncio.run(_fetch_team(db_path, team_id))
        assert team.monthly_token_budget is None

    def test_unknown_team(self, runner: CliRunner, db_path: Path) -> None:
        r = runner.invoke(cli_app, ["team", "set-budget", "ghost-id", "--tokens", "100"])
        assert r.exit_code != 0
        assert "not found" in r.stderr


class TestTeamUsage:
    def test_usage_shows_all_fields(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        # Set a budget so the percentage shows up
        runner.invoke(cli_app, ["team", "set-budget", team_id, "--tokens", "1000"])

        r = runner.invoke(cli_app, ["team", "usage", team_id])
        assert r.exit_code == 0, r.output
        assert "team:" in r.stdout
        assert "used:" in r.stdout
        assert "budget:  1,000" in r.stdout
        assert "resets:" in r.stdout

    def test_usage_unlimited(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(cli_app, ["team", "usage", team_id])
        assert r.exit_code == 0
        assert "budget:  unlimited" in r.stdout
