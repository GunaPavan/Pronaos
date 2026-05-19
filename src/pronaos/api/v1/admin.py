"""Admin endpoints — FinOps and operational visibility.

Phase 5.3 scope: ``GET /v1/admin/usage`` — paginated query over the
``usage_records`` table with filters and an aggregate totals block in
the same response.

Tenant isolation
----------------
Every query is silently scoped to ``principal.tenant_id`` regardless of
whether the caller supplied a ``team_id`` filter. A future ``admin:global``
scope can lift this restriction for self-hosted-multi-tenant operators; for
now an admin key can only inspect its own tenant's usage. That's the
correct default — a leaked admin key shouldn't expose every customer.

Scope
-----
``admin:usage`` (introduced in Phase 5.3). Distinct from a generic
``admin`` scope so least-privilege keys can be issued — a FinOps dashboard
key needs read access to usage but not the right to rotate keys or change
budgets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import Team, UsageRecord
from pronaos.guardrails.policy import validate_policy
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# Response models                                                             #
# --------------------------------------------------------------------------- #


class UsageItem(BaseModel):
    """One row of ``usage_records``, projected for the HTTP wire."""

    id: str
    ts: datetime
    tenant_id: str
    team_id: str
    key_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_hcents: int
    request_id: str | None
    status: str


class UsageTotals(BaseModel):
    """Aggregate over the same filter set as ``items`` — NOT just the
    current page. Lets dashboards display a single 'spend this period'
    figure without making a second query."""

    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_hcents: int


class UsageResponse(BaseModel):
    items: list[UsageItem]
    totals: UsageTotals
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    start_ts: Annotated[datetime | None, Query(description="Inclusive lower bound on ts")] = None,
    end_ts: Annotated[datetime | None, Query(description="Exclusive upper bound on ts")] = None,
    team_id: Annotated[str | None, Query(description="Filter to one team within the tenant")] = None,  # noqa: E501 — keep on one line for readable OpenAPI signature
    provider: Annotated[str | None, Query(description="Filter to one provider (e.g. 'anthropic')")] = None,  # noqa: E501
    model: Annotated[str | None, Query(description="Filter to one model id")] = None,
    status: Annotated[str | None, Query(description="success | error | …")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UsageResponse:
    """Paginated query over the tenant's usage with totals.

    The response carries ``items`` (the current page) and ``totals``
    (aggregates over the *full* filter, not the page). That shape lets a
    dashboard fetch "spend this month, plus first 100 line items" in one
    request.
    """
    # Build the WHERE clause once so it applies identically to both the
    # SELECT and the aggregate query — drift between them would silently
    # corrupt the totals.
    conditions: list[Any] = [UsageRecord.tenant_id == principal.tenant_id]
    if start_ts is not None:
        conditions.append(UsageRecord.ts >= start_ts)
    if end_ts is not None:
        conditions.append(UsageRecord.ts < end_ts)
    if team_id is not None:
        conditions.append(UsageRecord.team_id == team_id)
    if provider is not None:
        conditions.append(UsageRecord.provider == provider)
    if model is not None:
        conditions.append(UsageRecord.model == model)
    if status is not None:
        conditions.append(UsageRecord.status == status)

    # Page query — newest first is the dashboard-natural default.
    items_stmt = (
        select(UsageRecord)
        .where(*conditions)
        .order_by(UsageRecord.ts.desc())
        .limit(limit)
        .offset(offset)
    )
    items = (await session.execute(items_stmt)).scalars().all()

    # Aggregate query — runs over the SAME filter. COUNT(*) gives the total
    # number of matching rows (so the client can paginate); the SUMs give
    # the FinOps numbers.
    agg_stmt = select(
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_hcents), 0),
    ).where(*conditions)
    row = (await session.execute(agg_stmt)).one()
    requests, prompt_sum, completion_sum, cost_sum = row

    totals = UsageTotals(
        requests=int(requests),
        prompt_tokens=int(prompt_sum),
        completion_tokens=int(completion_sum),
        total_tokens=int(prompt_sum) + int(completion_sum),
        cost_hcents=int(cost_sum),
    )

    return UsageResponse(
        items=[_to_item(r) for r in items],
        totals=totals,
        limit=limit,
        offset=offset,
    )


def _to_item(r: UsageRecord) -> UsageItem:
    return UsageItem(
        id=r.id,
        ts=r.ts,
        tenant_id=r.tenant_id,
        team_id=r.team_id,
        key_id=r.key_id,
        provider=r.provider,
        model=r.model,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cost_hcents=r.cost_hcents,
        request_id=r.request_id,
        status=r.status,
    )


# --------------------------------------------------------------------------- #
# Guardrail policy (Phase 8.2 / Phase 11 admin endpoint)                       #
# --------------------------------------------------------------------------- #


class GuardrailPolicyBody(BaseModel):
    """Per-team guardrail policy override.

    Same JSON shape as the column on ``teams.guardrail_policy`` and the
    CLI ``--disable`` / ``--set-action`` flags. ``disabled_rules`` skips
    a rule entirely (no scan); ``rule_actions`` overrides the action a
    rule produces when it fires. Both fields are optional; an empty body
    is a valid "clear policy" request — equivalent to ``--reset`` on
    the CLI.
    """

    disabled_rules: list[str] = Field(default_factory=list)
    rule_actions: dict[str, str] = Field(default_factory=dict)

    def to_storage(self) -> dict[str, Any] | None:
        """Return the JSON to persist (None = clear / use engine defaults)."""
        out: dict[str, Any] = {}
        if self.disabled_rules:
            out["disabled_rules"] = sorted(set(self.disabled_rules))
        if self.rule_actions:
            out["rule_actions"] = {k: v.lower() for k, v in self.rule_actions.items()}
        return out or None


class GuardrailPolicyResponse(BaseModel):
    """Current per-team policy or ``null`` when defaults are in effect."""

    team_id: str
    policy: dict[str, Any] | None


@router.get(
    "/team/{team_id}/guardrail-policy",
    response_model=GuardrailPolicyResponse,
)
async def get_guardrail_policy(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GuardrailPolicyResponse:
    """Read the current policy for a team.

    Returns ``policy: null`` when no override is configured (engine
    defaults are in effect). Tenant-isolated: a 404 is returned both
    when the team doesn't exist AND when it belongs to a different
    tenant — admins can't probe foreign tenants by id.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return GuardrailPolicyResponse(team_id=team.id, policy=team.guardrail_policy)


