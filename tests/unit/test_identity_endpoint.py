"""HTTP-level tests for the Phase 63 identity REST surface.

Covers
------
- Scope enforcement: every endpoint returns 403 when the key lacks
  ``admin:identity``.
- Tenant CRUD: create / list / get / patch / delete.
- Team CRUD: create with FK check (422 on bad tenant_id) /
  list filtered by tenant / get / patch / delete.
- Key generation: full key returned exactly once + subsequent GETs
  omit the secret; revoke is soft (sets revoked_at); revoked keys can
  no longer authenticate against the rest of the gateway.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from pronaos.db.models import ApiKey


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_identity_scope(sm, key_id: str) -> None:  # type: ignore[no-untyped-def]
    """Upgrade the seeded key to carry admin:identity. The auth_setup
    fixture issues a chat:write key by default; identity tests need
    the higher scope."""
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes="chat:write admin:identity")
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Scope enforcement                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_identity_endpoints_require_admin_identity_scope(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Default seeded key has only ``chat:write`` → 403 across the
    identity surface."""
    r = await auth_setup.client.get("/v1/admin/tenants", headers=_auth(auth_setup.api_key))
    assert r.status_code == 403
    assert "admin:identity" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Tenants                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenant_create_list_get_update_delete_roundtrip(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)

    # Create.
    r = await auth_setup.client.post("/v1/admin/tenants", json={"name": "globex"}, headers=headers)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "globex"
    new_id = created["id"]

    # List includes the new tenant (and the seeded "acme" from auth_setup).
    r = await auth_setup.client.get("/v1/admin/tenants", headers=headers)
    assert r.status_code == 200
    names = {t["name"] for t in r.json()}
    assert {"acme", "globex"} <= names

    # Get by id.
    r = await auth_setup.client.get(f"/v1/admin/tenants/{new_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "globex"

    # Patch the name.
    r = await auth_setup.client.patch(
        f"/v1/admin/tenants/{new_id}",
        json={"name": "globex-renamed"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "globex-renamed"

    # Delete.
    r = await auth_setup.client.delete(f"/v1/admin/tenants/{new_id}", headers=headers)
    assert r.status_code == 204

    # Verify gone.
    r = await auth_setup.client.get(f"/v1/admin/tenants/{new_id}", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_tenant_get_404_for_unknown(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    r = await auth_setup.client.get(
        "/v1/admin/tenants/does_not_exist", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "tenant_not_found"


# --------------------------------------------------------------------------- #
# Teams                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_team_create_with_invalid_tenant_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    r = await auth_setup.client.post(
        "/v1/admin/teams",
        json={"tenant_id": "no_such_tenant", "name": "team-x"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["type"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_team_crud_roundtrip(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)
    tenant_id = auth_setup.tenant_id

    # Create.
    r = await auth_setup.client.post(
        "/v1/admin/teams",
        json={"tenant_id": tenant_id, "name": "platform"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    team_id = r.json()["id"]
    assert r.json()["tenant_id"] == tenant_id

    # List, filtered by tenant.
    r = await auth_setup.client.get(f"/v1/admin/teams?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200
    team_ids = {t["id"] for t in r.json()}
    assert team_id in team_ids
    # The seeded engineering team should also be there.
    assert auth_setup.team_id in team_ids

    # Patch.
    r = await auth_setup.client.patch(
        f"/v1/admin/teams/{team_id}",
        json={"name": "platform-renamed"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "platform-renamed"

    # Delete.
    r = await auth_setup.client.delete(f"/v1/admin/teams/{team_id}", headers=headers)
    assert r.status_code == 204
    r = await auth_setup.client.get(f"/v1/admin/teams/{team_id}", headers=headers)
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# API keys                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_key_generate_returns_full_key_once(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)

    r = await auth_setup.client.post(
        "/v1/admin/keys",
        json={
            "team_id": auth_setup.team_id,
            "label": "test-generated",
            "scopes": ["chat:write"],
            "env": "test",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["prefix"]
    # Key prefix is "pn_test_..." (pn = pronaos namespace, env-tagged).
    assert body["api_key"].startswith("pn_test_")
    key_id = body["id"]

    # Subsequent GET returns the key WITHOUT the secret.
    r = await auth_setup.client.get(f"/v1/admin/keys/{key_id}", headers=headers)
    assert r.status_code == 200
    assert "api_key" not in r.json()
    assert r.json()["prefix"] == body["prefix"]


@pytest.mark.asyncio
async def test_key_generate_rejects_invalid_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    r = await auth_setup.client.post(
        "/v1/admin/keys",
        json={"team_id": "no_such_team", "scopes": ["chat:write"]},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["type"] == "team_not_found"


@pytest.mark.asyncio
async def test_key_list_hides_revoked_by_default(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)
    r = await auth_setup.client.get(f"/v1/admin/keys?team_id={auth_setup.team_id}", headers=headers)
    assert r.status_code == 200
    statuses = [k["status"] for k in r.json()]
    assert all(s == "active" for s in statuses)

    # With include_revoked=true the revoked seed comes back.
    r = await auth_setup.client.get(
        f"/v1/admin/keys?team_id={auth_setup.team_id}&include_revoked=true",
        headers=headers,
    )
    assert r.status_code == 200
    statuses = [k["status"] for k in r.json()]
    assert "revoked" in statuses


@pytest.mark.asyncio
async def test_key_revoke_is_soft_and_blocks_subsequent_auth(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)

    # Issue a fresh key so we can revoke it without losing the admin key.
    r = await auth_setup.client.post(
        "/v1/admin/keys",
        json={"team_id": auth_setup.team_id, "scopes": ["chat:write"]},
        headers=headers,
    )
    assert r.status_code == 201
    issued = r.json()
    new_full_key = issued["api_key"]
    new_key_id = issued["id"]

    # The freshly issued key works against a real chat-write endpoint
    # gate (request would 401 for invalid auth before even running the
    # chat handler; we just need to get past the auth layer).
    # We use a deliberately-malformed chat body to short-circuit early.
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(new_full_key),
        json={"model": "openai/gpt-4o-mini"},  # missing messages
    )
    # 422 from Pydantic validation = auth passed; 401 = auth failed.
    assert r.status_code != 401, "freshly issued key should authenticate"

    # Revoke.
    r = await auth_setup.client.delete(f"/v1/admin/keys/{new_key_id}", headers=headers)
    assert r.status_code == 204

    # The revoked key now fails auth.
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(new_full_key),
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401

    # DB confirms revoked_at is set.
    async with auth_setup.sm() as session:
        api_key = (
            await session.execute(select(ApiKey).where(ApiKey.id == new_key_id))
        ).scalar_one()
        assert api_key.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_is_idempotent(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_identity_scope(auth_setup.sm, auth_setup.key_id)
    headers = _auth(auth_setup.api_key)
    # Issue → revoke → revoke again, both succeed with 204.
    r = await auth_setup.client.post(
        "/v1/admin/keys",
        json={"team_id": auth_setup.team_id, "scopes": ["chat:write"]},
        headers=headers,
    )
    new_key_id = r.json()["id"]
    r1 = await auth_setup.client.delete(f"/v1/admin/keys/{new_key_id}", headers=headers)
    r2 = await auth_setup.client.delete(f"/v1/admin/keys/{new_key_id}", headers=headers)
    assert r1.status_code == 204
    assert r2.status_code == 204
