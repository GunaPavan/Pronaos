"""HTTP-level tests for the Phase 66 composed routing endpoint.

Covers
------
- GET /v1/admin/routing/{team_id} returns every routing-related column.
- GET 404s on unknown team with a clear ``team_not_found`` detail.
- PUT updates strategy, allowlist, thresholds, scores.
- PUT null clears; PUT omitted is unchanged (model_fields_set semantics).
- PUT rejects invalid strategy enum (422); invalid score-dict shape (422);
  out-of-range thresholds (422).
- Scope split: GET requires admin:usage, PUT requires admin:identity.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from pronaos.db.models import ApiKey, Team


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes))
        await session.commit()


async def _set_team_routing_strategy(  # type: ignore[no-untyped-def]
    sm, team_id: str, strategy: str | None
) -> None:
    async with sm() as session:
        await session.execute(
            update(Team).where(Team.id == team_id).values(routing_strategy=strategy)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# GET                                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_get_returns_full_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        f"/v1/admin/routing/{auth_setup.team_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    expected = {
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
    assert set(body.keys()) == expected
    # All defaults NULL for a freshly seeded team.
    assert body["routing_strategy"] is None
    assert body["allowed_models"] is None
    assert body["quality_scores"] is None


@pytest.mark.asyncio
async def test_routing_get_404_for_unknown_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/routing/does_not_exist",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "team_not_found"


# --------------------------------------------------------------------------- #
# PUT — strategy + simple fields                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_put_sets_strategy(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"routing_strategy": "quality-aware-cheapest"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["routing_strategy"] == "quality-aware-cheapest"

    # Reload via GET; persisted.
    r = await auth_setup.client.get(
        f"/v1/admin/routing/{auth_setup.team_id}", headers=_auth(auth_setup.api_key)
    )
    assert r.json()["routing_strategy"] == "quality-aware-cheapest"


@pytest.mark.asyncio
async def test_routing_put_null_clears(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _set_team_routing_strategy(auth_setup.sm, auth_setup.team_id, "quality-aware-cheapest")
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"routing_strategy": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["routing_strategy"] is None


@pytest.mark.asyncio
async def test_routing_put_partial_preserves_untouched(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Setting one field shouldn't clobber another."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    # First write: strategy + threshold.
    await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={
            "routing_strategy": "quality-aware-cheapest",
            "quality_threshold": 0.8,
        },
        headers=_auth(auth_setup.api_key),
    )
    # Second write: only strategy.
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"routing_strategy": "cheapest"},
        headers=_auth(auth_setup.api_key),
    )
    body = r.json()
    assert body["routing_strategy"] == "cheapest"
    # quality_threshold should be UNCHANGED, not nulled.
    assert body["quality_threshold"] == 0.8


@pytest.mark.asyncio
async def test_routing_put_invalid_strategy_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"routing_strategy": "not-a-real-strategy"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert "invalid routing_strategy" in r.text.lower()


@pytest.mark.asyncio
async def test_routing_put_out_of_range_threshold_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"quality_threshold": 1.5},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# PUT — score dicts                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_put_quality_scores(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    scores = {
        "groq/llama-3.1-8b-instant": {"score": 0.4, "n_samples": 8},
        "groq/llama-3.3-70b-versatile": {
            "score": 0.95,
            "n_samples": 12,
            "source_eval_id": "basic-2026-05-01",
        },
    }
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"quality_scores": scores},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    persisted = r.json()["quality_scores"]
    assert persisted is not None
    assert persisted["groq/llama-3.1-8b-instant"]["score"] == 0.4
    assert persisted["groq/llama-3.3-70b-versatile"]["source_eval_id"] == "basic-2026-05-01"


@pytest.mark.asyncio
async def test_routing_put_invalid_score_shape_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A score dict missing the inner 'score' field must 422."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        # Missing inner 'score' key.
        json={"quality_scores": {"groq/llama-3.1-8b-instant": {"n_samples": 8}}},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert "score" in r.text.lower()


# --------------------------------------------------------------------------- #
# PUT — allowlist                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_put_allowed_models(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"allowed_models": ["groq/llama-3.1-8b-instant"]},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed_models"] == ["groq/llama-3.1-8b-instant"]

    # Empty list is distinct from null — "no models allowed" vs "no allowlist".
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"allowed_models": []},
        headers=_auth(auth_setup.api_key),
    )
    assert r.json()["allowed_models"] == []

    # Null clears back to "no allowlist".
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"allowed_models": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.json()["allowed_models"] is None


# --------------------------------------------------------------------------- #
# Scope enforcement                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_get_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Default seeded key has only chat:write — GET must 403."""
    r = await auth_setup.client.get(
        f"/v1/admin/routing/{auth_setup.team_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_routing_put_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """admin:usage alone is NOT enough to write — must have admin:identity."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={"routing_strategy": "cheapest"},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# Reasoning + prompt-cache thresholds                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_put_all_thresholds(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/routing/{auth_setup.team_id}",
        json={
            "quality_threshold": 0.75,
            "tool_use_threshold": 0.92,
            "prompt_cache_min_samples": 50,
            "prompt_cache_min_hit_rate": 0.15,
            "reasoning_aware_min_samples": 30,
            "reasoning_aware_max_ratio": 0.8,
        },
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quality_threshold"] == 0.75
    assert body["tool_use_threshold"] == 0.92
    assert body["prompt_cache_min_samples"] == 50
    assert body["prompt_cache_min_hit_rate"] == 0.15
    assert body["reasoning_aware_min_samples"] == 30
    assert body["reasoning_aware_max_ratio"] == 0.8
