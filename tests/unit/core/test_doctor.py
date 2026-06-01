"""Phase 61 — tests for core/doctor.py.

Covers
------
- Individual gate functions: pass / warn / fail / skip semantics
- ``run_doctor`` never raises even when individual gates throw
- ``DoctorReport.exit_code`` honors --strict
- ``DoctorReport.to_dict`` shape is stable for JSON consumers
- Config + DB gates against an in-memory SQLite + mocked settings
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.core.doctor import (
    DoctorGateResult,
    DoctorReport,
    Verdict,
    gate_at_least_one_active_key,
    gate_at_least_one_team,
    gate_at_least_one_tenant,
    gate_batches_worker,
    gate_core_tables,
    gate_database_url,
    gate_db_connect,
    gate_db_migrations,
    gate_mcp,
    gate_oidc,
    gate_provider_keys,
    gate_qdrant,
    gate_redis,
    gate_secret_key,
    run_doctor,
)
from pronaos.db.models import ApiKey, Base, Team, Tenant

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class FakeSettings:
    """Loose stand-in for pronaos.config.Settings — only the
    attributes the doctor reads are populated. Real Settings is
    pydantic-driven and a pain to instantiate inline."""

    secret_key: str = "x" * 64
    database_url: str = "sqlite+aiosqlite:///:memory:"
    redis_url: str | None = None
    qdrant_url: str = "http://localhost:6333"
    semantic_cache_enabled: bool = False
    mcp_enabled: bool = False
    batches_worker_enabled: bool = True
    batches_poll_interval_seconds: int = 60
    oidc_issuer: str | None = None
    # Provider settings_attr fields the catalog references.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    groq_api_key: str | None = None
    deepseek_api_key: str | None = None
    together_api_key: str | None = None
    fireworks_api_key: str | None = None
    cerebras_api_key: str | None = None
    openrouter_api_key: str | None = None
    mistral_api_key: str | None = None
    perplexity_api_key: str | None = None
    xai_api_key: str | None = None
    cohere_api_key: str | None = None
    voyage_api_key: str | None = None
    azure_openai_api_key: str | None = None
    ollama_api_key: str | None = None
    aws_access_key_id: str | None = None
    google_application_credentials: str | None = None
    bedrock_aws_region: str | None = None


@pytest_asyncio.fixture
async def db_url() -> Any:
    """A populated in-memory SQLite URL the doctor can probe.
    Migrations are simulated by stamping alembic_version with the
    latest revision found on disk."""
    from pronaos.core.doctor import _latest_migration_revision

    rev = _latest_migration_revision() or "0001"
    engine = create_async_engine("sqlite+aiosqlite:///file:memdb1?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text(f"INSERT INTO alembic_version VALUES ('{rev}')"))
    try:
        yield "sqlite+aiosqlite:///file:memdb1?mode=memory&cache=shared&uri=true"
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Verdict + report shape                                                      #
# --------------------------------------------------------------------------- #


class TestReport:
    def test_exit_code_zero_on_all_pass(self) -> None:
        r = DoctorReport(
            gates=[
                DoctorGateResult(name="a", verdict=Verdict.PASS),
                DoctorGateResult(name="b", verdict=Verdict.SKIP),
            ]
        )
        assert r.exit_code() == 0
        assert r.exit_code(strict=True) == 0

    def test_exit_code_one_on_any_fail(self) -> None:
        r = DoctorReport(
            gates=[
                DoctorGateResult(name="a", verdict=Verdict.PASS),
                DoctorGateResult(name="b", verdict=Verdict.FAIL),
            ]
        )
        assert r.exit_code() == 1

    def test_strict_promotes_warn_to_failure(self) -> None:
        r = DoctorReport(
            gates=[
                DoctorGateResult(name="a", verdict=Verdict.WARN),
            ]
        )
        assert r.exit_code() == 0
        assert r.exit_code(strict=True) == 1

    def test_to_dict_shape(self) -> None:
        r = DoctorReport(
            gates=[
                DoctorGateResult(name="a", verdict=Verdict.PASS, detail="ok"),
                DoctorGateResult(name="b", verdict=Verdict.FAIL, detail="bad"),
                DoctorGateResult(name="c", verdict=Verdict.WARN),
                DoctorGateResult(name="d", verdict=Verdict.SKIP),
            ]
        )
        d = r.to_dict()
        assert d["summary"] == {
            "pass": 1,
            "fail": 1,
            "warn": 1,
            "skip": 1,
            "total": 4,
        }
        assert len(d["gates"]) == 4
        assert d["gates"][1]["verdict"] == "FAIL"
        assert d["gates"][1]["detail"] == "bad"


# --------------------------------------------------------------------------- #
# Config gates                                                                #
# --------------------------------------------------------------------------- #


class TestConfigGates:
    @pytest.mark.asyncio
    async def test_secret_key_unset_fails(self) -> None:
        s = FakeSettings(secret_key="")
        r = await gate_secret_key(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_secret_key_short_warns(self) -> None:
        s = FakeSettings(secret_key="short")
        r = await gate_secret_key(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.WARN

    @pytest.mark.asyncio
    async def test_secret_key_long_enough_passes(self) -> None:
        s = FakeSettings(secret_key="x" * 64)
        r = await gate_secret_key(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS

    @pytest.mark.asyncio
    async def test_database_url_unset_fails(self) -> None:
        s = FakeSettings(database_url="")
        r = await gate_database_url(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_database_url_malformed_fails(self) -> None:
        s = FakeSettings(database_url="not-a-url")
        r = await gate_database_url(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_database_url_ok_passes(self) -> None:
        s = FakeSettings()
        r = await gate_database_url(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS
        assert "sqlite" in r.detail


# --------------------------------------------------------------------------- #
# DB gates                                                                    #
# --------------------------------------------------------------------------- #


class TestDbGates:
    @pytest.mark.asyncio
    async def test_db_connect_pass(self, db_url: str) -> None:
        s = FakeSettings(database_url=db_url)
        r = await gate_db_connect(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS, r.detail

    @pytest.mark.asyncio
    async def test_db_connect_bad_url_fails(self) -> None:
        s = FakeSettings(database_url="sqlite+aiosqlite:///nonexistent_path/db.sqlite")
        # SQLite is forgiving; create_engine on a bad path may still
        # succeed but the connect+SELECT 1 should fail or pass
        # depending on the OS. We use a clearly broken driver name
        # instead.
        s = FakeSettings(database_url="madeup+driver:///foo")
        r = await gate_db_connect(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_db_migrations_at_head(self, db_url: str) -> None:
        s = FakeSettings(database_url=db_url)
        r = await gate_db_migrations(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS, r.detail

    @pytest.mark.asyncio
    async def test_core_tables_all_present(self, db_url: str) -> None:
        s = FakeSettings(database_url=db_url)
        r = await gate_core_tables(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS, r.detail


# --------------------------------------------------------------------------- #
# Auth-seed gates                                                             #
# --------------------------------------------------------------------------- #


class TestAuthSeedGates:
    @pytest.mark.asyncio
    async def test_no_tenants_warns(self, db_url: str) -> None:
        s = FakeSettings(database_url=db_url)
        r = await gate_at_least_one_tenant(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.WARN

    @pytest.mark.asyncio
    async def test_with_tenant_passes(self, db_url: str) -> None:
        engine = create_async_engine(db_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as session:
            session.add(Tenant(name="acme"))
            await session.commit()
        await engine.dispose()

        s = FakeSettings(database_url=db_url)
        r = await gate_at_least_one_tenant(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS, r.detail

    @pytest.mark.asyncio
    async def test_with_team_and_active_key_passes(self, db_url: str) -> None:
        engine = create_async_engine(db_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as session:
            t = Tenant(name="acme")
            session.add(t)
            await session.flush()
            tm = Team(tenant_id=t.id, name="eng")
            session.add(tm)
            await session.flush()
            session.add(
                ApiKey(
                    team_id=tm.id,
                    prefix="pron_test",
                    key_hash="x" * 64,
                    scopes="chat:write",
                    label="active",
                )
            )
            await session.commit()
        await engine.dispose()

        s = FakeSettings(database_url=db_url)
        assert (await gate_at_least_one_team(s)).verdict == Verdict.PASS  # type: ignore[arg-type]
        assert (await gate_at_least_one_active_key(s)).verdict == Verdict.PASS  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Optional-backend gates                                                      #
# --------------------------------------------------------------------------- #


class TestOptionalGates:
    @pytest.mark.asyncio
    async def test_redis_skip_when_unset(self) -> None:
        s = FakeSettings(redis_url=None)
        r = await gate_redis(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.SKIP

    @pytest.mark.asyncio
    async def test_redis_fail_on_unreachable(self) -> None:
        s = FakeSettings(redis_url="redis://127.0.0.1:1")  # closed port
        r = await gate_redis(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_qdrant_skip_when_disabled(self) -> None:
        s = FakeSettings(semantic_cache_enabled=False)
        r = await gate_qdrant(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.SKIP

    @pytest.mark.asyncio
    async def test_mcp_skip_when_disabled(self) -> None:
        s = FakeSettings(mcp_enabled=False)
        r = await gate_mcp(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.SKIP

    @pytest.mark.asyncio
    async def test_mcp_pass_when_enabled(self) -> None:
        # The SDK + adapter both ship with the project; if MCP is
        # turned on the gate should pass.
        s = FakeSettings(mcp_enabled=True)
        r = await gate_mcp(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS

    @pytest.mark.asyncio
    async def test_oidc_skip_when_unset(self) -> None:
        s = FakeSettings(oidc_issuer=None)
        r = await gate_oidc(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.SKIP

    @pytest.mark.asyncio
    async def test_batches_worker_pass(self) -> None:
        s = FakeSettings(batches_worker_enabled=True)
        r = await gate_batches_worker(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS


class TestProviderKeysGate:
    @pytest.mark.asyncio
    async def test_no_keys_configured_fails(self) -> None:
        s = FakeSettings()  # all provider keys None by default
        r = await gate_provider_keys(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.FAIL

    @pytest.mark.asyncio
    async def test_one_key_configured_passes(self) -> None:
        s = FakeSettings(openai_api_key="sk-test")
        r = await gate_provider_keys(s)  # type: ignore[arg-type]
        assert r.verdict == Verdict.PASS
        assert "openai" in r.detail


# --------------------------------------------------------------------------- #
# Runner — never raises, aggregates correctly                                 #
# --------------------------------------------------------------------------- #


class TestRunner:
    @pytest.mark.asyncio
    async def test_run_doctor_returns_report(self, db_url: str) -> None:
        s = FakeSettings(database_url=db_url, openai_api_key="sk-test")
        report = await run_doctor(settings=s)  # type: ignore[arg-type]
        # 14 default gates run.
        assert len(report.gates) == 14
        # No gate raised.
        names = {g.name for g in report.gates}
        assert "config.secret_key" in names
        assert "db.connect" in names
        assert "providers.any_configured" in names

    @pytest.mark.asyncio
    async def test_run_doctor_swallows_gate_exceptions(self) -> None:
        """A buggy gate that raises is reported as FAIL, never
        crashes run_doctor."""
        from pronaos.core.doctor import _RegisteredGate

        async def crashing_gate(_settings: Any) -> DoctorGateResult:
            raise RuntimeError("synthetic")

        gates = [_RegisteredGate(name="crashing", fn=crashing_gate)]
        s = FakeSettings()
        report = await run_doctor(settings=s, gates=gates)  # type: ignore[arg-type]
        assert len(report.gates) == 1
        assert report.gates[0].verdict == Verdict.FAIL
        assert "synthetic" in report.gates[0].detail
        assert "RuntimeError" in report.gates[0].detail

    @pytest.mark.asyncio
    async def test_probe_providers_adds_per_provider_gates(self) -> None:
        s = FakeSettings(openai_api_key="sk-test")

        # Patch gate_provider_probe so the test doesn't hit the
        # network. We just verify the runner CALLS it for each
        # configured provider.
        from pronaos.core import doctor as doctor_mod

        called_for: list[str] = []

        async def fake_probe(_s: Any, *, provider_key: str) -> DoctorGateResult:
            called_for.append(provider_key)
            return DoctorGateResult(
                name=f"providers.probe.{provider_key}",
                verdict=Verdict.PASS,
            )

        with patch.object(doctor_mod, "gate_provider_probe", fake_probe):
            report = await run_doctor(  # type: ignore[arg-type]
                settings=s, probe_providers=True
            )
        # Only OpenAI was configured.
        assert called_for == ["openai"]
        # The probe gate appears in the report after the default 14.
        probe_gates = [g for g in report.gates if g.name.startswith("providers.probe.")]
        assert len(probe_gates) == 1
        assert probe_gates[0].verdict == Verdict.PASS
