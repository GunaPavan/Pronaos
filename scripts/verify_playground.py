"""Phase 65 playground backend verify (Claim #52).

The empirical question
----------------------
The playground UI consumes two backend surfaces:

1. ``GET /v1/admin/models`` (Phase 65 new) — returns the routable
   chat model catalog annotated with this team's allowlist + this
   gateway's configured providers.
2. ``POST /v1/chat/completions`` (long-standing) — gateway entry
   point for the chat call the playground actually fires.

This script proves both surfaces round-trip against an in-process
FastAPI app, with the same scope semantics the rest of /admin
already uses:

 1. GET /v1/admin/models → 200; well-formed; includes the seeded
    catalog row; allowed/configured flags carry through.
 2. GET /v1/admin/models with a key that lacks admin:usage → 403.
 3. After setting an allowlist with one fqmn, that fqmn alone
    reports allowed=true; every other row flips to allowed=false.
 4. POST /v1/chat/completions with a chat:write key authenticates
    (status != 401) — proves the playground's send path lands at
    the same middleware chain as production traffic.

No external services, no token spend. The chat call uses an
intentionally-invalid body so the test never reaches a real
provider — what we care about is the auth boundary.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_playground_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")
# Seed at least one provider so available_keys() returns a non-empty
# set and the configured-flag assertion has signal.
os.environ.setdefault("GROQ_API_KEY", "test-key-for-verify")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy import update as sa_update  # noqa: E402
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


async def _seed() -> tuple[str, str, str, str]:
    """Returns (usage_key, chat_key, team_id, identity_key) where:
      - usage_key has admin:usage scope (read /admin/models)
      - chat_key has chat:write only (cannot read /admin/models)
      - identity_key has admin:identity (used to update allowlist)
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
    chat_full, chat_prefix = generate_api_key("test")
    identity_full, identity_prefix = generate_api_key("test")

    async with sm() as session:
        tenant = Tenant(name="playground-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="playground-team")
        session.add(team)
        await session.flush()

        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="playground-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=chat_prefix,
                key_hash=hash_key(chat_full),
                scopes="chat:write",
                label="playground-chat",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:identity admin:usage",
                label="playground-identity",
            )
        )
        await session.commit()
        team_id = team.id

    await engine.dispose()
    return usage_full, chat_full, team_id, identity_full


