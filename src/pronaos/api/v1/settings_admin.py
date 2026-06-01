"""Gateway settings summary endpoint (Phase 71).

``GET /v1/admin/settings`` returns a sanitised view of the gateway's
runtime configuration — which optional features are active, which
providers are wired up, which integrations are configured. Nothing
sensitive is included (no API keys, no secrets).

This is the read surface for the admin UI's /settings page. Operators
who need to change settings must update the environment variables and
restart the gateway; there is no write surface here (gateway settings
are not per-tenant or per-operator — they're process-level config).

Scope: ``admin:usage`` (read-only, same as the rest of the dashboard).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import require_scope
from pronaos.config import get_settings
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-settings"])


class GatewaySettingsResponse(BaseModel):
    """Sanitised gateway config for the admin UI.

    All fields are booleans (or nullable strings that don't carry
    secrets). The intent is to let the operator see at a glance
    which optional features are active without exposing the values
    themselves. Operators who need to see the raw values should
    inspect the environment or the deployment config directly.
    """

    # Optional storage / cache backends
    redis_configured: bool
    semantic_cache_enabled: bool

    # Provider availability
    anthropic_configured: bool
    groq_configured: bool
    openai_configured: bool
    bedrock_configured: bool
    vertex_configured: bool

    # Optional feature flags
    mcp_enabled: bool
    presidio_enabled: bool
    singleflight_distributed: bool

    # OIDC/SSO admin auth (Phase 26)
    oidc_configured: bool
    # The issuer URL is not a secret and is useful for the UI to
    # display — operators need to verify it matches their IdP.
    oidc_issuer: str | None

    # Database scheme (safe to show — not a credential)
    database_scheme: str | None


@router.get(
    "/settings",
    response_model=GatewaySettingsResponse,
)
async def get_gateway_settings(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
) -> GatewaySettingsResponse:
    """Return sanitised gateway runtime config.

    Safe to expose to any ``admin:usage`` key. No secrets are
    returned — only booleans + the non-secret OIDC issuer URL.
    """
    settings = get_settings()

    # Database scheme only (not the full URL / credentials).
    db_scheme: str | None = None
    if settings.database_url:
        db_scheme = settings.database_url.split("://", 1)[0]

    return GatewaySettingsResponse(
        redis_configured=bool(settings.redis_url),
        semantic_cache_enabled=settings.semantic_cache_enabled,
        anthropic_configured=bool(settings.anthropic_api_key),
        groq_configured=bool(settings.groq_api_key),
        openai_configured=bool(settings.openai_api_key),
        bedrock_configured=bool(
            settings.aws_access_key_id and settings.aws_secret_access_key
        ),
        vertex_configured=bool(
            settings.vertex_project_id and settings.vertex_service_account_json
        ),
        mcp_enabled=settings.mcp_enabled,
        presidio_enabled=settings.presidio_enabled,
        singleflight_distributed=settings.singleflight_distributed,
        oidc_configured=bool(settings.oidc_issuer),
        oidc_issuer=settings.oidc_issuer,
        database_scheme=db_scheme,
    )
