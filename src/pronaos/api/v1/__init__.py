"""v1 public API surface."""

from fastapi import APIRouter

from pronaos.api.v1 import (
    admin,
    batches,
    batches_admin,
    budgets,
    chat,
    embeddings,
    health,
    identity,
    mcp_sse,
    models,
    reliability,
    rerank,
    routing,
    security,
    settings_admin,
    webhooks_admin,
)

router = APIRouter(prefix="/v1")
router.include_router(health.router)
router.include_router(chat.router)
router.include_router(embeddings.router)
router.include_router(rerank.router)
router.include_router(admin.router)
# Phase 48 — MCP SSE transport mounts under /v1/mcp/{sse,messages}.
# The routes return 503 when PRONAOS_MCP_ENABLED is false so the URL
# surface stays stable across the on/off operator flip.
router.include_router(mcp_sse.router)
# Phase 59 — async batches API. Routes return 422 ``batches_disabled``
# unless the team has ``batches_enabled=true``; per-team opt-in keeps
# the surface stable without forcing operators to handle large quota
# spikes by default.
router.include_router(batches.router)
# Phase 63 — identity CRUD (tenants / teams / keys) under
# /v1/admin/*. Mirrors the long-standing CLI surface; gated on the
# new ``admin:identity`` scope so existing admin:usage keys don't
# accidentally gain key-issuance power.
router.include_router(identity.router, prefix="/admin")
# Phase 64 — per-team budgets GET/PUT + usage timeseries for the
# FinOps dashboard. Reads on admin:usage; budget writes on
# admin:identity (changing a budget is a sensitive operation).
router.include_router(budgets.router, prefix="/admin")
# Phase 65 — model catalog enumerated for the admin playground.
# Same admin:usage scope as the rest of /admin reads.
router.include_router(models.router, prefix="/admin")
# Phase 66 — composed routing config (strategy + scores + thresholds +
# allowlist) for the admin routing console. GET on admin:usage; PUT on
# admin:identity (routing changes are operationally sensitive).
router.include_router(routing.router, prefix="/admin")
# Phase 67 — security config (guardrail_policy + PII tokenization)
# under /v1/admin/security/{team_id} + audit log surfaces under
# /v1/admin/audit/{tenant_id} (list + chain verify). Same scope split
# as routing: admin:usage GETs, admin:identity PUTs.
router.include_router(security.router, prefix="/admin")
# Phase 68 — reliability console (provider catalog + circuit state +
# doctor gates). admin:usage GETs; admin:identity on the breaker-reset
# POST (force-resetting a live breaker can re-expose traffic to a
# still-broken upstream).
router.include_router(reliability.router, prefix="/admin")
# Phase 69 — admin-scoped batch console (list/get any team's batch +
# admin cancel). Separate from the consumer /v1/batches/* surface which
# uses chat:write and only sees the calling team's batches.
router.include_router(batches_admin.router, prefix="/admin")
# Phase 70 — admin-scoped webhook console (cross-tenant GET/PUT +
# synchronous test-ping). admin:usage GETs; admin:identity writes.
# Separate from the tenant-isolated /v1/admin/tenant/{id}/webhook
# in admin.py which gates on principal.tenant_id == tenant_id.
router.include_router(webhooks_admin.router, prefix="/admin")
# Phase 71 — gateway settings summary (sanitised, read-only) and
# the OIDC subject editor for tenants (extended identity PATCH).
router.include_router(settings_admin.router, prefix="/admin")

__all__ = ["router"]
