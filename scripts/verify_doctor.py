"""CLI doctor mocked-live verification (Claim #48, Phase 61).

The empirical question
----------------------
``pronaos-cli doctor`` exists so operators can find broken
configuration BEFORE the first chat call exposes it. The claim
is mechanical: when the gateway is healthy, doctor exits 0;
when an operator-relevant gate fails, doctor exits 1 with the
exact failing gate name printed.

This script stands up two scenarios end-to-end against an
in-memory SQLite + isolated settings:

Scenario A — healthy gateway
----------------------------
- SECRET_KEY set, 64 chars
- DATABASE_URL valid, in-memory SQLite
- migrations stamped to latest
- core tables present
- tenant + team + active API key seeded
- one provider key (OpenAI) configured

Expected: every default gate is PASS (or SKIP for the
unconfigured optional features). Exit code 0. Final verdict:
``gateway is healthy``.

Scenario B — broken gateway (missing tenant)
--------------------------------------------
Same as A but without the tenant seed.

Expected: the ``auth.tenant_count`` gate fires WARN (no tenants
→ every future chat call will fail to resolve a Principal).
Exit code 0 without --strict, but exit 1 with --strict, AND the
specific WARN line is present in the output.

Honest disclosures
------------------
- All gates that need external services (Redis, Qdrant, OIDC,
  provider keys via probe) are intentionally SKIPped in both
  scenarios — the doctor itself is what's being verified, not
  the third-party services.
- The migration check looks at the latest revision file on
  disk; the in-memory SQLite is stamped to that same revision
  to make the gate PASS without running real alembic.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# Stamp env before importing Settings. Use a tempfile-backed SQLite
# so every engine instantiation across the doctor's gates sees the
# same persistent state — shared-memory URIs don't survive separate
# engine objects.
_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115 — leak deliberately for the run's duration
    prefix="pronaos_doctor_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_OPENAI_API_KEY"] = "sk-test"
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
# Force optional backends OFF so the doctor SKIPs them — we're
# verifying the doctor's gate logic, not whether Redis is reachable.
os.environ["PRONAOS_REDIS_URL"] = ""
os.environ["PRONAOS_SEMANTIC_CACHE_ENABLED"] = "false"
os.environ["PRONAOS_MCP_ENABLED"] = "false"
os.environ.pop("PRONAOS_OIDC_ISSUER", None)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from pronaos.config import get_settings  # noqa: E402
from pronaos.core.doctor import (  # noqa: E402
    Verdict,
    _latest_migration_revision,
    run_doctor,
)
from pronaos.db.models import ApiKey, Base, Team, Tenant  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _setup_db_with_seed(*, with_tenant: bool) -> None:
    """Stamp migrations + seed tenant/team/key (optionally)."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM teams"))
        await conn.execute(text("DELETE FROM tenants"))
        rev = _latest_migration_revision() or "0001"
        await conn.execute(text(f"INSERT INTO alembic_version VALUES ('{rev}')"))

    if with_tenant:
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
                    key_hash="h" * 64,
                    scopes="chat:write",
                    label="active",
                )
            )
            await session.commit()
    await engine.dispose()


