"""HTTP-level tests for the Phase 67 security + audit endpoints.

Covers
------
- GET /v1/admin/security/{team_id} returns the composed shape +
  static vocabulary (known_rule_ids, valid_actions).
- PUT updates guardrail_policy + pii fields with PATCH semantics
  (omitted unchanged, null clears for guardrail_policy).
- PUT rejects malformed policy (bad action, non-string rule key,
  wrong types) with 422.
- Scope split: GET requires admin:usage, PUT requires admin:identity.
- Audit list: pagination + team_id filter + 404 on unknown tenant.
- Audit verify: returns is_intact=true for an unbroken chain;
  returns is_intact=false with the right break record_id after a
  tamper.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from pronaos.audit.logger import AuditLogger
from pronaos.db.models import ApiKey, AuditRecord


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Security GET                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_security_get_returns_composed_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        f"/v1/admin/security/{auth_setup.team_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "team_id",
        "guardrail_policy",
        "pii_tokenization_enabled",
        "pii_token_ttl_seconds",
        "known_rule_ids",
        "valid_actions",
    }
    # Defaults on a freshly seeded team.
    assert body["guardrail_policy"] is None
    assert body["pii_tokenization_enabled"] is False
    # Vocabulary echoed back.
    for rule in ("pii.email", "pii.ssn", "injection", "presidio", "llama_guard"):
        assert rule in body["known_rule_ids"]
    assert set(body["valid_actions"]) >= {"block", "redact", "tokenize", "log_only"}


@pytest.mark.asyncio
async def test_security_get_404_for_unknown_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/security/no_such_team",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "team_not_found"


# --------------------------------------------------------------------------- #
# Security PUT                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_security_put_updates_policy(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity")
    policy = {
        "disabled_rules": ["pii.ipv4"],
        "rule_actions": {"pii.email": "redact", "injection": "block"},
    }
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": policy, "pii_tokenization_enabled": True},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["guardrail_policy"] == policy
    assert body["pii_tokenization_enabled"] is True


@pytest.mark.asyncio
async def test_security_put_partial_preserves_untouched(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Setting pii_token_ttl_seconds shouldn't clobber guardrail_policy."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity")
    await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": {"disabled_rules": ["pii.ssn"]}},
        headers=_auth(auth_setup.api_key),
    )
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"pii_token_ttl_seconds": 86400},
        headers=_auth(auth_setup.api_key),
    )
    body = r.json()
    assert body["pii_token_ttl_seconds"] == 86400
    # guardrail_policy unchanged.
    assert body["guardrail_policy"] == {"disabled_rules": ["pii.ssn"]}


@pytest.mark.asyncio
async def test_security_put_null_clears_policy(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity")
    # Seed a policy first.
    await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": {"disabled_rules": ["pii.ssn"]}},
        headers=_auth(auth_setup.api_key),
    )
    # Now clear via null.
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.json()["guardrail_policy"] is None


@pytest.mark.asyncio
async def test_security_put_rejects_invalid_action(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity")
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": {"rule_actions": {"pii.email": "yeet"}}},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert "yeet" in r.text


@pytest.mark.asyncio
async def test_security_put_rejects_non_dict_policy(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage admin:identity")
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": "not-an-object"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Scope enforcement                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_security_get_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    # Default seeded key only has chat:write.
    r = await auth_setup.client.get(
        f"/v1/admin/security/{auth_setup.team_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_security_put_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/security/{auth_setup.team_id}",
        json={"guardrail_policy": {"disabled_rules": []}},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Audit log — list                                                             #
# --------------------------------------------------------------------------- #


async def _seed_audit_records(sm, tenant_id: str, team_id: str, n: int = 3) -> list[AuditRecord]:  # type: ignore[no-untyped-def]
    """Use the real AuditLogger so the chain is well-formed."""
    logger = AuditLogger()
    records: list[AuditRecord] = []
    async with sm() as session:
        for i in range(n):
            rec = await logger.append(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                key_id="bootstrap",
                provider="groq",
                model="llama-3.1-8b-instant",
                request_body={"messages": [{"role": "user", "content": f"hi {i}"}]},
                response_body={"choices": [{"message": {"content": f"hi back {i}"}}]},
                request_id=f"req_{i}",
            )
            assert rec is not None
            records.append(rec)
        await session.commit()
    return records


@pytest.mark.asyncio
async def test_audit_list_returns_seeded_records(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_audit_records(auth_setup.sm, auth_setup.tenant_id, auth_setup.team_id, n=3)

    r = await auth_setup.client.get(
        f"/v1/admin/audit/{auth_setup.tenant_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Ordered oldest-first.
    assert body["items"][0]["prev_hash"] == ""
    assert body["items"][1]["prev_hash"] == body["items"][0]["this_hash"]


@pytest.mark.asyncio
async def test_audit_list_paginates(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_audit_records(auth_setup.sm, auth_setup.tenant_id, auth_setup.team_id, n=5)

    r = await auth_setup.client.get(
        f"/v1/admin/audit/{auth_setup.tenant_id}?limit=2&offset=2",
        headers=_auth(auth_setup.api_key),
    )
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 2


@pytest.mark.asyncio
async def test_audit_list_404_unknown_tenant(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/audit/no_such_tenant",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_audit_list_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    r = await auth_setup.client.get(
        f"/v1/admin/audit/{auth_setup.tenant_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Audit verify                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_verify_intact_chain(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_audit_records(auth_setup.sm, auth_setup.tenant_id, auth_setup.team_id, n=4)

    r = await auth_setup.client.post(
        f"/v1/admin/audit/{auth_setup.tenant_id}/verify",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_intact"] is True
    assert body["total_records"] == 4
    assert body["verified_records"] == 4
    assert body["breaks"] == []


@pytest.mark.asyncio
async def test_audit_verify_detects_tampered_record(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Tamper a stored field on a middle record; verifier should detect
    a hash_mismatch on that record (and a prev_hash_mismatch cascade
    on the next one — but we only assert the FIRST break carries the
    tampered record's id)."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    records = await _seed_audit_records(
        auth_setup.sm, auth_setup.tenant_id, auth_setup.team_id, n=3
    )

    # Mutate the model field on the middle record. Note: changing the
    # `model` field of an AuditRecord is exactly what an attacker
    # trying to retroactively re-label a call as a cheaper model would
    # do — this is the threat model.
    tampered_id = records[1].id
    async with auth_setup.sm() as session:
        await session.execute(
            update(AuditRecord)
            .where(AuditRecord.id == tampered_id)
            .values(model="groq/cheap-fake-model")
        )
        await session.commit()

    r = await auth_setup.client.post(
        f"/v1/admin/audit/{auth_setup.tenant_id}/verify",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_intact"] is False
    # The middle record's hash no longer matches; verifier flags it
    # plus the cascading prev_hash_mismatch on record 3.
    assert len(body["breaks"]) >= 1
    tampered_break = next(
        (b for b in body["breaks"] if b["record_id"] == tampered_id), None
    )
    assert tampered_break is not None
    assert tampered_break["reason"] == "hash_mismatch"