@router.put(
    "/team/{team_id}/guardrail-policy",
    response_model=GuardrailPolicyResponse,
)
async def put_guardrail_policy(
    team_id: str,
    body: GuardrailPolicyBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GuardrailPolicyResponse:
    """Replace the team's policy.

    The body is validated against the same rules the CLI applies
    (unknown action names, unknown top-level keys → 422). An empty body
    clears the policy (engine defaults apply). PUT semantics: each call
    replaces the policy wholesale — there's no PATCH for incremental
    edits. Operators wanting fine-grained updates should GET, mutate
    the dict locally, then PUT back. (Phase 12 may add PATCH if the
    UX warrants.)"""
    raw_policy = body.to_storage()
    errors = validate_policy(raw_policy)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"type": "invalid_policy", "errors": errors},
        )

    team = await _load_team_for_caller(session, team_id, principal)
    team.guardrail_policy = raw_policy
    # The get_db dependency commits on clean exit — no explicit commit needed.
    return GuardrailPolicyResponse(team_id=team.id, policy=raw_policy)


# --------------------------------------------------------------------------- #
# Model allowlist (Phase 17)                                                  #
# --------------------------------------------------------------------------- #
#
# Same shape as guardrail-policy: GET + PUT on a per-team resource, tenant-
# isolated by ``_load_team_for_caller``. The JSON body is a single ``patterns``
# field — explicit ``None`` clears the allowlist (team becomes unrestricted),
# an empty list explicitly denies everything.


