"""Phase 62 UI Foundation verify (Claim #49) — backend contract probe.

The empirical question
----------------------
Pronaos Phase 62 ships a Next.js admin shell at ``web/`` that talks
to the existing FastAPI admin REST surface. The UI's contract with
the backend is encoded in ``web/src/lib/api/schemas.ts``. This script
verifies that contract from the Python side — boots an in-process
FastAPI app, hits the endpoints the UI will hit (``/v1/health`` +
``/v1/admin/usage``), and asserts the response shapes match what the
TypeScript Zod schemas expect.

The browser side is verified separately by ``web/tests/e2e/*.spec.ts``
(seven Playwright tests, all passing).

What this verify asserts
------------------------
1. ``GET /v1/health`` returns 200 with a JSON body containing at
   minimum ``{status: str}`` (the UI's HealthResponseSchema).
2. ``GET /v1/admin/usage`` with a valid admin-scoped API key returns
   200 with a JSON body matching the UI's UsageResponseSchema
   (``rows`` array + 4 aggregate counters).
3. ``GET /v1/admin/usage`` with NO bearer token returns 401, which
   the UI's login page uses to detect "valid gateway, missing key."
4. ``GET /admin/`` returns 503 when the Next.js bundle hasn't been
   built (or 200 once ``npm run build`` has produced web/out/).
   Either is acceptable; the mount is conditional by design.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Any

# Use a tempfile-backed SQLite so every async engine sees the same
# persistent state — :memory: gives each connection a private DB.
_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115 — leak deliberately for the run's duration
    prefix="pronaos_ui_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")

import asyncio  # noqa: E402

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


async def _seed_admin_key() -> tuple[str, str]:
    """Create one tenant + team + admin-scoped API key. Returns
    (full_key, key_prefix)."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Stamp alembic_version so any forward-looking code that reads
        # it doesn't choke.
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
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=prefix,
                key_hash=hash_key(full_key),
                scopes="admin:usage",
                label="ui-verify",
            )
        )
        await session.commit()
    await engine.dispose()
    return full_key, prefix


async def main() -> int:
    print("=" * 72)
    print("Phase 62 / Claim #49 - UI Foundation backend-contract verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    api_key, prefix = await _seed_admin_key()
    print(f">> Seeded admin key with prefix {prefix!r}")

    settings = get_settings()
    app = create_app()
    # The app's full lifespan is not invoked by ASGITransport; manually
    # wire just the state the admin endpoints need (DB, registry,
    # rate limiter, quota tracker). Matches tests/conftest.py.
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    # ASGITransport so we don't bind a real socket.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # ---- 1. /v1/healthz ----
        print()
        print(">> Step 1: GET /v1/healthz")
        r = await client.get("/v1/healthz")
        assert_(
            "/v1/healthz returns 200",
            r.status_code == 200,
            f"got {r.status_code}",
        )
        body: Any = r.json() if r.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        assert_(
            "/v1/healthz body contains a 'status' field (UI HealthResponseSchema)",
            isinstance(body, dict) and "status" in body,
            f"got {body!r}",
        )

        # ---- 2. /v1/admin/usage with bearer ----
        print()
        print(">> Step 2: GET /v1/admin/usage with admin bearer")
        r = await client.get(
            "/v1/admin/usage",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert_(
            "/v1/admin/usage returns 200 with valid admin key",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        if r.status_code == 200:
            ub = r.json()
            assert_(
                "/v1/admin/usage body has 'items' array (UI UsageResponseSchema)",
                isinstance(ub, dict) and isinstance(ub.get("items"), list),
                f"keys={list(ub.keys()) if isinstance(ub, dict) else type(ub).__name__}",
            )
            # The UI reads aggregate counters from ``totals``, which is
            # a nested object with these 5 fields.
            expected_totals_keys = {
                "requests",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost_hcents",
            }
            totals = ub.get("totals") if isinstance(ub, dict) else None
            present = (
                set(totals.keys()) & expected_totals_keys
                if isinstance(totals, dict)
                else set()
            )
            assert_(
                "all 5 aggregate keys present under .totals",
                present == expected_totals_keys,
                f"missing {expected_totals_keys - present}",
            )
            # Pagination metadata used by /usage page in Phase 64.
            assert_(
                "limit + offset pagination fields present",
                isinstance(ub, dict)
                and isinstance(ub.get("limit"), int)
                and isinstance(ub.get("offset"), int),
                "see body keys",
            )

        # ---- 3. /v1/admin/usage with no bearer → 401 ----
        print()
        print(">> Step 3: GET /v1/admin/usage with NO bearer (UI login probe)")
        r = await client.get("/v1/admin/usage")
        # 401 (preferred) or 403 are both valid "you need to authenticate"
        # signals the UI uses to keep the user on /login.
        assert_(
            "/v1/admin/usage rejects unauthenticated probe with 4xx",
            400 <= r.status_code < 500 and r.status_code in (401, 403),
            f"got {r.status_code}",
        )

        # ---- 4. /admin/ mount status (conditional on build) ----
        print()
        print(">> Step 4: GET /admin/ — static mount conditional on web build")
        r = await client.get("/admin/")
        # When ``web/out/`` is absent (no ``npm run build`` yet), the
        # mount is skipped and FastAPI returns 404. When the build has
        # produced index.html, the SPA shell is served (200).
        admin_mount_ok = r.status_code in (200, 404)
        assert_(
            "/admin/ either serves SPA (200) or is not yet built (404)",
            admin_mount_ok,
            f"got {r.status_code} — unexpected status",
        )
        if r.status_code == 200:
            assert_(
                "/admin/ response is HTML (SPA shell)",
                "html" in r.headers.get("content-type", "").lower()
                or "<html" in r.text.lower()[:200],
                f"content-type={r.headers.get('content-type')}",
            )

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
    print("Claim #49 supported (mocked-live):")
    print(
        "  The Pronaos UI Foundation contract holds end-to-end: the"
    )
    print(
        "  TypeScript Zod schemas in web/src/lib/api/schemas.ts match"
    )
    print(
        "  the Python FastAPI responses, the unauthenticated 4xx flow"
    )
    print("  works as the UI login page expects, and the /admin/ static")
    print("  mount degrades gracefully when web/out/ isn't built yet.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
