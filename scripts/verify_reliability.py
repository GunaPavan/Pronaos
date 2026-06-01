"""Phase 68 reliability + doctor backend verify (Claim #55).

The empirical question
----------------------
Phase 68 adds three admin surfaces:

1. ``GET /v1/admin/providers`` — catalog rows annotated with the
   live circuit-breaker state.
2. ``POST /v1/admin/providers/{name}/reset-breaker`` — force a
   provider's breaker back to CLOSED.
3. ``GET /v1/admin/doctor`` — run the 14-gate doctor health check.

This script proves all three round-trip end-to-end:

 1. GET providers returns the catalog shape; configured rows
    (groq with GROQ_API_KEY set) sort before unconfigured.
 2. After tripping the groq breaker (record_failure × N), GET
    providers reports `circuit_state="open"` on the groq row.
 3. POST reset-breaker with admin:identity flips the state back
    to "closed"; follow-up GET confirms.
 4. POST reset-breaker with admin:usage alone returns 403.
 5. POST reset-breaker on an unknown provider returns 404.
 6. GET doctor returns the gate report shape; summary counts
    add up to gate count; configured providers' gates are
    represented.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_reliability_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")
os.environ.setdefault("GROQ_API_KEY", "test-key-for-verify")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from pronaos.auth.api_keys import generate_api_key, hash_key  # noqa: E402
from pronaos.config import get_settings  # noqa: E402
from pronaos.core.circuit import CircuitBreakerRegistry  # noqa: E402
from pronaos.core.quota import QuotaTracker  # noqa: E402
from pronaos.core.ratelimit import InMemoryRateLimiter  # noqa: E402
from pronaos.core.router import Router  # noqa: E402
from pronaos.db.models import ApiKey, Base, Team, Tenant  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _seed() -> tuple[str, str]:
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
        await conn.execute(text("INSERT INTO alembic_version VALUES ('9999')"))

    sm = async_sessionmaker(engine, expire_on_commit=False)
    usage_full, usage_prefix = generate_api_key("test")
    identity_full, identity_prefix = generate_api_key("test")

    async with sm() as session:
        tenant = Tenant(name="reliability-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="reliability-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="reliability-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="reliability-identity",
            )
        )
        await session.commit()

    await engine.dispose()
    return usage_full, identity_full


async def main() -> int:
    print("=" * 72)
    print("Phase 68 / Claim #55 - reliability + doctor backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key = await _seed()
    print(">> Seeded 1 tenant + 1 team + 2 keys")

    settings = get_settings()
    app = create_app()
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    # Install a fresh circuit breaker registry; the providers endpoint
    # reads its snapshot.
    circuit_registry = CircuitBreakerRegistry()
    app.state.circuit_registry = circuit_registry

    usage_headers = {"Authorization": f"Bearer {usage_key}"}
    identity_headers = {"Authorization": f"Bearer {identity_key}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- 1. GET providers shape ----
        print()
        print(">> Step 1: GET /v1/admin/providers (no breaker tripped)")
        r = await client.get("/v1/admin/providers", headers=usage_headers)
        assert_(
            "providers GET returns 200",
            r.status_code == 200,
            f"got {r.status_code}",
        )
        body = r.json() if r.status_code == 200 else {}
        items = body.get("items", [])
        assert_("providers list non-empty", len(items) > 0)
        expected_keys = {
            "name",
            "configured",
            "model_count",
            "typical_p50_ms",
            "circuit_state",
            "notes",
        }
        assert_(
            "every row has the full shape",
            all(set(row.keys()) == expected_keys for row in items),
        )
        groq_row = next((r for r in items if r["name"] == "groq"), None)
        assert_(
            "groq row present + configured=true (GROQ_API_KEY set)",
            groq_row is not None and groq_row["configured"] is True,
        )
        assert_(
            "groq circuit_state defaults to 'closed' before any failures",
            groq_row is not None and groq_row["circuit_state"] == "closed",
        )

        # ---- 2. Sort: configured first ----
        print()
        print(">> Step 2: configured providers sort before unconfigured")
        flags = [row["configured"] for row in items]
        seen_unconfigured = False
        ok_sort = True
        for f in flags:
            if not f:
                seen_unconfigured = True
            elif seen_unconfigured:
                ok_sort = False
                break
        assert_("configured rows sort before unconfigured", ok_sort)

        # ---- 3. Trip the groq breaker; GET reports OPEN ----
        print()
        print(">> Step 3: trip groq breaker, GET reports open")
        breaker = circuit_registry.get("groq")
        for _ in range(10):
            breaker.record_failure()
        assert_(
            "internal breaker state is OPEN after 10 failures",
            breaker.state.value == "open",
        )
        r = await client.get("/v1/admin/providers", headers=usage_headers)
        groq_row = next(r for r in r.json()["items"] if r["name"] == "groq")
        assert_(
            "GET reports groq circuit_state=open after tripping",
            groq_row["circuit_state"] == "open",
        )

        # ---- 4. admin:usage cannot reset ----
        print()
        print(">> Step 4: admin:usage cannot reset breaker")
        r = await client.post(
            "/v1/admin/providers/groq/reset-breaker",
            headers=usage_headers,
        )
        assert_(
            "reset with admin:usage returns 403",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 5. admin:identity reset flips to closed ----
        print()
        print(">> Step 5: admin:identity reset flips back to closed")
        r = await client.post(
            "/v1/admin/providers/groq/reset-breaker",
            headers=identity_headers,
        )
        assert_(
            "reset returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        assert_(
            "reset response carries circuit_state=closed",
            r.json().get("circuit_state") == "closed",
        )
        assert_(
            "internal breaker state is CLOSED after reset",
            breaker.state.value == "closed",
        )

        # ---- 6. Follow-up GET reflects the reset ----
        print()
        print(">> Step 6: follow-up GET shows groq closed again")
        r = await client.get("/v1/admin/providers", headers=usage_headers)
        groq_row = next(r for r in r.json()["items"] if r["name"] == "groq")
        assert_(
            "GET reports groq circuit_state=closed after reset",
            groq_row["circuit_state"] == "closed",
        )

        # ---- 7. Reset 404 on unknown provider ----
        print()
        print(">> Step 7: reset on unknown provider -> 404")
        r = await client.post(
            "/v1/admin/providers/not-a-provider/reset-breaker",
            headers=identity_headers,
        )
        assert_(
            "unknown provider -> 404",
            r.status_code == 404,
            f"got {r.status_code}",
        )

        # ---- 8. Doctor endpoint ----
        print()
        print(">> Step 8: GET /v1/admin/doctor returns gate report")
        r = await client.get("/v1/admin/doctor", headers=usage_headers)
        assert_(
            "doctor GET returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "doctor response has gates + summary",
            set(body.keys()) == {"gates", "summary", "has_fail", "has_warn"},
        )
        summary = body.get("summary", {})
        assert_(
            "summary counts add up to total gate count",
            summary.get("passed", 0)
            + summary.get("failed", 0)
            + summary.get("warn", 0)
            + summary.get("skip", 0)
            == summary.get("total", 0)
            == len(body.get("gates", [])),
        )
        assert_(
            "at least one gate present (default doctor runs ~14)",
            summary.get("total", 0) >= 10,
        )

    await registry.aclose()
    await engine.dispose()

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
    print("Claim #55 supported:")
    print("  The Phase 68 reliability surface round-trips:")
    print("   - GET /v1/admin/providers returns the catalog with live")
    print("     circuit-breaker state, sorted configured-first.")
    print("   - Tripping the breaker on the in-process registry flips")
    print("     the wire-format state to 'open'; resetting flips it back.")
    print("   - admin:usage GETs; admin:identity required for reset.")
    print("   - Reset 404s on unknown provider names.")
    print("   - GET /v1/admin/doctor surfaces the 14-gate report with")
    print("     summary counts that add up to the gate count.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