class AllowedModelsBody(BaseModel):
    """Per-team model allowlist.

    ``patterns`` semantics:

    - ``None`` (or field omitted): clear the allowlist; team becomes
      unrestricted.
    - ``[]``: deny everything — useful for pausing a team without
      revoking its keys.
    - ``["groq/*", "anthropic/claude-opus-*"]``: allow only models
      matching any of the fnmatch-style patterns.
    """

    patterns: list[str] | None = None


class AllowedModelsResponse(BaseModel):
    """Current per-team allowlist; ``null`` ≡ unrestricted."""

    team_id: str
    allowed_models: list[str] | None


@router.get(
    "/team/{team_id}/allowed-models",
    response_model=AllowedModelsResponse,
)
async def get_allowed_models(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedModelsResponse:
    """Read a team's current model allowlist.

    Returns ``allowed_models: null`` when no policy is set (team can
    invoke any model in the catalog). Same tenant-isolation behaviour
    as ``get_guardrail_policy``.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return AllowedModelsResponse(team_id=team.id, allowed_models=team.allowed_models)


@router.put(
    "/team/{team_id}/allowed-models",
    response_model=AllowedModelsResponse,
)
async def put_allowed_models(
    team_id: str,
    body: AllowedModelsBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedModelsResponse:
    """Replace the team's model allowlist.

    Validated against the same rules as the CLI (every entry must be a
    non-empty string). Invalid input → 422 with a per-field reason
    so operators can fix the typo without trial-and-error. PUT
    semantics: each call replaces the policy wholesale."""
    from pronaos.core.model_access import validate_allowed_models

    raw = body.patterns
    if raw is not None:
        try:
            raw = validate_allowed_models(raw)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"type": "invalid_allowed_models", "error": str(e)},
            ) from None

    team = await _load_team_for_caller(session, team_id, principal)
    team.allowed_models = raw
    return AllowedModelsResponse(team_id=team.id, allowed_models=raw)


# --------------------------------------------------------------------------- #
# Routing strategy (Phase 21)                                                 #
# --------------------------------------------------------------------------- #
#
# Per-team strategy for resolving ``model="auto"`` requests. NULL = no
# preference; the gateway falls back to ``cheapest``. Validated against
# the RoutingStrategy enum on PUT so bad strings never reach the DB.


class RoutingStrategyBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/routing-strategy``.

    ``strategy: null`` clears the column (unset → defaults to ``cheapest``);
    a string is validated against the ``RoutingStrategy`` enum.
    """

    strategy: str | None = None


class RoutingStrategyResponse(BaseModel):
    """Current per-team routing strategy; ``null`` ≡ unset (defaults to cheapest)."""

    team_id: str
    routing_strategy: str | None


@router.get(
    "/team/{team_id}/routing-strategy",
    response_model=RoutingStrategyResponse,
)
async def get_routing_strategy(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingStrategyResponse:
    """Read a team's current routing strategy.

    Returns ``routing_strategy: null`` when no strategy is set; the
    gateway will treat that as ``cheapest`` for auto-routed requests.
    Same tenant-isolation behaviour as the allowlist endpoint.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return RoutingStrategyResponse(
        team_id=team.id, routing_strategy=team.routing_strategy
    )


@router.put(
    "/team/{team_id}/routing-strategy",
    response_model=RoutingStrategyResponse,
)
async def put_routing_strategy(
    team_id: str,
    body: RoutingStrategyBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingStrategyResponse:
    """Replace the team's routing strategy.

    Validated against the ``RoutingStrategy`` enum. Invalid input → 422.
    PUT semantics: each call replaces the value wholesale.
    """
    from pronaos.core.scorer import RoutingStrategy

    raw = body.strategy
    if raw is not None:
        try:
            raw = RoutingStrategy(raw.strip().lower()).value
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "invalid_routing_strategy",
                    "error": (
                        "strategy must be one of: "
                        + ", ".join(s.value for s in RoutingStrategy)
                    ),
                },
            ) from None

    team = await _load_team_for_caller(session, team_id, principal)
    team.routing_strategy = raw
    return RoutingStrategyResponse(team_id=team.id, routing_strategy=raw)


# --------------------------------------------------------------------------- #
# Tenant webhook config (Phase 19)                                            #
# --------------------------------------------------------------------------- #
#
# Tenant-scoped, not team-scoped, because operational events typically route
# to ONE incident channel per organisation. GET shows current state with the
# secret REDACTED (just "(set)" / "(missing)"), PUT sets both fields.


class WebhookBody(BaseModel):
    """Per-tenant webhook config.

    Either both ``url`` and ``secret`` are non-empty, or both are
    ``None`` (omitted) to clear. Mixed states (URL but no secret, or
    vice versa) → 422; sending an unsigned payload would be a security
    smell."""

    url: str | None = None
    secret: str | None = None


class WebhookResponse(BaseModel):
    """Current webhook config; secret is REDACTED on GET (the gateway
    treats it as a write-only field once stored — operators rotate by
    overwriting, never by reading)."""

    tenant_id: str
    url: str | None
    secret_set: bool


@router.get(
    "/tenant/{tenant_id}/webhook",
    response_model=WebhookResponse,
)
async def get_webhook(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookResponse:
    """Read a tenant's current webhook config.

    Tenant-isolated: a caller can only read their own tenant's config.
    Cross-tenant requests return 403. Secret is redacted in the
    response — see the WebhookResponse docstring for rationale."""
    from sqlalchemy import select

    from pronaos.db.models import Tenant

    # Tenant-isolation: the caller's tenant_id must match the URL's
    # tenant_id. We don't surface 404 for "wrong tenant" to avoid
    # leaking tenant existence to outsiders.
    if principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"type": "forbidden", "message": "cross-tenant access denied"},
        )

    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "tenant_not_found"},
        )
    return WebhookResponse(
        tenant_id=tenant_row.id,
        url=tenant_row.webhook_url,
        secret_set=bool(tenant_row.webhook_secret),
    )


@router.put(
    "/tenant/{tenant_id}/webhook",
    response_model=WebhookResponse,
)
async def put_webhook(
    tenant_id: str,
    body: WebhookBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookResponse:
    """Set or clear a tenant's webhook config.

    Body shape: ``{"url": "...", "secret": "..."}``. Both must be
    present + non-empty to enable webhooks; both omitted (or null)
    clears the config. Mixed states → 422 (a half-configured webhook
    would either fail the no-op check or send unsigned payloads —
    neither is what the operator meant)."""
    from sqlalchemy import select

    from pronaos.db.models import Tenant

    if principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"type": "forbidden", "message": "cross-tenant access denied"},
        )

    # Validate the "both or neither" invariant up front so the storage
    # layer never holds a half-configured webhook.
    url_set = bool(body.url and body.url.strip())
    secret_set = bool(body.secret and body.secret.strip())
    if url_set != secret_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_webhook_config",
                "error": (
                    "url and secret must both be set, or both omitted; "
                    f"got url_set={url_set}, secret_set={secret_set}"
                ),
            },
        )
    if url_set:
        assert body.url is not None  # narrowing for mypy
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "invalid_webhook_url",
                    "error": "webhook URL must start with http:// or https://",
                },
            )

    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "tenant_not_found"},
        )
    tenant_row.webhook_url = body.url if url_set else None
    tenant_row.webhook_secret = body.secret if secret_set else None
    return WebhookResponse(
        tenant_id=tenant_row.id,
        url=tenant_row.webhook_url,
        secret_set=bool(tenant_row.webhook_secret),
    )


async def _load_team_for_caller(
    session: AsyncSession, team_id: str, principal: Principal
) -> Team:
    """Fetch ``team_id`` if and only if it belongs to ``principal.tenant_id``.

    Otherwise returns 404 — the same response whether the team doesn't
    exist or belongs to a different tenant. The two cases are
    deliberately indistinguishable so an admin can't enumerate other
    tenants' team ids by probing this endpoint."""
    team = await session.get(Team, team_id)
    if team is None or team.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team not found: {team_id}",
        )
    return team


__all__ = ["UsageItem", "UsageResponse", "UsageTotals", "router"]
