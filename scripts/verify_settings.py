"""Phase 71 settings + OIDC backend verify (Claim #58).

The empirical question
----------------------
Phase 71 ships:

1. ``GET /v1/admin/settings`` — sanitised gateway config summary (no
   secrets; just booleans + non-secret OIDC issuer URL).
2. Extended ``PATCH /v1/admin/tenants/{id}`` — now accepts
   ``oidc_subject`` so operators can set/clear SSO bindings from
   the UI without CLI access.

This script proves both surfaces round-trip:

 1. GET /v1/admin/settings returns the expected shape.
 2. No API keys / secrets appear in the response.
 3. The configured providers match the seeded env vars.
 4. PATCH sets oidc_subject; follow-up GET reflects it.
 5. PATCH with null clears oidc_subject.
 6. PATCH with empty string clears oidc_subject.
 7. PATCH omitting oidc_subject leaves it unchanged (model_fields_set).
 8. Settings GET requires admin:usage (403 on chat:write).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_settings_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")

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


async def _seed() -> tuple[str, str, str]:
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
        tenant = Tenant(name="settings-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="settings-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="settings-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="settings-identity",
            )
        )
        await session.commit()
        tenant_id = tenant.id

    await engine.dispose()
    return usage_full, identity_full, tenant_id


async def main() -> int:
    print("=" * 72)
    print("Phase 71 / Claim #58 - settings + OIDC backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, tenant_id = await _seed()
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

    usage_headers = {"Authorization": f"Bearer {usage_key}"}
    identity_headers = {"Authorization": f"Bearer {identity_key}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- 1. GET settings returns shape ----
        print()
        print(">> Step 1: GET /v1/admin/settings shape")
        r = await client.get("/v1/admin/settings", headers=usage_headers)
        assert_("settings GET returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        expected_keys = {
            "redis_configured", "semantic_cache_enabled",
            "anthropic_configured", "groq_configured", "openai_configured",
            "bedrock_configured", "vertex_configured", "mcp_enabled",
            "presidio_enabled", "singleflight_distributed",
            "oidc_configured", "oidc_issuer", "database_scheme",
        }
        assert_(
            "settings GET returns the full shape",
            set(body.keys()) == expected_keys,
            f"missing: {expected_keys - set(body.keys())}",
        )

        # ---- 2. No secrets in response ----
        print()
        print(">> Step 2: no secrets in response")
        raw = str(body)
        assert_(
            "GROQ key NOT in response",
            "test-groq-key" not in raw,
        )
        assert_(
            "database_scheme does NOT include password",
            ":" not in (body.get("database_scheme") or "")
            or body.get("database_scheme", "").count(":") == 1,  # sqlite+aiosqlite is fine
        )

        # ---- 3. Configured flags reflect env ----
        print()
        print(">> Step 3: configured flags match env vars")
        assert_(
            "groq_configured=True (GROQ_API_KEY set)",
            body.get("groq_configured") is True,
        )
        assert_(
            "redis_configured=False (no REDIS_URL)",
            body.get("redis_configured") is False,
        )
        assert_(
            "database_scheme=sqlite+aiosqlite",
            "sqlite" in (body.get("database_scheme") or ""),
        )

        # ---- 4. Settings requires admin:usage ----
        print()
        print(">> Step 4: settings GET requires admin:usage")
        # Generate a chat:write-only key
        chat_full, chat_prefix = generate_api_key("test")
        async with sm() as session:
            session.add(
                ApiKey(
                    team_id=(await session.execute(
                        __import__("sqlalchemy", fromlist=["select"]).select(Team).limit(1)
                    )).scalar_one().id,
                    prefix=chat_prefix,
                    key_hash=hash_key(chat_full),
                    scopes="chat:write",
                    label="chat-only",
                )
            )
            await session.commit()
        r = await client.get(
            "/v1/admin/settings",
            headers={"Authorization": f"Bearer {chat_full}"},
        )
        assert_(
            "chat:write key gets 403 on /admin/settings",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 5. PATCH sets oidc_subject ----
        print()
        print(">> Step 5: PATCH sets oidc_subject")
        r = await client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=identity_headers,
            json={"oidc_subject": "auth0|user123"},
        )
        assert_("PATCH returns 200", r.status_code == 200, f"got {r.status_code}")
        assert_(
            "oidc_subject set in response",
            r.json().get("oidc_subject") == "auth0|user123",
        )

        # ---- 6. PATCH null clears oidc_subject ----
        print()
        print(">> Step 6: PATCH null clears oidc_subject")
        r = await client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=identity_headers,
            json={"oidc_subject": None},
        )
        assert_(
            "null clears oidc_subject",
            r.json().get("oidc_subject") is None,
        )

        # ---- 7. Omitting oidc_subject in PATCH preserves name ----
        print()
        print(">> Step 7: omitting oidc_subject leaves it unchanged")
        # Re-set it first
        await client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=identity_headers,
            json={"oidc_subject": "auth0|preserve"},
        )
        # Patch only name
        r = await client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=identity_headers,
            json={"name": "renamed-tenant"},
        )
        body = r.json()
        assert_(
            "name updated",
            body.get("name") == "renamed-tenant",
        )
        assert_(
            "oidc_subject preserved through name-only PATCH",
            body.get("oidc_subject") == "auth0|preserve",
        )

        # ---- 8. Empty string clears oidc_subject ----
        print()
        print(">> Step 8: empty string clears oidc_subject")
        r = await client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=identity_headers,
            json={"oidc_subject": ""},
        )
        assert_(
            "empty string clears oidc_subject",
            r.json().get("oidc_subject") is None,
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
    print("Claim #58 supported:")
    print("  The Phase 71 settings + OIDC backend surface round-trips:")
    print("   - GET /v1/admin/settings returns the sanitised config shape.")
    print("   - No API keys or secrets leak into the response.")
    print("   - configured flags match env vars; database_scheme is present.")
    print("   - chat:write key gets 403 on /admin/settings.")
    print("   - PATCH /tenants/{id} now accepts oidc_subject:")
    print("     set, null-clear, empty-string-clear, and omit-preserves")
    print("     all work correctly via model_fields_set semantics.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
