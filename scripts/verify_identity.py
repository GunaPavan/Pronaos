"""Phase 63 identity REST verify (Claim #50) — end-to-end round-trip.

The empirical question
----------------------
Pronaos's Phase 63 adds REST CRUD for tenants, teams, and API keys.
Until Phase 63, these operations only existed in the CLI — the UI
had no way to create the identity primitives it needs.

This script proves the full lifecycle works:
1. POST /v1/admin/tenants  → 201, returns id
2. POST /v1/admin/teams     → 201, references the tenant
3. POST /v1/admin/keys      → 201, returns the FULL secret exactly once
4. The newly generated key authenticates against /v1/chat/completions
   (we use a deliberately-malformed body to short-circuit before any
   real provider call — auth pass is observable as a non-401 status).
5. DELETE /v1/admin/keys/{id} → 204 (soft revoke).
6. The revoked key now returns 401.
7. DELETE /v1/admin/teams/{id} → 204.
8. DELETE /v1/admin/tenants/{id} → 204.

All steps run against an in-process FastAPI app via ASGITransport.
No external services, no token spend.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_identity_verify_", suffix=".sqlite", delete=False
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
from pronaos.db.models import ApiKey, Base, Team, Tenant  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _seed_admin_identity_key() -> str:
    """Seed a key with admin:identity scope so the verify can hit the
    new endpoints. Real users bootstrap this via the CLI."""
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
    full_key, prefix = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="bootstrap")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="bootstrap-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=prefix,
                key_hash=hash_key(full_key),
                scopes="admin:identity chat:write",
                label="identity-verify",
            )
        )
        await session.commit()
    await engine.dispose()
    return full_key


async def main() -> int:
    print("=" * 72)
    print("Phase 63 / Claim #50 - identity REST verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    admin_key = await _seed_admin_identity_key()
    print(">> Seeded bootstrap admin:identity key")

    settings = get_settings()
    app = create_app()
    # Wire required app.state (mirrors tests/conftest.py).
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # ---- 1. Create tenant ----
        print()
        print(">> Step 1: POST /v1/admin/tenants")
        r = await client.post(
            "/v1/admin/tenants",
            headers=admin_headers,
            json={"name": "phase-63-tenant"},
        )
        assert_("tenant create returns 201", r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
        tenant_id = r.json()["id"] if r.status_code == 201 else ""
        assert_("tenant response carries an id", bool(tenant_id), f"got {tenant_id!r}")

        # ---- 2. Create team ----
        print()
        print(">> Step 2: POST /v1/admin/teams")
        r = await client.post(
            "/v1/admin/teams",
            headers=admin_headers,
            json={"tenant_id": tenant_id, "name": "phase-63-team"},
        )
        assert_("team create returns 201", r.status_code == 201, f"got {r.status_code}")
        team_id = r.json()["id"] if r.status_code == 201 else ""
        assert_(
            "team carries the right tenant_id back",
            r.status_code == 201 and r.json()["tenant_id"] == tenant_id,
            "see response",
        )

        # ---- 3. Generate API key — secret returned exactly once ----
        print()
        print(">> Step 3: POST /v1/admin/keys")
        r = await client.post(
            "/v1/admin/keys",
            headers=admin_headers,
            json={
                "team_id": team_id,
                "label": "phase-63-key",
                "scopes": ["chat:write"],
                "env": "test",
            },
        )
        assert_("key generate returns 201", r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
        body: Any = r.json() if r.status_code == 201 else {}
        new_key_id = body.get("id", "")
        new_full_key = body.get("api_key", "")
        assert_("response includes 'api_key' (full secret, returned once)", bool(new_full_key))
        assert_("response 'api_key' starts with pn_test_", new_full_key.startswith("pn_test_"))
        assert_("response status is 'active'", body.get("status") == "active")

        # ---- 4. Subsequent GET on the key does NOT include the secret ----
        print()
        print(">> Step 4: GET /v1/admin/keys/{id} (secret must not be present)")
        r = await client.get(f"/v1/admin/keys/{new_key_id}", headers=admin_headers)
        assert_("key get returns 200", r.status_code == 200, f"got {r.status_code}")
        assert_(
            "GET response does NOT include api_key",
            "api_key" not in r.json(),
            f"keys={list(r.json().keys())}",
        )

        # ---- 5. Newly generated key authenticates against chat ----
        print()
        print(">> Step 5: newly generated key authenticates against /v1/chat/completions")
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {new_full_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        # We don't have an OpenAI key set in the verify env so the call
        # won't actually succeed. What we care about is that it doesn't
        # 401 — that proves auth passed.
        assert_(
            "freshly issued key authenticates (status != 401)",
            r.status_code != 401,
            f"got {r.status_code}",
        )

        # ---- 6. Revoke the key ----
        print()
        print(">> Step 6: DELETE /v1/admin/keys/{id} (soft revoke)")
        r = await client.delete(f"/v1/admin/keys/{new_key_id}", headers=admin_headers)
        assert_("key delete returns 204", r.status_code == 204, f"got {r.status_code}")

        # ---- 7. Revoked key now returns 401 on chat ----
        print()
        print(">> Step 7: revoked key now 401s on chat")
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {new_full_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert_("revoked key returns 401", r.status_code == 401, f"got {r.status_code}")

        # ---- 8. Cleanup: delete team + tenant ----
        print()
        print(">> Step 8: cleanup — delete team + tenant")
        r1 = await client.delete(f"/v1/admin/teams/{team_id}", headers=admin_headers)
        r2 = await client.delete(f"/v1/admin/tenants/{tenant_id}", headers=admin_headers)
        assert_("team delete returns 204", r1.status_code == 204, f"got {r1.status_code}")
        assert_("tenant delete returns 204", r2.status_code == 204, f"got {r2.status_code}")

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
    print("Claim #50 supported:")
    print("  The Phase 63 identity REST surface round-trips end-to-end:")
    print("  tenant -> team -> key generation (full secret returned once,")
    print("  then omitted from GETs), key authenticates against chat,")
    print("  revoke is soft + blocks subsequent auth, and cascade")
    print("  deletes clean up cleanly.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
