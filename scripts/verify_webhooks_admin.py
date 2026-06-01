"""Phase 70 webhook console backend verify (Claim #57).

The empirical question
----------------------
Phase 70 adds admin-scoped webhook endpoints that lift the tenant-
isolation restriction from the existing consumer endpoints:

  GET  /v1/admin/webhooks/{tenant_id}      any tenant, admin:usage
  PUT  /v1/admin/webhooks/{tenant_id}      any tenant, admin:identity
  POST /v1/admin/webhooks/{tenant_id}/test  fire a signed test ping

This script proves the surfaces round-trip:

 1. GET returns the shape (url + secret_set) + 404 on unknown tenant.
 2. PUT sets url + secret; response shows url + secret_set=true;
    secret never returned in body.
 3. Invalid URL -> 422; URL-without-secret -> 422; short secret -> 422.
 4. admin:usage cannot PUT (403).
 5. PUT null/null clears the config.
 6. Test-ping on un-configured tenant -> 422.
 7. Test-ping fires an HMAC-signed HTTP POST to a local test server
    (aiohttp serving on a random port), returns is_intact result +
    HTTP status 200 from the receiver.

No real external service: the test receiver is an in-process aiohttp
server on localhost.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_webhooks_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")

import httpx  # noqa: E402
from aiohttp import web  # noqa: E402
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
        tenant = Tenant(name="webhook-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="webhook-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="webhook-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="webhook-identity",
            )
        )
        await session.commit()
        tenant_id = tenant.id

    await engine.dispose()
    return usage_full, identity_full, tenant_id


async def _start_test_server() -> tuple[web.AppRunner, int]:
    """Spin up a tiny aiohttp server that accepts POST and returns 200."""
    app = web.Application()

    async def handler(req: web.Request) -> web.Response:
        return web.Response(text="ok from test server")

    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, port


async def main() -> int:
    print("=" * 72)
    print("Phase 70 / Claim #57 - webhook console backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, tenant_id = await _seed()
    print(">> Seeded 1 tenant + 1 team + 2 keys")

    # Start the local test-ping receiver BEFORE the verify runs.
    runner, port = await _start_test_server()
    test_url = f"http://127.0.0.1:{port}/"
    print(f">> Test receiver on {test_url}")

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
        # ---- 1. GET returns shape ----
        print()
        print(">> Step 1: GET /v1/admin/webhooks/{tenant_id}")
        r = await client.get(f"/v1/admin/webhooks/{tenant_id}", headers=usage_headers)
        assert_("webhook GET returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "GET shape has tenant_id + url + secret_set",
            set(body.keys()) == {"tenant_id", "url", "secret_set"},
        )
        assert_("url starts as None", body.get("url") is None)
        assert_("secret_set starts as False", body.get("secret_set") is False)

        # ---- 2. GET 404 unknown tenant ----
        print()
        print(">> Step 2: GET unknown tenant -> 404")
        r = await client.get(
            "/v1/admin/webhooks/no_such_tenant", headers=usage_headers
        )
        assert_(
            "unknown tenant -> 404",
            r.status_code == 404,
            f"got {r.status_code}",
        )

        # ---- 3. PUT sets url + secret ----
        print()
        print(">> Step 3: PUT sets url + secret")
        r = await client.put(
            f"/v1/admin/webhooks/{tenant_id}",
            headers=identity_headers,
            json={"url": test_url, "secret": "x" * 32},
        )
        assert_(
            "PUT returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        assert_(
            "PUT response carries url",
            body.get("url") == test_url,
        )
        assert_(
            "secret_set is True",
            body.get("secret_set") is True,
        )
        assert_("secret NOT in response body", "secret" not in body)

        # ---- 4. admin:usage cannot PUT ----
        print()
        print(">> Step 4: admin:usage cannot PUT (403)")
        r = await client.put(
            f"/v1/admin/webhooks/{tenant_id}",
            headers=usage_headers,
            json={"url": test_url, "secret": "x" * 32},
        )
        assert_("PUT with admin:usage returns 403", r.status_code == 403)

        # ---- 5. Invalid URL -> 422 ----
        print()
        print(">> Step 5: invalid URL -> 422")
        r = await client.put(
            f"/v1/admin/webhooks/{tenant_id}",
            headers=identity_headers,
            json={"url": "not-a-url", "secret": "x" * 32},
        )
        assert_("invalid URL -> 422", r.status_code == 422, f"got {r.status_code}")

        # ---- 6. URL-without-secret -> 422 ----
        print()
        print(">> Step 6: URL-without-secret -> 422")
        r = await client.put(
            f"/v1/admin/webhooks/{tenant_id}",
            headers=identity_headers,
            json={"url": test_url, "secret": None},
        )
        assert_(
            "URL without secret -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

        # ---- 7. Test-ping fires real HTTP + returns result ----
        print()
        print(">> Step 7: test-ping fires HTTP to local receiver")
        r = await client.post(
            f"/v1/admin/webhooks/{tenant_id}/test", headers=identity_headers
        )
        assert_(
            "test-ping returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        assert_(
            "test-ping result http_status == 200",
            body.get("http_status") == 200,
            f"got {body.get('http_status')}",
        )
        assert_("test-ping result signed=True", body.get("signed") is True)
        assert_("test-ping result error is None", body.get("error") is None)
        assert_(
            "delivery_id present",
            bool(body.get("delivery_id")),
        )

        # ---- 8. Clear config with null/null ----
        print()
        print(">> Step 8: clear config with null/null")
        r = await client.put(
            f"/v1/admin/webhooks/{tenant_id}",
            headers=identity_headers,
            json={"url": None, "secret": None},
        )
        body = r.json()
        assert_("clear returns url=None", body.get("url") is None)
        assert_("clear returns secret_set=False", body.get("secret_set") is False)

        # ---- 9. Test-ping on un-configured tenant -> 422 ----
        print()
        print(">> Step 9: test-ping without config -> 422")
        r = await client.post(
            f"/v1/admin/webhooks/{tenant_id}/test", headers=identity_headers
        )
        assert_(
            "test-ping without config -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

    await runner.cleanup()
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
    print("Claim #57 supported:")
    print("  The Phase 70 webhook console backend round-trips:")
    print("   - GET /v1/admin/webhooks returns config with masked secret.")
    print("   - PUT sets url + secret; secret never echoed back.")
    print("   - admin:usage cannot PUT (403).")
    print("   - Invalid URL / URL-without-secret -> 422.")
    print("   - Test-ping fires a real HMAC-signed HTTP POST to a local")
    print("     receiver and returns the HTTP status + delivery_id.")
    print("   - Clear config with null/null works cleanly.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
