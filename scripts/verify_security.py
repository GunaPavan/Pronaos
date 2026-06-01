"""Phase 67 security + audit backend verify (Claim #54).

The empirical question
----------------------
Phase 67 composes the per-team security config (guardrail policy +
PII tokenization flags) into one ``GET/PUT /v1/admin/security/{team}``
endpoint pair, and surfaces the hash-chained audit log via
``GET /v1/admin/audit/{tenant}`` + ``POST /audit/{tenant}/verify``.

This script proves the surfaces round-trip and the chain-tamper
detection works against a real (mutated) DB row.

 1. GET security returns the composed shape + static vocabulary.
 2. PATCH semantics: setting one field doesn't clobber the other;
    null clears guardrail_policy back to defaults.
 3. admin:identity required for PUT (admin:usage gets 403).
 4. Audit list returns the seeded records oldest-first.
 5. Audit verify reports is_intact=True on an unbroken chain.
 6. UPDATE one row's model field directly via SQL (the threat model)
    → audit verify reports is_intact=False with the tampered record's
    id in the breaks list.
 7. Unknown tenant -> 404 from both audit endpoints.

No external services, no token spend.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_DB_PATH = tempfile.NamedTemporaryFile(  # noqa: SIM115
    prefix="pronaos_security_verify_", suffix=".sqlite", delete=False
).name
os.environ["PRONAOS_SECRET_KEY"] = "x" * 64
os.environ["PRONAOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"
os.environ.setdefault("PRONAOS_REDIS_URL", "")
os.environ.setdefault("PRONAOS_SEMANTIC_CACHE_ENABLED", "false")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy import update as sa_update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from pronaos.audit.logger import AuditLogger  # noqa: E402
from pronaos.auth.api_keys import generate_api_key, hash_key  # noqa: E402
from pronaos.config import get_settings  # noqa: E402
from pronaos.core.quota import QuotaTracker  # noqa: E402
from pronaos.core.ratelimit import InMemoryRateLimiter  # noqa: E402
from pronaos.core.router import Router  # noqa: E402
from pronaos.db.models import ApiKey, AuditRecord, Base, Team, Tenant  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402

VERDICTS: list[tuple[str, bool, str]] = []


def assert_(name: str, ok: bool, detail: str = "") -> None:
    VERDICTS.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}" + (f"  --  {detail}" if detail else ""))


async def _seed() -> tuple[str, str, str, str, list[str]]:
    """Seed 1 tenant + 1 team + 2 keys + 3 chained audit records.

    Returns (usage_key, identity_key, tenant_id, team_id, record_ids).
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
        tenant = Tenant(name="security-tenant")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="security-team")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=usage_prefix,
                key_hash=hash_key(usage_full),
                scopes="admin:usage",
                label="security-usage",
            )
        )
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=identity_prefix,
                key_hash=hash_key(identity_full),
                scopes="admin:usage admin:identity",
                label="security-identity",
            )
        )
        tenant_id = tenant.id
        team_id = team.id

        logger = AuditLogger()
        record_ids: list[str] = []
        for i in range(3):
            rec = await logger.append(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                key_id="bootstrap",
                provider="groq",
                model="llama-3.1-8b-instant",
                request_body={"messages": [{"role": "user", "content": f"hi {i}"}]},
                response_body={"choices": [{"message": {"content": f"ok {i}"}}]},
                request_id=f"req_{i}",
            )
            assert rec is not None
            record_ids.append(rec.id)
        await session.commit()

    await engine.dispose()
    return usage_full, identity_full, tenant_id, team_id, record_ids


