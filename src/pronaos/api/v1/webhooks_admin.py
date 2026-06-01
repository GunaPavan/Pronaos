"""Admin-scoped webhook console endpoints (Phase 70).

The existing ``/v1/admin/tenant/{id}/webhook`` endpoints in admin.py
are tenant-isolated — a key can only read/write its OWN tenant's
config. An operator managing multiple tenants needs cross-tenant
visibility.

This module adds three admin endpoints:

  GET  /v1/admin/webhooks/{tenant_id}       read any tenant's config
  PUT  /v1/admin/webhooks/{tenant_id}       write any tenant's config
  POST /v1/admin/webhooks/{tenant_id}/test  send a test ping + return result

Scope model
-----------
GET uses ``admin:usage`` (read-only; same as the rest of the
dashboard). PUT and test-ping use ``admin:identity`` — writing a
webhook URL changes where operational events are dispatched, which
is sensitive enough to require the write scope.
"""

from __future__ import annotations

import json
import time
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.core.webhooks import WebhookEvent, sign_payload
from pronaos.db.models import Tenant
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-webhooks"])

# Maximum URL length mirrors the DB column (String(2048)).
_MAX_URL_LEN = 2048
# Minimum secret length — anything shorter is a security smell.
_MIN_SECRET_LEN = 16

# Test-ping request / connect timeouts in seconds.
_TEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


# --------------------------------------------------------------------------- #
# Shared schemas                                                              #
# --------------------------------------------------------------------------- #


class WebhookAdminResponse(BaseModel):
    """Webhook config for one tenant.

    ``secret_set`` is a boolean rather than the literal secret so the
    secret never travels back over the wire after it's been stored.
    Operators rotate by re-writing via PUT — they never need to read it
    back.
    """

    tenant_id: str
    url: str | None
    secret_set: bool


class WebhookUpdateBody(BaseModel):
    """Set or clear a tenant's webhook config.

    Both ``url`` and ``secret`` must be supplied (non-null) to enable,
    or both omitted / null to clear. A URL without a secret would send
    unsigned payloads; a secret without a URL would be unreachable.
    The "both or neither" invariant keeps the config coherent.
    """

    url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    secret: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("URL scheme must be http or https")
            if not parsed.netloc:
                raise ValueError("URL must have a host")
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("secret")
    @classmethod
    def _check_secret(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < _MIN_SECRET_LEN:
            raise ValueError(f"secret must be at least {_MIN_SECRET_LEN} characters")
        return v


class WebhookTestResult(BaseModel):
    """Synchronous test-ping result.

    ``http_status`` is the upstream's HTTP status code. ``error`` is
    non-null when the request itself failed (e.g. connection refused)
    before a status code could be read. ``signed`` confirms the payload
    was HMAC-signed before dispatch.
    """

    tenant_id: str
    http_status: int | None
    response_body: str | None
    error: str | None
    signed: bool
    delivery_id: str


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


async def _load_tenant(session: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "tenant_not_found", "tenant_id": tenant_id},
        )
    return tenant


def _to_response(tenant: Tenant) -> WebhookAdminResponse:
    return WebhookAdminResponse(
        tenant_id=tenant.id,
        url=tenant.webhook_url,
        secret_set=bool(tenant.webhook_secret),
    )


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.get(
    "/webhooks/{tenant_id}",
    response_model=WebhookAdminResponse,
)
async def admin_get_webhook(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookAdminResponse:
    """Return webhook config for any tenant (no tenant-isolation guard).

    Unlike the legacy ``GET /v1/admin/tenant/{id}/webhook`` endpoint
    which restricts callers to their own tenant, this admin endpoint
    allows cross-tenant reads so an operator managing multiple tenants
    can review all webhook configs without switching keys.
    """
    tenant = await _load_tenant(session, tenant_id)
    return _to_response(tenant)


@router.put(
    "/webhooks/{tenant_id}",
    response_model=WebhookAdminResponse,
)
async def admin_put_webhook(
    tenant_id: str,
    body: WebhookUpdateBody,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookAdminResponse:
    """Set or clear a tenant's webhook config.

    **Both** ``url`` + ``secret`` must be supplied to enable; both
    omitted (null) clears the config. Mixed states → 422.

    The secret is write-only — once stored, it is never returned in
    GET responses. Rotate by PUTting a new pair.
    """
    url, secret = body.url, body.secret

    # Enforce "both or neither" invariant.
    if (url is None) != (secret is None):
        raise HTTPException(
            status_code=422,
            detail={
                "type": "webhook_config_invalid",
                "hint": (
                    "url and secret must both be set or both omitted; "
                    "a URL without a secret would send unsigned payloads"
                ),
            },
        )

    tenant = await _load_tenant(session, tenant_id)
    tenant.webhook_url = url
    tenant.webhook_secret = secret
    await session.commit()
    await session.refresh(tenant)

    log.info(
        "admin.webhooks.updated",
        tenant_id=tenant_id,
        url_set=bool(url),
        secret_set=bool(secret),
    )
    return _to_response(tenant)


@router.post(
    "/webhooks/{tenant_id}/test",
    response_model=WebhookTestResult,
)
async def admin_test_webhook(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookTestResult:
    """Send a test ping to the tenant's configured webhook URL.

    Fires synchronously and returns the HTTP status + response body so
    operators can immediately see whether their endpoint is reachable
    and returning a 2xx. 422 if no webhook URL is configured.

    The payload is a Pronaos-signed ``webhook.test`` event so the
    receiver's signature-validation code exercises the same path as
    production events.
    """
    tenant = await _load_tenant(session, tenant_id)
    if not tenant.webhook_url or not tenant.webhook_secret:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "webhook_not_configured",
                "hint": (
                    "configure the webhook URL + secret via "
                    "PUT /v1/admin/webhooks/{tenant_id} first"
                ),
            },
        )

    import uuid

    delivery_id = uuid.uuid4().hex
    # "webhook.test" is not part of the production EventType Literal;
    # we use it here purely as a test-ping signal. Receivers should
    # handle it gracefully (return 200 even if they don't process it).
    event = WebhookEvent(
        event="webhook.test",  # type: ignore[arg-type]
        tenant_id=tenant_id,
        data={"delivery_id": delivery_id},
        ts=time.time(),
    )
    body_bytes = json.dumps(event.to_body()).encode("utf-8")
    signature = sign_payload(body_bytes, tenant.webhook_secret)

    http_status: int | None = None
    response_body: str | None = None
    error: str | None = None

    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.post(
                tenant.webhook_url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Pronaos-Event": event.event,
                    "X-Pronaos-Signature": signature,
                    "X-Pronaos-Delivery": delivery_id,
                },
            )
            http_status = resp.status_code
            response_body = resp.text[:2048]  # cap; full bodies can be large
    except httpx.TimeoutException as exc:
        error = f"timeout: {exc}"
    except httpx.ConnectError as exc:
        error = f"connection error: {exc}"
    except Exception as exc:
        error = f"request failed: {type(exc).__name__}: {exc}"

    log.info(
        "admin.webhooks.test_ping",
        tenant_id=tenant_id,
        delivery_id=delivery_id,
        http_status=http_status,
        error=error,
    )
    return WebhookTestResult(
        tenant_id=tenant_id,
        http_status=http_status,
        response_body=response_body,
        error=error,
        signed=True,
        delivery_id=delivery_id,
    )
