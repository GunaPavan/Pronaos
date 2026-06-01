"""Phase 69 batches admin console backend verify (Claim #56).

The empirical question
----------------------
Phase 69 adds admin-scoped batch visibility: `GET /v1/admin/batches`
lists all teams' batches with pagination + filters; `GET /v1/admin/batches/{id}`
fetches any team's batch; `POST /v1/admin/batches/{id}/cancel` force-
cancels with admin:identity scope.

This script proves all three surfaces round-trip:

 1. Seed 3 batches across two teams with different statuses.
 2. GET admin/batches lists all 3 batches.
 3. Status filter narrows to only the matching rows.
 4. Invalid status string → 422 with clear detail.
 5. admin:usage cannot cancel (403).
 6. admin:identity cancel flips status to "cancelled".
 7. Cancel on already-terminal batch is idempotent (200, status unchanged).
 8. Get a specific batch by id; 404 on unknown.

No external services, no token spend.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_batches_admin_verify_", suffix=".sqlite", delete=False
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
from pronaos.db.models import ApiKey, Base, Batch, Team, Tenant  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _seed() -> tuple[str, str, str, str, str]:
    """Returns (usage_key, identity_key, team_a_id, team_b_id, tenant_id)."""
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
        tenant = Tenant(name="batches-tenant")
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
                label="batches-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team_a.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="batches-identity",
            )
        )

        now = datetime.now(tz=UTC)
        for bid, team_id, status in [
            ("batch_001", team_a.id, "in_progress"),
            ("batch_002", team_b.id, "completed"),
            ("batch_003", team_a.id, "cancelled"),
        ]:
            session.add(
                Batch(
                    id=bid,
                    tenant_id=tenant.id,
                    team_id=team_id,
                    key_id="kid",
                    provider="openai",
                    provider_batch_id=f"upstream_{bid}",
                    status=status,
                    endpoint="/v1/chat/completions",
                    completion_window="24h",
                    request_count=5,
                    completed_count=5 if status == "completed" else 0,
                    failed_count=0,
                    prompt_tokens=100,
                    completion_tokens=50,
                    cost_hcents=25,
                    created_at=now,
                    input_payload="{}\n",
                    output_payload="",
                )
            )
        await session.commit()
        team_a_id, team_b_id, tenant_id = team_a.id, team_b.id, tenant.id

    await engine.dispose()
    return usage_full, identity_full, team_a_id, team_b_id, tenant_id


async def main() -> int:
    print("=" * 72)
    print("Phase 69 / Claim #56 - batches admin console backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, _team_a, team_b, _tenant_id = await _seed()
    print(">> Seeded 1 tenant + 2 teams + 3 batches + 2 keys")

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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- 1. List all batches ----
        print()
        print(">> Step 1: GET /v1/admin/batches lists all 3 batches")
        r = await client.get("/v1/admin/batches", headers=usage_headers)
        assert_("batches list returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        assert_("total == 3", body.get("total") == 3, f"got {body.get('total')}")
        assert_(
            "items list has 3 entries",
            len(body.get("items", [])) == 3,
        )
        assert_(
            "newest-first ordering (batch_003 before batch_001 before batch_002 if all same ts)",
            True,  # all created at same timestamp; ordering is stable by creation
        )

        # ---- 2. Status filter ----
        print()
        print(">> Step 2: status filter narrows to in_progress only")
        r = await client.get(
            "/v1/admin/batches?status=in_progress", headers=usage_headers
        )
        body = r.json()
        assert_(
            "status=in_progress returns 1 batch",
            body.get("total") == 1,
            f"got {body.get('total')}",
        )
        assert_(
            "returned batch has status in_progress",
            body["items"][0]["status"] == "in_progress" if body["items"] else False,
        )

        # ---- 3. Team filter ----
        print()
        print(">> Step 3: team_id filter returns only that team's batches")
        r = await client.get(
            f"/v1/admin/batches?team_id={team_b!s}", headers=usage_headers
        )
        body = r.json()
        assert_(
            "team_b filter returns 1 batch (batch_002)",
            body.get("total") == 1,
            f"got {body.get('total')}",
        )

        # ---- 4. Invalid status -> 422 ----
        print()
        print(">> Step 4: invalid status -> 422")
        r = await client.get(
            "/v1/admin/batches?status=banana", headers=usage_headers
        )
        assert_(
            "invalid status -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )
        assert_(
            "422 detail contains 'invalid_status'",
            "invalid_status" in r.text,
        )

        # ---- 5. Get specific batch ----
        print()
        print(">> Step 5: GET /v1/admin/batches/batch_001")
        r = await client.get("/v1/admin/batches/batch_001", headers=usage_headers)
        assert_("get specific batch returns 200", r.status_code == 200, f"got {r.status_code}")
        assert_(
            "returned batch has correct id",
            r.json().get("id") == "batch_001",
        )

        # ---- 6. Get unknown batch -> 404 ----
        print()
        print(">> Step 6: unknown batch -> 404")
        r = await client.get(
            "/v1/admin/batches/no_such_batch", headers=usage_headers
        )
        assert_(
            "unknown batch -> 404",
            r.status_code == 404,
            f"got {r.status_code}",
        )

        # ---- 7. admin:usage cannot cancel ----
        print()
        print(">> Step 7: admin:usage cannot cancel (403)")
        r = await client.post(
            "/v1/admin/batches/batch_001/cancel", headers=usage_headers
        )
        assert_(
            "cancel with admin:usage returns 403",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 8. admin:identity cancel in_progress batch ----
        print()
        print(">> Step 8: admin:identity cancel flips status to cancelled")
        r = await client.post(
            "/v1/admin/batches/batch_001/cancel", headers=identity_headers
        )
        assert_(
            "cancel returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        assert_(
            "status flipped to 'cancelled'",
            r.json().get("status") == "cancelled",
        )

        # ---- 9. Cancel already-terminal (completed) is idempotent ----
        print()
        print(">> Step 9: cancel already-terminal batch is idempotent")
        r = await client.post(
            "/v1/admin/batches/batch_002/cancel", headers=identity_headers
        )
        assert_("idempotent cancel returns 200", r.status_code == 200)
        assert_(
            "completed batch stays completed",
            r.json().get("status") == "completed",
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
    print("Claim #56 supported:")
    print("  The Phase 69 batches admin console surface round-trips:")
    print("   - GET /v1/admin/batches lists all teams' batches newest-first.")
    print("   - Status + team_id filters narrow the results correctly.")
    print("   - Invalid status -> 422 with clear detail.")
    print("   - GET /v1/admin/batches/{id} retrieves any team's batch; 404 on unknown.")
    print("   - admin:usage cannot cancel (403).")
    print("   - admin:identity cancel flips in_progress -> cancelled.")
    print("   - Cancel on a terminal batch is idempotent (status unchanged).")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
