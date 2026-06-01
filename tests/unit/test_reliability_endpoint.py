"""HTTP-level tests for the Phase 68 reliability + doctor endpoints.

Covers
------
- GET /v1/admin/providers returns the catalog with circuit state +
  configured flag.
- Configured providers sort before unconfigured.
- Anthropic native is present even though it has no CATALOG entry.
- POST /v1/admin/providers/{name}/reset-breaker resets the state to
  CLOSED and 404s on unknown providers.
- Scope split: GET on admin:usage; reset on admin:identity.
- GET /v1/admin/doctor returns the gate report with the summary
  counts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.db.models import ApiKey


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes))
        await session.commit()


async def _install_circuit_registry(client) -> CircuitBreakerRegistry:  # type: ignore[no-untyped-def]
    """Install a fresh CircuitBreakerRegistry on the in-process app.

    Returns the registry so tests can directly mutate breaker state
    (trip / reset) without going through the failover layer.
    """
    # httpx.AsyncClient wraps an ASGITransport that holds a reference
    # to the FastAPI app. ``transport.app`` is the same FastAPI
    # instance we configured in conftest.
    app = client._transport.app  # type: ignore[attr-defined]
    registry = CircuitBreakerRegistry()
    app.state.circuit_registry = registry
    return registry


# --------------------------------------------------------------------------- #
# Providers GET                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_providers_list_returns_catalog_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get("/v1/admin/providers", headers=_auth(auth_setup.api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    assert len(body["items"]) > 0
    expected = {
        "name",
        "configured",
        "model_count",
        "typical_p50_ms",
        "circuit_state",
        "notes",
    }
    for row in body["items"]:
        assert set(row.keys()) == expected
        # Every row defaults to "closed" since no breaker has been
        # created yet (no chat traffic in this test).
        assert row["circuit_state"] == "closed"


@pytest.mark.asyncio
async def test_providers_includes_anthropic_native(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get("/v1/admin/providers", headers=_auth(auth_setup.api_key))
    names = {row["name"] for row in r.json()["items"]}
    assert "anthropic" in names
    assert "groq" in names


@pytest.mark.asyncio
async def test_providers_sort_configured_first(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get("/v1/admin/providers", headers=_auth(auth_setup.api_key))
    items = r.json()["items"]
    configured_flags = [row["configured"] for row in items]
    # Once we hit the first False, every subsequent row must be False too.
    seen_unconfigured = False
    for is_configured in configured_flags:
        if not is_configured:
            seen_unconfigured = True
        elif seen_unconfigured:
            pytest.fail("configured providers must sort before unconfigured")


@pytest.mark.asyncio
async def test_providers_surfaces_live_circuit_state(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    registry = await _install_circuit_registry(auth_setup.client)
    # Trip the groq breaker by recording enough failures.
    breaker = registry.get("groq")
    for _ in range(10):  # well above default threshold (5)
        breaker.record_failure()
    assert breaker.state.value == "open"

    r = await auth_setup.client.get("/v1/admin/providers", headers=_auth(auth_setup.api_key))
    groq_row = next(row for row in r.json()["items"] if row["name"] == "groq")
    assert groq_row["circuit_state"] == "open"


@pytest.mark.asyncio
async def test_providers_get_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    # Default key only has chat:write.
    r = await auth_setup.client.get("/v1/admin/providers", headers=_auth(auth_setup.api_key))
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Reset breaker                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reset_breaker_flips_state_to_closed(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    registry = await _install_circuit_registry(auth_setup.client)
    breaker = registry.get("groq")
    for _ in range(10):
        breaker.record_failure()
    assert breaker.state.value == "open"

    r = await auth_setup.client.post(
        "/v1/admin/providers/groq/reset-breaker",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "groq", "circuit_state": "closed"}
    assert breaker.state.value == "closed"


@pytest.mark.asyncio
async def test_reset_breaker_404s_on_unknown_provider(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    r = await auth_setup.client.post(
        "/v1/admin/providers/not-a-provider/reset-breaker",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "provider_not_found"


@pytest.mark.asyncio
async def test_reset_breaker_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """admin:usage alone is not enough to reset a breaker."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.post(
        "/v1/admin/providers/groq/reset-breaker",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Doctor                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_doctor_returns_gate_report(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get("/v1/admin/doctor", headers=_auth(auth_setup.api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"gates", "summary", "has_fail", "has_warn"}
    assert isinstance(body["gates"], list) and len(body["gates"]) > 0
    summary = body["summary"]
    assert set(summary.keys()) == {"total", "passed", "failed", "warn", "skip"}
    # Summary counts add up to the gate count.
    assert (
        summary["passed"] + summary["failed"] + summary["warn"] + summary["skip"]
        == summary["total"]
    )
    assert summary["total"] == len(body["gates"])
    # Every gate has the expected shape.
    for g in body["gates"]:
        assert set(g.keys()) == {"name", "verdict", "detail"}
        assert g["verdict"] in {"PASS", "FAIL", "WARN", "SKIP"}


@pytest.mark.asyncio
async def test_doctor_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    r = await auth_setup.client.get("/v1/admin/doctor", headers=_auth(auth_setup.api_key))
    assert r.status_code == 403