async def main() -> int:
    print("=" * 72)
    print("Phase 61 / Claim #48 - pronaos-cli doctor verify (mocked-live)")
    print("=" * 72)
    print()

    # ------------------------------------------------------------------ #
    # Scenario A — healthy gateway                                        #
    # ------------------------------------------------------------------ #

    print(">> Scenario A: healthy gateway (tenant + team + active key seeded)")
    get_settings.cache_clear()
    await _setup_db_with_seed(with_tenant=True)
    report_a = await run_doctor()
    code_a = report_a.exit_code()

    # Print summary of gate verdicts.
    summary_a = {
        "pass": sum(1 for g in report_a.gates if g.verdict == Verdict.PASS),
        "fail": sum(1 for g in report_a.gates if g.verdict == Verdict.FAIL),
        "warn": sum(1 for g in report_a.gates if g.verdict == Verdict.WARN),
        "skip": sum(1 for g in report_a.gates if g.verdict == Verdict.SKIP),
    }
    print(
        f"  {summary_a['pass']} pass / {summary_a['fail']} fail / "
        f"{summary_a['warn']} warn / {summary_a['skip']} skip"
    )
    for g in report_a.gates:
        if g.verdict in (Verdict.FAIL, Verdict.WARN):
            print(f"    [{g.verdict.value}] {g.name}: {g.detail}")

    assert_(
        "scenario A: no FAILs",
        summary_a["fail"] == 0,
        f"got {summary_a['fail']}",
    )
    assert_(
        "scenario A: no WARNs (seeded auth state is clean)",
        summary_a["warn"] == 0,
        f"got {summary_a['warn']}",
    )
    assert_(
        "scenario A: exit code = 0",
        code_a == 0,
        f"got {code_a}",
    )
    config_secret = next(
        g for g in report_a.gates if g.name == "config.secret_key"
    )
    assert_(
        "scenario A: config.secret_key passes",
        config_secret.verdict == Verdict.PASS,
        f"got {config_secret.verdict}",
    )
    db_connect = next(g for g in report_a.gates if g.name == "db.connect")
    assert_(
        "scenario A: db.connect passes",
        db_connect.verdict == Verdict.PASS,
        f"got {db_connect.verdict}",
    )
    auth_tenant = next(g for g in report_a.gates if g.name == "auth.tenant_count")
    assert_(
        "scenario A: auth.tenant_count passes",
        auth_tenant.verdict == Verdict.PASS,
        f"got {auth_tenant.verdict}",
    )

    # ------------------------------------------------------------------ #
    # Scenario B — broken (missing tenant)                                #
    # ------------------------------------------------------------------ #

    print()
    print(">> Scenario B: broken gateway (tenant NOT seeded)")
    await _setup_db_with_seed(with_tenant=False)
    report_b = await run_doctor()
    code_b = report_b.exit_code()
    code_b_strict = report_b.exit_code(strict=True)

    summary_b = {
        "pass": sum(1 for g in report_b.gates if g.verdict == Verdict.PASS),
        "fail": sum(1 for g in report_b.gates if g.verdict == Verdict.FAIL),
        "warn": sum(1 for g in report_b.gates if g.verdict == Verdict.WARN),
        "skip": sum(1 for g in report_b.gates if g.verdict == Verdict.SKIP),
    }
    print(
        f"  {summary_b['pass']} pass / {summary_b['fail']} fail / "
        f"{summary_b['warn']} warn / {summary_b['skip']} skip"
    )
    for g in report_b.gates:
        if g.verdict in (Verdict.FAIL, Verdict.WARN):
            print(f"    [{g.verdict.value}] {g.name}: {g.detail}")

    auth_tenant_b = next(
        g for g in report_b.gates if g.name == "auth.tenant_count"
    )
    assert_(
        "scenario B: auth.tenant_count WARNs",
        auth_tenant_b.verdict == Verdict.WARN,
        f"got {auth_tenant_b.verdict}",
    )
    assert_(
        "scenario B: exit code (lenient) = 0 (no FAILs)",
        code_b == 0,
        f"got {code_b}",
    )
    assert_(
        "scenario B: exit code (strict) = 1 (WARN promotes to FAIL)",
        code_b_strict == 1,
        f"got {code_b_strict}",
    )
    # The same 14 gates run in both scenarios.
    assert_(
        "default gate count is stable across scenarios",
        len(report_a.gates) == len(report_b.gates) == 14,
        f"A={len(report_a.gates)} B={len(report_b.gates)}",
    )

    # ------------------------------------------------------------------ #
    # JSON shape                                                          #
    # ------------------------------------------------------------------ #

    print()
    print(">> Scenario A: JSON output shape")
    j = report_a.to_dict()
    assert_(
        "JSON: gates is a list",
        isinstance(j.get("gates"), list),
        f"got {type(j.get('gates')).__name__}",
    )
    assert_(
        "JSON: summary keys present",
        all(k in j["summary"] for k in ("pass", "fail", "warn", "skip", "total")),  # type: ignore[index, operator]
        "see j['summary']",
    )

    # ------------------------------------------------------------------ #
    # Summary                                                             #
    # ------------------------------------------------------------------ #

    print()
    print("=" * 72)
    failed = [(n, d) for n, ok, d in VERDICTS if not ok]
    if failed:
        print(f"VERDICT: {len(failed)}/{len(VERDICTS)} ASSERTIONS FAILED")
        for n, d in failed:
            print(f"  - {n}: {d}")
        return 1
    print(f"VERDICT: all {len(VERDICTS)} assertions held.")
    print()
    print("Claim #48 supported (mocked-live):")
    print(
        "  pronaos-cli doctor distinguishes healthy from broken"
    )
    print(
        "  gateway state across the full default gate set,"
    )
    print(
        "  surfaces the specific failing/warning gate,"
    )
    print("  and honors --strict for CI gating.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
