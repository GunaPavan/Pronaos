"""HTTP-level tests for the Phase 70 admin-scoped webhook console.

Covers
------
- GET /v1/admin/webhooks/{tenant_id} returns url + secret_set flag.
- 404 on unknown tenant.
- PUT sets url + secret; invalid URL → 422; missing-only-one → 422.
- PUT with both null clears the config.
- POST /v1/admin/webhooks/{tenant_id}/test → 422 when no webhook
  is configured; when one IS configured it fires httpx (mocked via
  respx) and returns the HTTP status in the result.
- Scope split: GET requires admin:usage; PUT + test require admin:identity.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import update

from pronaos.db.models import ApiKey, Tenant


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _grant_scope(sm, key_id: str, scopes: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(scopes=scopes)
        )
        await session.commit()


async def _set_webhook(  # type: ignore[no-untyped-def]
    sm, tenant_id: str, url: str | None, secret: str | None
) -> None:
    async with sm() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(webhook_url=url, webhook_secret=secret)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# GET                                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_webhook_get_returns_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"tenant_id", "url", "secret_set"}
    assert body["url"] is None
    assert body["secret_set"] is False


@pytest.mark.asyncio
async def test_webhook_get_reflects_configured_url(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    await _set_webhook(
        auth_setup.sm,
        auth_setup.tenant_id,
        "https://example.com/hook",
        "s" * 32,
    )
    r = await auth_setup.client.get(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        headers=_auth(auth_setup.api_key),
    )
    body = r.json()
    assert body["url"] == "https://example.com/hook"
    assert body["secret_set"] is True


@pytest.mark.asyncio
async def test_webhook_get_404_unknown_tenant(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.get(
        "/v1/admin/webhooks/no_such_tenant",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["type"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_webhook_get_requires_admin_usage(auth_setup) -> None:  # type: ignore[no-untyped-def]
    r = await auth_setup.client.get(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# PUT                                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_webhook_put_sets_url_and_secret(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        json={"url": "https://hook.example.com/events", "secret": "a" * 32},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "https://hook.example.com/events"
    assert body["secret_set"] is True
    # Secret must not be echoed back.
    assert "secret" not in body


@pytest.mark.asyncio
async def test_webhook_put_null_clears_config(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    await _set_webhook(
        auth_setup.sm, auth_setup.tenant_id, "https://hook.example.com/events", "a" * 32
    )
    r = await auth_setup.client.put(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        json={"url": None, "secret": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["url"] is None
    assert body["secret_set"] is False


@pytest.mark.asyncio
async def test_webhook_put_invalid_url_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        json={"url": "not-a-url", "secret": "a" * 32},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_webhook_put_url_without_secret_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        json={"url": "https://example.com/hook", "secret": None},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert "webhook_config_invalid" in r.text


@pytest.mark.asyncio
async def test_webhook_put_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.put(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}",
        json={"url": "https://example.com/hook", "secret": "a" * 32},
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Test-ping                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_webhook_test_422_when_not_configured(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    r = await auth_setup.client.post(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}/test",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 422
    assert "webhook_not_configured" in r.text


@pytest.mark.asyncio
async def test_webhook_test_fires_and_returns_status(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    await _set_webhook(
        auth_setup.sm,
        auth_setup.tenant_id,
        "https://hook.example.com/events",
        "a" * 32,
    )
    # Mock the outbound httpx call made by the test-ping handler.
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://hook.example.com/events").mock(
            return_value=httpx.Response(200, text="OK")
        )
        r = await auth_setup.client.post(
            f"/v1/admin/webhooks/{auth_setup.tenant_id}/test",
            headers=_auth(auth_setup.api_key),
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["http_status"] == 200
    assert body["signed"] is True
    assert body["error"] is None
    assert body["tenant_id"] == auth_setup.tenant_id


@pytest.mark.asyncio
async def test_webhook_test_captures_upstream_error(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A connection error should NOT raise — it should be captured in ``error``."""
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:identity")
    await _set_webhook(
        auth_setup.sm,
        auth_setup.tenant_id,
        "https://hook.example.com/events",
        "a" * 32,
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://hook.example.com/events").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        r = await auth_setup.client.post(
            f"/v1/admin/webhooks/{auth_setup.tenant_id}/test",
            headers=_auth(auth_setup.api_key),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["http_status"] is None
    assert body["error"] is not None
    assert "connection error" in body["error"]


@pytest.mark.asyncio
async def test_webhook_test_requires_admin_identity(auth_setup) -> None:  # type: ignore[no-untyped-def]
    await _grant_scope(auth_setup.sm, auth_setup.key_id, "admin:usage")
    r = await auth_setup.client.post(
        f"/v1/admin/webhooks/{auth_setup.tenant_id}/test",
        headers=_auth(auth_setup.api_key),
    )
    assert r.status_code == 403
