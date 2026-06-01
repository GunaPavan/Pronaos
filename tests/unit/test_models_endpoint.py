"""HTTP-level tests for the Phase 65 models endpoint.

Covers
------
- GET /v1/admin/models returns the catalog of routable chat models
  with pricing + capability + configured + allowed flags.
- The endpoint requires admin:usage; a key with only chat:write
  receives a 403 with the standard ``missing required scope`` detail.
- Anthropic native models surface even though anthropic isn't in
  the catalog dict (the endpoint composes anthropic._PRICING).
- Team.allowed_models, when set, flips ``allowed`` to false on
  every fqmn outside the allowlist.
- ``provider_configured`` mirrors the registry's available_keys
  view (e.g. Cohere is in the catalog but no API key in the test
  env → configured=false; groq + anthropic + bedrock are seeded
  in conftest → configured=true).
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from pronaos.db.models import ApiKey, Team


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes)
        )
        await session.commit()


async def _set_allowed_models(  # type: ignore[no-untyped-def]
    sm, team_id: str, allowed: list[str] | None
) -> None:
    async with sm() as session:
        await session.execute(
            update(Team).where(Team.id == team_id).values(allowed_models=allowed)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Shape + scope                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_models_endpoint_returns_catalog_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    assert len(body["items"]) > 0
    # Every row carries the full ModelInfo shape.
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
    for row in body["items"]:
        assert set(row.keys()) == expected_keys, row


@pytest.mark.asyncio
async def test_models_endpoint_requires_admin_usage_scope(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """Default seeded key only has chat:write — must 403."""
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    # Match the standard scope-missing detail shape used by other endpoints.
    if isinstance(detail, dict):
        assert "admin:usage" in (detail.get("hint") or "")
    else:
        assert "admin:usage" in detail


# --------------------------------------------------------------------------- #
# Catalog composition                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_models_endpoint_includes_anthropic_native(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Anthropic isn't a catalog entry but its three models must still
    appear, populated from the native adapter's pricing dict."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    fqmns = {row["fqmn"] for row in r.json()["items"]}
    assert "anthropic/claude-opus-4-7" in fqmns
    assert "anthropic/claude-sonnet-4-6" in fqmns
    assert "anthropic/claude-haiku-4-5" in fqmns


@pytest.mark.asyncio
async def test_models_endpoint_includes_groq_catalog(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Groq catalog models with capability flags carry through unchanged."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    items = {row["fqmn"]: row for row in r.json()["items"]}
    llama8b = items.get("groq/llama-3.1-8b-instant")
    assert llama8b is not None
    assert llama8b["provider"] == "groq"
    assert llama8b["supports_tools"] is True
    assert llama8b["supports_streaming"] is True
    # Llama 3.1 8B is text-only.
    assert llama8b["supports_vision"] is False
    assert llama8b["max_context_tokens"] == 128_000


# --------------------------------------------------------------------------- #
# Configured flag                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_models_endpoint_marks_provider_configured(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """conftest seeds GROQ + ANTHROPIC + AWS env vars → those provider
    rows should report configured=true. Cohere has no key in the test
    env → its rows must report configured=false."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    rows = r.json()["items"]
    by_provider = {row["provider"] for row in rows}
    assert "groq" in by_provider
    assert "anthropic" in by_provider

    groq_rows = [row for row in rows if row["provider"] == "groq"]
    assert groq_rows and all(row["provider_configured"] for row in groq_rows)

    cohere_rows = [row for row in rows if row["provider"] == "cohere"]
    if cohere_rows:  # Cohere only appears if it has chat models in catalog
        assert all(not row["provider_configured"] for row in cohere_rows)


# --------------------------------------------------------------------------- #
# Allowlist flag                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_models_endpoint_no_allowlist_marks_everything_allowed(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """Team.allowed_models is NULL by default — every fqmn must report
    allowed=true."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    assert all(row["allowed"] for row in r.json()["items"])


@pytest.mark.asyncio
async def test_models_endpoint_respects_team_allowlist(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Setting allowed_models = [X] makes only X's fqmn report
    allowed=true; every other row must report allowed=false."""
    await _set_allowed_models(
        auth_setup.sm,
        auth_setup.team_id,
        ["groq/llama-3.1-8b-instant"],
    )
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    rows = r.json()["items"]
    allowed_rows = [row for row in rows if row["allowed"]]
    assert len(allowed_rows) == 1
    assert allowed_rows[0]["fqmn"] == "groq/llama-3.1-8b-instant"


# --------------------------------------------------------------------------- #
# Sort order                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_models_endpoint_sorts_allowed_configured_first(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """The endpoint sorts by (allowed && configured) first, then allowed-
    but-unconfigured, then disallowed. Inside each bucket items are
    alphabetical so the UI dropdown reads sensibly."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/models", headers=_auth(auth_setup.api_key)
    )
    items = r.json()["items"]

    # Find the index where ``allowed && configured`` ends. Every item up
    # to that index must satisfy the bucket invariant, and every item
    # after must NOT.
    def _bucket(row: dict[str, object]) -> int:
        if row["allowed"] and row["provider_configured"]:
            return 0
        if row["allowed"]:
            return 1
        return 2

    buckets = [_bucket(it) for it in items]
    assert buckets == sorted(buckets), "items must be sorted by bucket"

    # Inside each bucket the fqmns are alphabetical.
    for bucket_id in (0, 1, 2):
        bucket_fqmns = [
            it["fqmn"] for it, b in zip(items, buckets, strict=True) if b == bucket_id
        ]
        assert bucket_fqmns == sorted(bucket_fqmns), (
            f"bucket {bucket_id} must be alphabetical"
        )
