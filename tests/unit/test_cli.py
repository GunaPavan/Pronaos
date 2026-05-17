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
        # Set both budgets so both percentage lines show.
        runner.invoke(cli_app, ["team", "set-budget", team_id, "--tokens", "1000"])
        runner.invoke(cli_app, ["team", "set-cost-budget", team_id, "--cents", "5000"])

        r = runner.invoke(cli_app, ["team", "usage", team_id])
        assert r.exit_code == 0, r.output
        assert "team:" in r.stdout
        assert "tokens used:" in r.stdout
        assert "token budget: 1,000" in r.stdout
        assert "cost used:" in r.stdout
        assert "cost budget:  $50.00" in r.stdout
        assert "resets:" in r.stdout

    def test_usage_unlimited(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(cli_app, ["team", "usage", team_id])
        assert r.exit_code == 0
        assert "token budget: unlimited" in r.stdout
        assert "cost budget:  unlimited" in r.stdout


# --------------------------------------------------------------------------- #
# Phase-5.4: team chargeback                                                  #
# --------------------------------------------------------------------------- #


async def _seed_usage_rows(
    db: Path,
    team_id: str,
    rows: list[dict],
) -> None:
    """Insert UsageRecord rows directly. Each dict is kwargs for UsageRecord."""
    from pronaos.db.models import UsageRecord

    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        for r in rows:
            session.add(UsageRecord(team_id=team_id, **r))
        await session.commit()
    await engine.dispose()


def _common_usage(tenant_id: str, key_id: str = "k-1") -> list[dict]:
    """Three rows in the current month: 2 anthropic-opus successes and 1 groq."""
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    return [
        {
            "tenant_id": tenant_id,
            "key_id": key_id,
            "provider": "anthropic",
            "model": "anthropic/claude-opus-4-7",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cost_hcents": 1500,  # $0.15
            "status": "success",
            "ts": now,
        },
        {
            "tenant_id": tenant_id,
            "key_id": key_id,
            "provider": "anthropic",
            "model": "anthropic/claude-opus-4-7",
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "cost_hcents": 3000,  # $0.30
            "status": "success",
            "ts": now,
        },
        {
            "tenant_id": tenant_id,
            "key_id": key_id,
            "provider": "groq",
            "model": "groq/llama-3.1-8b-instant",
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "cost_hcents": 50,  # $0.0050
            "status": "success",
            "ts": now,
        },
    ]


class TestTeamChargeback:
    def test_unknown_team(self, runner: CliRunner, db_path: Path) -> None:
        r = runner.invoke(cli_app, ["team", "chargeback", "ghost-id"])
        assert r.exit_code != 0
        assert "not found" in r.stderr

    def test_empty_usage_window(self, runner: CliRunner, db_path: Path) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(cli_app, ["team", "chargeback", team_id])
        assert r.exit_code == 0, r.output
        # No usage rows seeded — totals should all be zero and we should
        # see the explicit "no usage" line so an operator isn't confused
        # by a blank report.
        assert "requests: 0" in r.stdout
        assert "(no usage in window)" in r.stdout

    def test_totals_aggregate_correctly(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        tenant_id, team_id = asyncio.run(_seed_tenant_team(db_path))
        asyncio.run(_seed_usage_rows(db_path, team_id, _common_usage(tenant_id)))

        r = runner.invoke(cli_app, ["team", "chargeback", team_id])
        assert r.exit_code == 0, r.output
        assert "requests: 3" in r.stdout
        # Tokens: 100+50 + 200+100 + 50+25 = 525
        assert "525" in r.stdout
        # Cost: 1500 + 3000 + 50 = 4550 hcents = $0.4550
        assert "$0.4550" in r.stdout

    def test_group_by_model_default(self, runner: CliRunner, db_path: Path) -> None:
        tenant_id, team_id = asyncio.run(_seed_tenant_team(db_path))
        asyncio.run(_seed_usage_rows(db_path, team_id, _common_usage(tenant_id)))

        r = runner.invoke(cli_app, ["team", "chargeback", team_id])
        assert r.exit_code == 0, r.output
        # Both models should be listed, opus first (higher cost)
        assert "anthropic/claude-opus-4-7" in r.stdout
        assert "groq/llama-3.1-8b-instant" in r.stdout
        opus_pos = r.stdout.index("anthropic/claude-opus-4-7")
        groq_pos = r.stdout.index("groq/llama-3.1-8b-instant")
        assert opus_pos < groq_pos, "higher-cost group must come first"

    def test_group_by_provider(self, runner: CliRunner, db_path: Path) -> None:
        tenant_id, team_id = asyncio.run(_seed_tenant_team(db_path))
        asyncio.run(_seed_usage_rows(db_path, team_id, _common_usage(tenant_id)))

        r = runner.invoke(
            cli_app, ["team", "chargeback", team_id, "--group-by", "provider"]
        )
        assert r.exit_code == 0, r.output
        assert "by provider:" in r.stdout
        assert "anthropic" in r.stdout
        assert "groq" in r.stdout

    def test_group_by_invalid_rejected(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(
            cli_app, ["team", "chargeback", team_id, "--group-by", "potato"]
        )
        assert r.exit_code != 0
        assert "--group-by" in r.stderr

    def test_since_until_filter_window(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        """A window entirely in the past excludes the seeded "now" rows."""
        tenant_id, team_id = asyncio.run(_seed_tenant_team(db_path))
        asyncio.run(_seed_usage_rows(db_path, team_id, _common_usage(tenant_id)))

        from datetime import UTC, datetime, timedelta

        # 30 days ago → 29 days ago: a 1-day window in the past
        far_past = (datetime.now(tz=UTC) - timedelta(days=30)).isoformat()
        less_past = (datetime.now(tz=UTC) - timedelta(days=29)).isoformat()
        r = runner.invoke(
            cli_app,
            ["team", "chargeback", team_id, "--since", far_past, "--until", less_past],
        )
        assert r.exit_code == 0, r.output
        assert "requests: 0" in r.stdout

    def test_since_after_until_rejected(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(
            cli_app,
            [
                "team",
                "chargeback",
                team_id,
                "--since",
                "2026-06-01",
                "--until",
                "2026-05-01",
            ],
        )
        assert r.exit_code != 0
        assert "after --since" in r.stderr

    def test_malformed_iso_rejected(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(
            cli_app, ["team", "chargeback", team_id, "--since", "not-a-date"]
        )
        assert r.exit_code != 0
        assert "ISO" in r.stderr


# --------------------------------------------------------------------------- #
# Phase-5.5: scoped key issuance via CLI                                      #
# --------------------------------------------------------------------------- #


class TestKeyIssueScopes:
    def test_issue_admin_usage_key_stores_scope(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        """`key issue --scopes "admin:usage"` must produce a key whose DB row
        has exactly that scope. Required so operators can mint dashboard-only
        keys without giving them chat access."""
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(
            cli_app,
            ["key", "issue", "--team", team_id, "--scopes", "admin:usage", "--label", "fin"],
        )
        assert r.exit_code == 0, r.output
        assert "admin:usage" in r.stdout

        # Read back the key and confirm the scope persisted.
        r2 = runner.invoke(cli_app, ["key", "list", "--team", team_id])
        assert r2.exit_code == 0
        key_id = r2.stdout.strip().split()[0]

        key = asyncio.run(_fetch_key(db_path, key_id))
        assert key.scope_list() == ["admin:usage"]

    def test_issue_admin_usage_key_does_not_break_default(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        # Sanity guard: issuing with the default scope still works and
        # produces a chat:write key (otherwise we'd have silently broken
        # the most common operator path).
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(cli_app, ["key", "issue", "--team", team_id])
        assert r.exit_code == 0
        r2 = runner.invoke(cli_app, ["key", "list", "--team", team_id])
        key_id = r2.stdout.strip().split()[0]
        key = asyncio.run(_fetch_key(db_path, key_id))
        assert key.scope_list() == ["chat:write"]

    def test_issue_multi_scope_key(self, runner: CliRunner, db_path: Path) -> None:
        """A space-separated scopes string round-trips as a multi-element list."""
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(
            cli_app,
            [
                "key",
                "issue",
                "--team",
                team_id,
                "--scopes",
                "chat:write admin:usage",
            ],
        )
        assert r.exit_code == 0, r.output

        r2 = runner.invoke(cli_app, ["key", "list", "--team", team_id])
        key_id = r2.stdout.strip().split()[0]
        key = asyncio.run(_fetch_key(db_path, key_id))
        assert set(key.scope_list()) == {"chat:write", "admin:usage"}


# --------------------------------------------------------------------------- #
# Phase-5.7: cost-budget CLI                                                  #
# --------------------------------------------------------------------------- #


class TestTeamSetCostBudget:
    def test_set_cost_budget_stores_hcents(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        """--cents 5000 ($50.00) must persist as 500,000 hcents internally
        but display as $50.00 for humans."""
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        r = runner.invoke(
            cli_app, ["team", "set-cost-budget", team_id, "--cents", "5000"]
        )
        assert r.exit_code == 0, r.output
        assert "$50.00" in r.stdout

        team = asyncio.run(_fetch_team(db_path, team_id))
        assert team.monthly_cost_hcents_budget == 500_000

    def test_set_unlimited_clears_cost_budget(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))

        runner.invoke(cli_app, ["team", "set-cost-budget", team_id, "--cents", "5000"])
        r = runner.invoke(
            cli_app, ["team", "set-cost-budget", team_id, "--unlimited"]
        )
        assert r.exit_code == 0, r.output
        assert "unlimited" in r.stdout

        team = asyncio.run(_fetch_team(db_path, team_id))
        assert team.monthly_cost_hcents_budget is None

    def test_negative_cents_rejected(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(
            cli_app, ["team", "set-cost-budget", team_id, "--cents", "0"]
        )
        assert r.exit_code != 0
        assert "must be > 0" in r.stderr

    def test_conflicting_flags_rejected(
        self, runner: CliRunner, db_path: Path
    ) -> None:
        _, team_id = asyncio.run(_seed_tenant_team(db_path))
        r = runner.invoke(
            cli_app,
            ["team", "set-cost-budget", team_id, "--cents", "100", "--unlimited"],
        )
        assert r.exit_code != 0

    def test_unknown_team(self, runner: CliRunner, db_path: Path) -> None:
        r = runner.invoke(
            cli_app, ["team", "set-cost-budget", "ghost-id", "--cents", "100"]
        )
        assert r.exit_code != 0
        assert "not found" in r.stderr
