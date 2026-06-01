"""HTTP-level tests for the Phase 59 /v1/batches surface.

Exercises auth gate -> per-team gate -> Pydantic validation ->
provider client (mocked via respx) -> DB row write. The provider
batch clients themselves are unit-tested in test_batches.py; here
we verify the endpoint glue.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from pronaos.db.models import Batch, Team


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_batches(sm, team_id: str) -> None:  # type: ignore[no-untyped-def]
    """Flip the team's batches_enabled bit ON, bypassing the CLI/admin."""
    async with sm() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        team.batches_enabled = True
        await session.commit()


# --------------------------------------------------------------------------- #
# Per-team gate                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_batches_disabled_by_default(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {"model": "openai/gpt-4o-mini", "messages": []},
                }
            ]
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == "batches_disabled"


@pytest.mark.asyncio
async def test_get_batch_disabled_for_unenabled_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.get("/v1/batches/some_id", headers=_auth(auth_setup.api_key))
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batches_disabled"


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mixed_providers_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {"model": "openai/gpt-4o-mini", "messages": []},
                },
                {
                    "custom_id": "r2",
                    "body": {"model": "anthropic/claude-opus-4-7", "messages": []},
                },
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batch_mixed_providers"


@pytest.mark.asyncio
async def test_unsupported_provider_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {
                        "model": "groq/llama-3.3-70b-versatile",
                        "messages": [],
                    },
                }
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batch_unsupported_provider"


@pytest.mark.asyncio
async def test_missing_model_field_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "requests": [
                {"custom_id": "r1", "body": {"messages": []}},
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batch_missing_model"


@pytest.mark.asyncio
async def test_unsupported_endpoint_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Phase 60 widens the endpoint gate to chat + embeddings.
    Anything outside that set still 422s — this asserts on a
    speculative future endpoint to keep the regression honest."""
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "endpoint": "/v1/audio/transcriptions",
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {"model": "openai/whisper-1"},
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batch_endpoint_unsupported"


# --------------------------------------------------------------------------- #
# Submit (mocked OpenAI Files + Batches APIs)                                 #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_submit_openai_persists_row_and_returns_id(  # type: ignore[no-untyped-def]
    auth_setup, monkeypatch
) -> None:
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    # Tests run with anthropic key set but no openai key; inject one
    # so _make_client doesn't 503.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from pronaos.config import get_settings

    get_settings.cache_clear()

    respx.post("https://api.openai.com/v1/files").mock(
        return_value=httpx.Response(200, json={"id": "file-abc"})
    )
    respx.post("https://api.openai.com/v1/batches").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "batch_xyz",
                "status": "validating",
                "object": "batch",
            },
        )
    )

    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {
                        "model": "openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["provider_batch_id"] == "batch_xyz"
    assert body["status"] == "validating"
    assert body["request_counts"]["total"] == 1
    assert body["id"].startswith("pron_batch_")
    # Headers stamped.
    assert resp.headers.get("X-Pronaos-Batch-Provider") == "openai"
    assert resp.headers.get("X-Pronaos-Batch-Id") == body["id"]

    # Row persisted.
    async with auth_setup.sm() as session:
        rows = (await session.execute(select(Batch))).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == auth_setup.team_id
        assert rows[0].tenant_id == auth_setup.tenant_id
        assert rows[0].input_payload  # JSONL serialised


# --------------------------------------------------------------------------- #
# GET + results + cancel                                                      #
# --------------------------------------------------------------------------- #


async def _seed_batch(
    sm,  # type: ignore[no-untyped-def]
    *,
    tenant_id: str,
    team_id: str,
    status: str,
    output_payload: str = "",
) -> str:
    from datetime import UTC, datetime

    async with sm() as session:
        batch = Batch(
            id="pron_batch_seed_001",
            tenant_id=tenant_id,
            team_id=team_id,
            key_id="kid",
            provider="openai",
            provider_batch_id="batch_xyz",
            status=status,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            request_count=1,
            completed_count=1 if status == "completed" else 0,
            failed_count=0,
            prompt_tokens=10,
            completion_tokens=5,
            cost_hcents=0,
            created_at=datetime.now(UTC),
            input_payload="{}\n",
            output_payload=output_payload,
        )
        session.add(batch)
        await session.commit()
        return batch.id


@pytest.mark.asyncio
async def test_get_batch_returns_seeded_row(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="in_progress",
    )

    resp = await auth_setup.client.get(f"/v1/batches/{bid}", headers=_auth(auth_setup.api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == bid
    assert body["status"] == "in_progress"


@pytest.mark.asyncio
async def test_get_batch_404_for_unknown_id(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.get(
        "/v1/batches/pron_batch_no_such",
        headers=_auth(auth_setup.api_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_batch_404_for_other_team(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Tenant isolation: a caller from a different team gets 404,
    not 403 (avoid leaking existence)."""
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id="other-tenant",
        team_id="other-team",
        status="in_progress",
    )
    resp = await auth_setup.client.get(f"/v1/batches/{bid}", headers=_auth(auth_setup.api_key))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_results_409_when_not_completed(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="in_progress",
    )
    resp = await auth_setup.client.get(
        f"/v1/batches/{bid}/results", headers=_auth(auth_setup.api_key)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["type"] == "batch_not_completed"


@pytest.mark.asyncio
async def test_results_returns_jsonl_when_completed(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    jsonl = '{"custom_id":"r1","response":{"body":{"usage":{}}}}\n'
    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="completed",
        output_payload=jsonl,
    )
    resp = await auth_setup.client.get(
        f"/v1/batches/{bid}/results", headers=_auth(auth_setup.api_key)
    )
    assert resp.status_code == 200
    assert resp.text == jsonl
    assert resp.headers.get("content-type", "").startswith("application/jsonl")


@pytest.mark.asyncio
async def test_cancel_terminal_is_idempotent(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="completed",
    )
    resp = await auth_setup.client.post(
        f"/v1/batches/{bid}/cancel", headers=_auth(auth_setup.api_key)
    )
    assert resp.status_code == 200
    # Status unchanged.
    assert resp.json()["status"] == "completed"


@respx.mock
@pytest.mark.asyncio
async def test_cancel_in_flight_calls_provider(  # type: ignore[no-untyped-def]
    auth_setup, monkeypatch
) -> None:
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from pronaos.config import get_settings

    get_settings.cache_clear()

    bid = await _seed_batch(
        auth_setup.sm,
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="in_progress",
    )
    cancel_route = respx.post("https://api.openai.com/v1/batches/batch_xyz/cancel").mock(
        return_value=httpx.Response(200, json={"id": "batch_xyz", "status": "cancelling"})
    )
    resp = await auth_setup.client.post(
        f"/v1/batches/{bid}/cancel", headers=_auth(auth_setup.api_key)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert cancel_route.called
