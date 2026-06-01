"""Phase 66 routing console backend verify (Claim #53).

The empirical question
----------------------
Phase 66 ships the routing console UI. The backend half is a new
``/v1/admin/routing/{team_id}`` endpoint that composes every per-team
routing-related column (strategy + allowlist + thresholds + scores)
into one GET/PUT pair. This script proves the surface round-trips:

 1. GET returns the full shape with every nullable field.
 2. PUT a strategy -> follow-up GET reflects it.
 3. PUT with PATCH semantics: setting one field doesn't clobber
    another, ``null`` clears, omitted is unchanged.
 4. Scope split holds: ``admin:usage`` GETs work; PUT requires
    ``admin:identity``.
 5. Validation: invalid strategy enum -> 422; out-of-range threshold
    -> 422; bad score-dict shape -> 422.
 6. Score dicts round-trip with their metadata (n_samples,
    source_eval_id) intact.
 7. Allowlist: ``null`` != ``[]`` (no allowlist vs "no models allowed").

No external services, no token spend.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_routing_verify_", suffix=".sqlite", delete=False
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


async def _seed() -> tuple[str, str, str]:
    """Seed 1 tenant + 1 team + 2 keys. Returns (usage_key, identity_key, team_id)."""
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
        tenant = Tenant(name="routing-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="routing-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="routing-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="routing-identity",
            )
        )
        await session.commit()
        team_id = team.id

    await engine.dispose()
    return usage_full, identity_full, team_id


async def main() -> int:
    print("=" * 72)
    print("Phase 66 / Claim #53 - routing console backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, team_id = await _seed()
    print(">> Seeded 1 tenant + 1 team + 2 keys (admin:usage, admin:identity)")

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
        # ---- 1. GET returns the full shape ----
        print()
        print(">> Step 1: GET /v1/admin/routing/{team_id}")
        r = await client.get(f"/v1/admin/routing/{team_id}", headers=usage_headers)
        assert_("routing GET returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        expected_keys = {
            "team_id",
            "routing_strategy",
            "allowed_models",
            "quality_threshold",
            "quality_scores",
            "tool_use_threshold",
            "tool_use_scores",
            "prompt_cache_min_samples",
            "prompt_cache_min_hit_rate",
            "reasoning_aware_min_samples",
            "reasoning_aware_max_ratio",
        }
        assert_(
            "GET returns the full 11-field shape",
            set(body.keys()) == expected_keys,
            f"missing: {expected_keys - set(body.keys())}",
        )
        assert_(
            "all fields NULL on a freshly seeded team",
            all(body[k] is None for k in expected_keys if k != "team_id"),
        )

        # ---- 2. admin:usage cannot PUT ----
        print()
        print(">> Step 2: admin:usage key cannot PUT")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=usage_headers,
            json={"routing_strategy": "cheapest"},
        )
        assert_(
            "PUT with admin:usage returns 403",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 3. admin:identity PUT sets strategy ----
        print()
        print(">> Step 3: admin:identity PUT sets the strategy")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"routing_strategy": "quality-aware-cheapest"},
        )
        assert_(
            "PUT with admin:identity returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        assert_(
            "PUT response carries new strategy",
            r.json().get("routing_strategy") == "quality-aware-cheapest",
        )

        # ---- 4. PATCH semantics: setting one field doesn't clobber others ----
        print()
        print(">> Step 4: PATCH semantics -- setting threshold preserves strategy")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"quality_threshold": 0.85},
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "quality_threshold updated to 0.85",
            body.get("quality_threshold") == 0.85,
        )
        assert_(
            "routing_strategy still quality-aware-cheapest (omitted != cleared)",
            body.get("routing_strategy") == "quality-aware-cheapest",
        )

        # ---- 5. null clears ----
        print()
        print(">> Step 5: null clears the column")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"quality_threshold": None},
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "null clears quality_threshold",
            body.get("quality_threshold") is None,
        )
        assert_(
            "strategy preserved through null-clear (still quality-aware-cheapest)",
            body.get("routing_strategy") == "quality-aware-cheapest",
        )

        # ---- 6. Invalid strategy -> 422 ----
        print()
        print(">> Step 6: invalid strategy enum value -> 422")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"routing_strategy": "not-a-real-strategy"},
        )
        assert_(
            "invalid strategy -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

        # ---- 7. Out-of-range threshold -> 422 ----
        print()
        print(">> Step 7: threshold > 1.0 -> 422")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"quality_threshold": 1.5},
        )
        assert_(
            "out-of-range threshold -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

        # ---- 8. Score dicts round-trip with metadata ----
        print()
        print(">> Step 8: score dicts preserve metadata (n_samples / source_eval_id)")
        scores_in = {
            "groq/llama-3.1-8b-instant": {
                "score": 0.4,
                "n_samples": 8,
                "source_eval_id": "basic-2026-05",
            },
            "groq/llama-3.3-70b-versatile": {"score": 0.95, "n_samples": 12},
        }
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"quality_scores": scores_in},
        )
        scores_out = r.json().get("quality_scores", {}) if r.status_code == 200 else {}
        assert_(
            "scores round-trip with correct score values",
            scores_out.get("groq/llama-3.1-8b-instant", {}).get("score") == 0.4
            and scores_out.get("groq/llama-3.3-70b-versatile", {}).get("score") == 0.95,
        )
        assert_(
            "score metadata (n_samples) preserved verbatim",
            scores_out.get("groq/llama-3.1-8b-instant", {}).get("n_samples") == 8,
        )
        assert_(
            "score metadata (source_eval_id) preserved verbatim",
            scores_out.get("groq/llama-3.1-8b-instant", {}).get("source_eval_id")
            == "basic-2026-05",
        )

        # ---- 9. Bad score-dict shape -> 422 ----
        print()
        print(">> Step 9: score dict missing 'score' key -> 422")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"quality_scores": {"groq/llama-3.1-8b-instant": {"n_samples": 8}}},
        )
        assert_(
            "score dict missing 'score' -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

        # ---- 10. Allowlist: null vs [] are distinct ----
        print()
        print(">> Step 10: allowlist -- null != empty list")
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"allowed_models": []},
        )
        assert_(
            "PUT allowed_models=[] returns 200",
            r.status_code == 200,
            f"got {r.status_code}",
        )
        assert_(
            "empty list persists as []",
            r.json().get("allowed_models") == [],
        )
        r = await client.put(
            f"/v1/admin/routing/{team_id}",
            headers=identity_headers,
            json={"allowed_models": None},
        )
        assert_(
            "null clears allowlist (back to None)",
            r.json().get("allowed_models") is None,
        )

        # ---- 11. Follow-up GET reflects every persisted change ----
        print()
        print(">> Step 11: follow-up GET shows the persisted state")
        r = await client.get(f"/v1/admin/routing/{team_id}", headers=usage_headers)
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "final GET shows strategy + scores + cleared threshold",
            body.get("routing_strategy") == "quality-aware-cheapest"
            and body.get("quality_threshold") is None
            and body.get("quality_scores") is not None
            and body.get("allowed_models") is None,
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
    print("Claim #53 supported:")
    print("  The Phase 66 routing console backend round-trips:")
    print("   - GET composes every per-team routing-related column.")
    print("   - PUT uses PATCH semantics (omitted unchanged, null clears).")
    print("   - admin:usage GETs; admin:identity writes; clean 403 on mismatch.")
    print("   - Strategy enum + thresholds + score-dict shape validated.")
    print("   - Score metadata (n_samples, source_eval_id) preserved.")
    print("   - Allowlist treats null (no list) and [] (empty list) as distinct.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