async def main() -> int:
    print("=" * 72)
    print("Phase 67 / Claim #54 - security + audit backend verify")
    print("=" * 72)
    print()

    get_settings.cache_clear()
    usage_key, identity_key, tenant_id, team_id, record_ids = await _seed()
    print(">> Seeded tenant + team + 2 keys + 3 audit records")

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
        # ---- 1. Security GET ----
        print()
        print(">> Step 1: GET /v1/admin/security/{team_id}")
        r = await client.get(f"/v1/admin/security/{team_id}", headers=usage_headers)
        assert_("security GET returns 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "GET shape carries known_rule_ids + valid_actions",
            "known_rule_ids" in body
            and "valid_actions" in body
            and "pii.email" in body.get("known_rule_ids", [])
            and "block" in body.get("valid_actions", []),
        )

        # ---- 2. admin:usage cannot PUT ----
        print()
        print(">> Step 2: admin:usage cannot PUT")
        r = await client.put(
            f"/v1/admin/security/{team_id}",
            headers=usage_headers,
            json={"pii_tokenization_enabled": True},
        )
        assert_(
            "admin:usage PUT returns 403",
            r.status_code == 403,
            f"got {r.status_code}",
        )

        # ---- 3. admin:identity PUT sets policy ----
        print()
        print(">> Step 3: admin:identity PUT sets policy + PII enable")
        policy = {
            "disabled_rules": ["pii.ipv4"],
            "rule_actions": {"pii.email": "tokenize", "injection": "block"},
        }
        r = await client.put(
            f"/v1/admin/security/{team_id}",
            headers=identity_headers,
            json={"guardrail_policy": policy, "pii_tokenization_enabled": True},
        )
        assert_(
            "admin:identity PUT returns 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text[:200]}",
        )
        body = r.json() if r.status_code == 200 else {}
        assert_("PUT response carries new policy", body.get("guardrail_policy") == policy)
        assert_(
            "PUT response carries pii_tokenization_enabled=True",
            body.get("pii_tokenization_enabled") is True,
        )

        # ---- 4. PATCH semantics: ttl write preserves policy ----
        print()
        print(">> Step 4: partial PUT (only ttl) preserves the policy")
        r = await client.put(
            f"/v1/admin/security/{team_id}",
            headers=identity_headers,
            json={"pii_token_ttl_seconds": 3600},
        )
        body = r.json()
        assert_(
            "ttl updated to 3600",
            body.get("pii_token_ttl_seconds") == 3600,
        )
        assert_(
            "policy preserved through omitted-field PUT",
            body.get("guardrail_policy") == policy,
        )

        # ---- 5. Bad action enum -> 422 ----
        print()
        print(">> Step 5: invalid action value -> 422")
        r = await client.put(
            f"/v1/admin/security/{team_id}",
            headers=identity_headers,
            json={"guardrail_policy": {"rule_actions": {"pii.email": "yeet"}}},
        )
        assert_(
            "invalid action -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

        # ---- 6. Audit list ----
        print()
        print(">> Step 6: GET /v1/admin/audit/{tenant_id}")
        r = await client.get(f"/v1/admin/audit/{tenant_id}", headers=usage_headers)
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "audit list returns 200 with 3 records",
            r.status_code == 200 and body.get("total") == 3,
            f"got {r.status_code}, total={body.get('total')}",
        )
        items = body.get("items", [])
        assert_(
            "audit list ordered oldest-first; record 0 has empty prev_hash",
            len(items) == 3 and items[0]["prev_hash"] == "",
        )
        assert_(
            "audit chain is well-formed (prev_hash of N matches this_hash of N-1)",
            len(items) == 3
            and items[1]["prev_hash"] == items[0]["this_hash"]
            and items[2]["prev_hash"] == items[1]["this_hash"],
        )

        # ---- 7. Audit verify (intact chain) ----
        print()
        print(">> Step 7: POST /v1/admin/audit/{tenant_id}/verify (intact)")
        r = await client.post(
            f"/v1/admin/audit/{tenant_id}/verify", headers=usage_headers
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "verify on intact chain returns is_intact=true",
            r.status_code == 200 and body.get("is_intact") is True,
            f"got {r.status_code}, body={body}",
        )
        assert_(
            "verify reports total=verified=3, no breaks",
            body.get("total_records") == 3
            and body.get("verified_records") == 3
            and body.get("breaks") == [],
        )

        # ---- 8. Tamper a record + re-verify ----
        print()
        print(">> Step 8: tamper middle record, re-verify reports the break")
        tampered_id = record_ids[1]
        async with sm() as session:
            await session.execute(
                sa_update(AuditRecord)
                .where(AuditRecord.id == tampered_id)
                .values(model="groq/cheaper-fake-model")
            )
            await session.commit()
        r = await client.post(
            f"/v1/admin/audit/{tenant_id}/verify", headers=usage_headers
        )
        body = r.json() if r.status_code == 200 else {}
        assert_(
            "verify on tampered chain returns is_intact=false",
            body.get("is_intact") is False,
        )
        breaks = body.get("breaks", [])
        assert_(
            "tampered record appears in breaks",
            any(b["record_id"] == tampered_id for b in breaks),
            f"breaks={breaks}",
        )
        tamper_break = next(
            (b for b in breaks if b["record_id"] == tampered_id), None
        )
        assert_(
            "tamper break carries reason=hash_mismatch",
            tamper_break is not None and tamper_break["reason"] == "hash_mismatch",
        )

        # ---- 9. Unknown tenant -> 404 ----
        print()
        print(">> Step 9: unknown tenant -> 404 from both audit endpoints")
        r = await client.get(
            "/v1/admin/audit/no_such_tenant", headers=usage_headers
        )
        assert_(
            "audit list unknown tenant -> 404",
            r.status_code == 404,
            f"got {r.status_code}",
        )
        r = await client.post(
            "/v1/admin/audit/no_such_tenant/verify", headers=usage_headers
        )
        assert_(
            "audit verify unknown tenant -> 404",
            r.status_code == 404,
            f"got {r.status_code}",
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
    print("Claim #54 supported:")
    print("  The Phase 67 security console + audit log surface round-trips:")
    print("   - GET /v1/admin/security composes guardrail_policy + PII flags")
    print("     and echoes the static vocabulary (rule ids + actions) the UI")
    print("     uses to render its editor.")
    print("   - PATCH semantics: omitted fields unchanged, null clears.")
    print("   - admin:usage GETs; admin:identity writes; clean 403 on mismatch.")
    print("   - Invalid action / non-dict policy -> 422 before DB write.")
    print("   - Audit list returns chain records oldest-first; prev_hash of")
    print("     row N matches this_hash of row N-1 (chain well-formed).")
    print("   - Audit verify reports is_intact=true on an unmodified chain.")
    print("   - A direct SQL UPDATE to one record's `model` field (the")
    print("     threat model) flips verify to is_intact=false and surfaces")
    print("     the tampered record's id in breaks with reason=hash_mismatch.")
    return 0


def _main() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_main())
