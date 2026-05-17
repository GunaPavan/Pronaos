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

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import UsageRecord
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
    team_id: Annotated[str | None, Query(description="Filter to one team within the tenant")] = None,
    provider: Annotated[str | None, Query(description="Filter to one provider (e.g. 'anthropic')")] = None,
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


__all__ = ["router", "UsageResponse", "UsageItem", "UsageTotals"]