async def main() -> int:
    print("=" * 72)
    print("Phase 65 / Claim #52 - playground backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, chat_key, team_id, _identity_key = await _seed()
    print(">> Seeded tenant + 1 team + 3 keys (admin:usage, chat:write, admin:identity)")

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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- 1. GET /v1/admin/models with admin:usage ----
        print()
        print(">> Step 1: GET /v1/admin/models with admin:usage")
        r = await client.get(
            "/v1/admin/models",
            headers={"Authorization": f"Bearer {usage_key}"},
        )
        assert_(
            "models GET returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json() if r.status_code == 200 else {}
        items = body.get("items", [])
        assert_("models response has 'items' array", isinstance(items, list))
        assert_("models response is non-empty", len(items) > 0, f"got {len(items)}")

        # ---- 2. Per-row shape ----
        print()
        print(">> Step 2: every row has the full ModelInfo shape")
        expected_keys = {
            "fqmn",
            "provider",
            "input_hcents_per_mtok",
            "output_hcents_per_mtok",
            "supports_tools",
            "supports_streaming",
            "supports_vision",
            "max_context_tokens",
            "provider_configured",
            "allowed",
        }
        shape_ok = all(set(row.keys()) == expected_keys for row in items)
        assert_("every row has the full ModelInfo shape", shape_ok)

        # ---- 3. Anthropic native models surface ----
        print()
        print(">> Step 3: anthropic native models present even without a catalog entry")
        fqmns = {row["fqmn"] for row in items}
        assert_(
            "anthropic/claude-opus-4-7 present",
            "anthropic/claude-opus-4-7" in fqmns,
        )
        assert_(
            "anthropic/claude-sonnet-4-6 present",
            "anthropic/claude-sonnet-4-6" in fqmns,
        )
        assert_(
            "anthropic/claude-haiku-4-5 present",
            "anthropic/claude-haiku-4-5" in fqmns,
        )

        # ---- 4. Groq is configured (we set GROQ_API_KEY); anthropic is not ----
        print()
        print(">> Step 4: provider_configured reflects available_keys()")
        by_provider: dict[str, list[bool]] = {}
        for row in items:
            by_provider.setdefault(row["provider"], []).append(
                bool(row["provider_configured"])
            )
        groq_configured = by_provider.get("groq", [])
        assert_(
            "groq rows report configured=true",
            len(groq_configured) > 0 and all(groq_configured),
            f"groq configured flags: {groq_configured}",
        )
        anthropic_configured = by_provider.get("anthropic", [])
        assert_(
            "anthropic rows report configured=false (no ANTHROPIC_API_KEY set)",
            len(anthropic_configured) > 0 and not any(anthropic_configured),
            f"anthropic configured flags: {anthropic_configured}",
        )

        # ---- 5. chat:write key cannot read /admin/models ----
        print()
        print(">> Step 5: chat:write key cannot read /admin/models")
        r = await client.get(
            "/v1/admin/models",
            headers={"Authorization": f"Bearer {chat_key}"},
        )
        assert_(
            "chat:write key gets 403 on /admin/models",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 6. Set allowlist; only that row flips allowed=true ----
        # The PATCH /admin/teams endpoint only accepts name updates today,
        # so set allowed_models directly on the row. The endpoint's
        # downstream reader (models endpoint) loads the team via session.get
        # which honours the value regardless of how it got there.
        print()
        print(">> Step 6: allowlist restricts the 'allowed' flag to exactly one row")
        chosen = "groq/llama-3.1-8b-instant"
        async with sm() as session:
            await session.execute(
                sa_update(Team)
                .where(Team.id == team_id)
                .values(allowed_models=[chosen])
            )
            await session.commit()
        r = await client.get(
            "/v1/admin/models",
            headers={"Authorization": f"Bearer {usage_key}"},
        )
        allowed_rows = [row for row in r.json()["items"] if row["allowed"]]
        assert_(
            "exactly one row reports allowed=true",
            len(allowed_rows) == 1,
            f"got {len(allowed_rows)}",
        )
        assert_(
            "allowed row is the one we whitelisted",
            len(allowed_rows) == 1 and allowed_rows[0]["fqmn"] == chosen,
            f"got {allowed_rows[0]['fqmn'] if allowed_rows else 'none'}",
        )

        # ---- 7. Chat endpoint authenticates with the chat:write key ----
        # Use openai/* — OPENAI_API_KEY isn't set, so the chat handler
        # rejects the call BEFORE any network round-trip, returning a
        # provider-not-configured 5xx. A 401 here would indicate the
        # gateway's own auth layer rejected our key, which is what we're
        # testing AGAINST.
        print()
        print(">> Step 7: POST /v1/chat/completions authenticates with chat:write")
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {chat_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert_(
            "fresh chat:write key authenticates (status != 401)",
            r.status_code != 401,
            f"got {r.status_code}",
        )

        # ---- 8. Sort invariant: allowed-and-configured before allowed-only ----
        print()
        print(
            ">> Step 8: rows sorted by (allowed && configured) then (allowed) then disallowed"
        )

        def _bucket(row: dict[str, object]) -> int:
            if row["allowed"] and row["provider_configured"]:
                return 0
            if row["allowed"]:
                return 1
            return 2

        models_again = await client.get(
            "/v1/admin/models",
            headers={"Authorization": f"Bearer {usage_key}"},
        )
        buckets = [_bucket(row) for row in models_again.json()["items"]]
        assert_(
            "rows are bucket-sorted",
            buckets == sorted(buckets),
            f"first 10 buckets: {buckets[:10]}",
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
    print("Claim #52 supported:")
    print("  The Phase 65 playground backend surface round-trips:")
    print("   - GET /v1/admin/models returns the catalog with shape, anthropic")
    print("     native rows, provider_configured reflecting actual env config,")
    print("     and bucket-sorted output.")
    print("   - admin:usage gates the read; chat:write gets a clean 403.")
    print("   - Team.allowed_models flows through to the 'allowed' flag on")
    print("     every fqmn.")
    print("   - The chat endpoint (the playground's send path) still")
    print("     authenticates against chat:write keys end-to-end.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
