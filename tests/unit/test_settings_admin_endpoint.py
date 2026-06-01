"""HTTP-level tests for the Phase 71 settings + OIDC subject endpoints.

Covers
------
- GET /v1/admin/settings returns the sanitised config shape.
- No API keys / secrets leaked in the response.
- Scope gate: requires admin:usage.

- PATCH /v1/admin/tenants/{id} now accepts oidc_subject.
- Setting oidc_subject persists; null clears; empty string clears.
- Omitting oidc_subject leaves the existing value unchanged (model_fields_set).
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from pronaos.db.models import ApiKey


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Settings GET                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_get_returns_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/settings", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    expected = {
        "redis_configured",
        "semantic_cache_enabled",
        "anthropic_configured",
        "groq_configured",
        "openai_configured",
        "bedrock_configured",
        "vertex_configured",
        "mcp_enabled",
        "presidio_enabled",
        "singleflight_distributed",
        "oidc_configured",
        "oidc_issuer",
        "database_scheme",
    }
    assert set(body.keys()) == expected


@pytest.mark.asyncio
async def test_settings_no_secrets_in_response(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/settings", headers=_auth(auth_setup.api_key)
    )
    body = r.json()
    # groq and anthropic keys are set in conftest — response must NOT
    # include them (only configured=true/false).
    raw = str(body)
    assert "test-key-for-tests" not in raw
    assert "AKIAIOSFODNN7EXAMPLE" not in raw  # AWS key from conftest


@pytest.mark.asyncio
async def test_settings_reflects_configured_providers(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """conftest sets GROQ + ANTHROPIC + AWS env vars → those must be True."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/settings", headers=_auth(auth_setup.api_key)
    )
    body = r.json()
    # Both GROQ_API_KEY and ANTHROPIC_API_KEY set in conftest.
    assert body["groq_configured"] is True
    assert body["anthropic_configured"] is True
    # database_scheme present (SQLite in tests).
    assert body["database_scheme"] is not None


@pytest.mark.asyncio
async def test_settings_get_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    r = await auth_setup.client.get(
        "/v1/admin/settings", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# OIDC subject PATCH (Phase 71 extension to identity PATCH)                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_identity_patch_sets_oidc_subject(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    r = await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": "auth0|abc123"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["oidc_subject"] == "auth0|abc123"


@pytest.mark.asyncio
async def test_identity_patch_null_clears_oidc_subject(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    # First set it.
    await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": "auth0|abc123"},
        headers=_auth(auth_setup.api_key),
    )
    # Clear with explicit null.
    r = await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.json()["oidc_subject"] is None


@pytest.mark.asyncio
async def test_identity_patch_omitting_oidc_subject_preserves_it(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Omitting oidc_subject from the PATCH body must not clobber the existing value."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": "auth0|preserve_me"},
        headers=_auth(auth_setup.api_key),
    )
    # PATCH only the name.
    r = await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"name": "new-name"},
        headers=_auth(auth_setup.api_key),
    )
    body = r.json()
    assert body["name"] == "new-name"
    assert body["oidc_subject"] == "auth0|preserve_me"


@pytest.mark.asyncio
async def test_identity_patch_empty_string_clears_oidc_subject(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": "auth0|abc123"},
        headers=_auth(auth_setup.api_key),
    )
    r = await auth_setup.client.patch(
        f"/v1/admin/tenants/{auth_setup.tenant_id}",
        json={"oidc_subject": ""},
        headers=_auth(auth_setup.api_key),
    )
    assert r.json()["oidc_subject"] is None
