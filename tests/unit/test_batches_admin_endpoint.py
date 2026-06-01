"""HTTP-level tests for the Phase 69 admin-scoped batch console.

Covers
------
- GET /v1/admin/batches returns all teams' batches (paginated).
- Status + team_id filters narrow the list.
- Invalid status string → 422 with clear detail.
- GET /v1/admin/batches/{id} returns any team's batch; 404 on unknown.
- POST /v1/admin/batches/{id}/cancel flips to 'cancelled'; idempotent
  on terminal batches.
- Scope split: GETs require admin:usage; cancel requires admin:identity.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from pronaos.db.models import ApiKey, Batch


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes))
        await session.commit()


async def _seed_batch(  # type: ignore[no-untyped-def]
    sm,
    *,
    batch_id: str,
    tenant_id: str,
    team_id: str,
    status: str = "in_progress",
) -> str:
    async with sm() as session:
        session.add(
            Batch(
                id=batch_id,
                tenant_id=tenant_id,
                team_id=team_id,
                key_id="kid",
                provider="openai",
                provider_batch_id=f"upstream_{batch_id}",
                status=status,
                endpoint="/v1/chat/completions",
                completion_window="24h",
                request_count=5,
                completed_count=3 if status == "completed" else 0,
                failed_count=0,
                prompt_tokens=100,
                completion_tokens=50,
                cost_hcents=25,
                created_at=datetime.now(UTC),
                input_payload="{}\n",
                output_payload="" if status != "completed" else '{"result": "ok"}\n',
            )
        )
        await session.commit()
    return batch_id


# --------------------------------------------------------------------------- #
# List                                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_batch_list_returns_seeded_batches(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b001",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="in_progress",
    )

    r = await auth_setup.client.get("/v1/admin/batches", headers=_auth(auth_setup.api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "b001"
    assert body["items"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_admin_batch_list_status_filter(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b002",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="completed",
    )
    await _seed_batch(
        auth_setup.sm,
        batch_id="b003",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="cancelled",
    )

    r = await auth_setup.client.get(
        "/v1/admin/batches?status=completed", headers=_auth(auth_setup.api_key)
    )
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "b002"


@pytest.mark.asyncio
async def test_admin_batch_list_invalid_status_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/batches?status=not_a_status", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 422
    assert "invalid_status" in r.text


@pytest.mark.asyncio
async def test_admin_batch_list_pagination(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    for i in range(5):
        await _seed_batch(
            auth_setup.sm,
            batch_id=f"bp{i:03d}",
            tenant_id=auth_setup.tenant_id,
            team_id=auth_setup.team_id,
        )

    r = await auth_setup.client.get(
        "/v1/admin/batches?limit=2&offset=2", headers=_auth(auth_setup.api_key)
    )
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


# --------------------------------------------------------------------------- #
# Get one                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_get_batch_returns_any_team_batch(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b100",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
    )

    r = await auth_setup.client.get("/v1/admin/batches/b100", headers=_auth(auth_setup.api_key))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "b100"


@pytest.mark.asyncio
async def test_admin_get_batch_404_unknown(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/batches/no_such_batch", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "batch_not_found"


# --------------------------------------------------------------------------- #
# Cancel                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_cancel_batch_flips_status(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b200",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="in_progress",
    )

    r = await auth_setup.client.post(
        "/v1/admin/batches/b200/cancel", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_admin_cancel_already_terminal_is_idempotent(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b201",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
        status="completed",
    )

    r = await auth_setup.client.post(
        "/v1/admin/batches/b201/cancel", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 200
    # Status unchanged — cancel on terminal is a no-op.
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_admin_cancel_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _seed_batch(
        auth_setup.sm,
        batch_id="b202",
        tenant_id=auth_setup.tenant_id,
        team_id=auth_setup.team_id,
    )

    r = await auth_setup.client.post(
        "/v1/admin/batches/b202/cancel", headers=_auth(auth_setup.api_key)
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Scope guards                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_batch_list_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    # Default key only has chat:write.
    r = await auth_setup.client.get("/v1/admin/batches", headers=_auth(auth_setup.api_key))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_get_batch_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    r = await auth_setup.client.get("/v1/admin/batches/any", headers=_auth(auth_setup.api_key))
    assert r.status_code == 403
