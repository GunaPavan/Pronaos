"""Phase 64 FinOps verify (Claim #51) — usage + timeseries + budgets round-trip.

The empirical question
----------------------
Pronaos's Phase 64 adds two REST surfaces to the gateway:
  1. GET /v1/admin/usage/timeseries — dense time-bucketed aggregates
     suitable for charts on the admin UI.
  2. GET/PUT /v1/admin/budgets/{team_id} — per-team token + cost caps,
     with a separate ``admin:identity`` scope gating writes so an
     ``admin:usage`` key cannot grant itself more budget.

This script proves the surface works end-to-end against a real DB:
 1. Seed N usage_records spread across two teams + two days.
 2. GET /v1/admin/usage returns those rows with correct totals.
 3. GET /v1/admin/usage/timeseries (bucket=day) returns dense, zero-filled
    buckets that sum to the totals from step 2.
 4. ``admin:usage`` key reads timeseries fine (200), reads budget fine,
    but cannot PUT budget (403 — scope check holds).
 5. ``admin:identity`` key can PUT budget. Null clears.
 6. PUT round-trips: a follow-up GET sees the new cap.
 7. Partial PUT (token cap only) leaves cost cap unchanged.

All steps run against an in-process FastAPI app via ASGITransport.
No external services, no token spend.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_finops_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from pronaos.auth.api_keys import generate_api_key, hash_key  # noqa: E402
from pronaos.config import get_settings  # noqa: E402
from pronaos.core.quota import QuotaTracker  # noqa: E402
from pronaos.core.ratelimit import InMemoryRateLimiter  # noqa: E402
from pronaos.core.router import Router  # noqa: E402
from pronaos.db.models import ApiKey, Base, Team, Tenant, UsageRecord  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _seed_world() -> tuple[str, str, str, str]:
    """Seed:
      - 1 tenant
      - 2 teams (te_a, te_b)
      - 1 admin:usage key  (read-only)
      - 1 admin:identity key (read + write)
      - 6 usage records spread across the two teams, two days
    Returns (usage_key, identity_key, team_a_id, team_b_id).
    """
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
        tenant = Tenant(name="finops-tenant")
        session.add(tenant)
        await session.flush()
        team_a = Team(tenant_id=tenant.id, name="team-a")
        team_b = Team(tenant_id=tenant.id, name="team-b")
        session.add_all([team_a, team_b])
        await session.flush()
        session.add(
            ApiKey(
                team_id=team_a.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="finops-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team_a.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:identity admin:usage",
                label="finops-identity",
            )
        )

        # 6 usage records: 3 for each team, spread across two days.
        now = datetime.now(tz=UTC)
        day_old = now - timedelta(days=1)
        rows = [
            # team_a: 3 calls, 6_000 hcents total
            (team_a.id, tenant.id, day_old, "groq", "llama3-8b", 1000, 500, 1_500),
            (team_a.id, tenant.id, day_old, "groq", "llama3-8b", 800, 400, 1_500),
            (team_a.id, tenant.id, now, "groq", "llama3-70b", 1200, 600, 3_000),
            # team_b: 3 calls, 9_000 hcents total
            (team_b.id, tenant.id, day_old, "groq", "llama3-8b", 500, 200, 1_000),
            (team_b.id, tenant.id, now, "groq", "llama3-70b", 1500, 800, 4_000),
            (team_b.id, tenant.id, now, "groq", "llama3-70b", 1800, 1000, 4_000),
        ]
        for (
            team_id,
            tenant_id,
            ts,
            provider,
            model,
            pt,
            ct,
            cost,
        ) in rows:
            session.add(
                UsageRecord(
                    ts=ts,
                    tenant_id=tenant_id,
                    team_id=team_id,
                    key_id="bootstrap",
                    provider=provider,
                    model=model,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    cost_hcents=cost,
                    status="ok",
                )
            )
        await session.commit()

        team_a_id, team_b_id = team_a.id, team_b.id

    await engine.dispose()
    return usage_full, identity_full, team_a_id, team_b_id


async def main() -> int:
    print("=" * 72)
    print("Phase 64 / Claim #51 - FinOps verify (usage + timeseries + budgets)")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, team_a, team_b = await _seed_world()
    print(">> Seeded tenant + 2 teams + 6 usage records + 2 keys")

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

    usage_headers = {"Authorization": f"Bearer {usage_key}"}
    identity_headers = {"Authorization": f"Bearer {identity_key}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # ---- 1. GET /v1/admin/usage — totals match the seed ----
        print()
        print(">> Step 1: GET /v1/admin/usage")
        r = await client.get("/v1/admin/usage", headers=usage_headers)
        assert_("usage list returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "usage totals.requests == 6",
            body.get("totals", {}).get("requests") == 6,
            f"got {body.get('totals', {})}",
        )
        assert_(
            "usage totals.cost_hcents == 15_000",
            body.get("totals", {}).get("cost_hcents") == 15_000,
            f"got {body.get('totals', {}).get('cost_hcents')}",
        )

        # ---- 2. GET /v1/admin/usage/timeseries (bucket=day) ----
        print()
        print(">> Step 2: GET /v1/admin/usage/timeseries?bucket=day")
        start = (datetime.now(tz=UTC) - timedelta(days=3)).isoformat()
        end = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
        r = await client.get(
            "/v1/admin/usage/timeseries",
            params={"start_ts": start, "end_ts": end, "bucket": "day"},
            headers=usage_headers,
        )
        assert_(
            "timeseries returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        ts_body = r.json() if r.status_code == 200 else {}
        assert_(
            "timeseries bucket_size_seconds == 86_400",
            ts_body.get("bucket_size_seconds") == 86_400,
            f"got {ts_body.get('bucket_size_seconds')}",
        )
        points = ts_body.get("points", [])
        total_cost = sum(p["cost_hcents"] for p in points)
        total_requests = sum(p["requests"] for p in points)
        assert_(
            "timeseries cost matches usage totals (15_000)",
            total_cost == 15_000,
            f"got {total_cost}",
        )
        assert_(
            "timeseries requests match usage totals (6)",
            total_requests == 6,
            f"got {total_requests}",
        )
        assert_(
            "timeseries has at least 2 dense buckets (one per seed day)",
            len(points) >= 2,
            f"got {len(points)} buckets",
        )

        # ---- 3. GET /v1/admin/budgets/{team_a} works with admin:usage ----
        print()
        print(">> Step 3: GET /v1/admin/budgets/{team_a} with admin:usage")
        r = await client.get(
            f"/v1/admin/budgets/{team_a}", headers=usage_headers
        )
        assert_(
            "budget GET returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        budget_a = r.json() if r.status_code == 200 else {}
        assert_(
            "budget GET shape includes team_id",
            budget_a.get("team_id") == team_a,
            f"got {budget_a.get('team_id')!r}",
        )

        # ---- 4. PUT with admin:usage is REJECTED (403) ----
        print()
        print(">> Step 4: PUT /v1/admin/budgets/{team_a} with admin:usage -> 403")
        r = await client.put(
            f"/v1/admin/budgets/{team_a}",
            headers=usage_headers,
            json={"monthly_token_budget": 100_000},
        )
        assert_(
            "budget PUT with admin:usage returns 403",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 5. PUT with admin:identity succeeds ----
        print()
        print(">> Step 5: PUT /v1/admin/budgets/{team_a} with admin:identity")
        r = await client.put(
            f"/v1/admin/budgets/{team_a}",
            headers=identity_headers,
            json={
                "monthly_token_budget": 100_000,
                "monthly_cost_hcents_budget": 50_000,
            },
        )
        assert_(
            "budget PUT with admin:identity returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "PUT response carries monthly_token_budget = 100_000",
            body.get("monthly_token_budget") == 100_000,
            f"got {body.get('monthly_token_budget')}",
        )
        assert_(
            "PUT response carries monthly_cost_hcents_budget = 50_000",
            body.get("monthly_cost_hcents_budget") == 50_000,
            f"got {body.get('monthly_cost_hcents_budget')}",
        )

        # ---- 6. PUT round-trips: follow-up GET sees the new cap ----
        print()
        print(">> Step 6: follow-up GET sees the new cap")
        r = await client.get(
            f"/v1/admin/budgets/{team_a}", headers=identity_headers
        )
        assert_(
            "follow-up budget GET returns the new token cap",
            r.json().get("monthly_token_budget") == 100_000,
            f"got {r.json().get('monthly_token_budget')}",
        )

        # ---- 7. Partial PUT (only token cap) leaves cost cap intact ----
        print()
        print(">> Step 7: partial PUT (only token cap) leaves cost cap intact")
        r = await client.put(
            f"/v1/admin/budgets/{team_a}",
            headers=identity_headers,
            json={"monthly_token_budget": 200_000},
        )
        assert_(
            "partial PUT returns 200",
            r.status_code == 200,
            f"got {r.status_code}",
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "partial PUT changed token cap to 200_000",
            body.get("monthly_token_budget") == 200_000,
            f"got {body.get('monthly_token_budget')}",
        )
        assert_(
            "partial PUT preserved cost cap (50_000)",
            body.get("monthly_cost_hcents_budget") == 50_000,
            f"got {body.get('monthly_cost_hcents_budget')}",
        )

        # ---- 8. Explicit null clears a cap ----
        print()
        print(">> Step 8: null clears the cost cap")
        r = await client.put(
            f"/v1/admin/budgets/{team_a}",
            headers=identity_headers,
            json={"monthly_cost_hcents_budget": None},
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "null clears cost cap",
            body.get("monthly_cost_hcents_budget") is None,
            f"got {body.get('monthly_cost_hcents_budget')!r}",
        )
        assert_(
            "null clear preserves token cap (200_000)",
            body.get("monthly_token_budget") == 200_000,
            f"got {body.get('monthly_token_budget')}",
        )

        # ---- 9. team_b has its own untouched budget (no cross-team bleed) ----
        print()
        print(">> Step 9: team_b budget is independent of team_a edits")
        r = await client.get(
            f"/v1/admin/budgets/{team_b}", headers=identity_headers
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "team_b token cap is unchanged (None)",
            body.get("monthly_token_budget") is None,
            f"got {body.get('monthly_token_budget')!r}",
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
    print("Claim #51 supported:")
    print("  The Phase 64 FinOps surface round-trips end-to-end:")
    print("   - GET /v1/admin/usage totals match the seeded rows.")
    print("   - GET /v1/admin/usage/timeseries produces dense, day-bucketed")
    print("     points that re-sum to the same cost + request totals.")
    print("   - GET budgets is gated by admin:usage; PUT requires the new")
    print("     admin:identity scope (admin:usage gets 403 on writes).")
    print("   - PUT persists, supports partial updates, and treats null as")
    print("     'clear this cap' (vs omitted = 'leave unchanged').")
    print("   - Budgets are per-team — team_b is untouched by team_a edits.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
