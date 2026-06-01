"""Admin-scoped batch console endpoints (Phase 69).

The existing ``/v1/batches/*`` endpoints are consumer-scoped
(``chat:write``) and only return the calling team's batches. The
Phase 69 admin console needs cross-team visibility — an operator
wants to see ALL batches across the tenant, filter by team/status,
and cancel misbehaving batches.

This module adds three admin endpoints:

  GET  /v1/admin/batches           list batches (all teams or filtered)
  GET  /v1/admin/batches/{id}      get one batch (any team)
  POST /v1/admin/batches/{id}/cancel  cancel any team's batch

Scope model
-----------
GETs use ``admin:usage`` (read-only; same scope as the FinOps and
routing dashboards). Cancel uses ``admin:identity`` — cancelling a
running batch at 50% pricing is a financially impactful decision
that shouldn't be available to read-only dashboard keys.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.api.v1.batches import BatchResponse, _to_response
from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import Batch
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-batches"])

# Valid status values (mirrors the DB model state machine).
_VALID_STATUSES = {
    "validating",
    "in_progress",
    "finalizing",
    "completed",
    "failed",
    "expired",
    "cancelled",
}


class AdminBatchListResponse(BaseModel):
    items: list[BatchResponse]
    total: int
    limit: int
    offset: int


@router.get(
    "/batches",
    response_model=AdminBatchListResponse,
)
async def admin_list_batches(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    team_id: Annotated[str | None, Query()] = None,
    tenant_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminBatchListResponse:
    """Paginated list of batches across all teams.

    Optional filters: ``team_id``, ``tenant_id``, ``status``.
    Ordered newest-first (``created_at`` desc) so the operator sees
    the most recent submissions at the top.
    """
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "invalid_status",
                "hint": f"status must be one of {sorted(_VALID_STATUSES)}; "
                f"got {status!r}",
            },
        )

    base = select(Batch)
    if team_id:
        base = base.where(Batch.team_id == team_id)
    if tenant_id:
        base = base.where(Batch.tenant_id == tenant_id)
    if status:
        base = base.where(Batch.status == status)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (
        (
            await session.execute(
                base.order_by(Batch.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )

    items = [_to_response(b) for b in rows]
    log.info(
        "admin.batches.list",
        total=total,
        returned=len(items),
        filters={"team_id": team_id, "status": status},
    )
    return AdminBatchListResponse(
        items=items, total=total or 0, limit=limit, offset=offset
    )


@router.get(
    "/batches/{batch_id}",
    response_model=BatchResponse,
)
async def admin_get_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BatchResponse:
    """Get any team's batch by id.

    Unlike the consumer endpoint (which gates on the caller's team),
    this admin endpoint retrieves the batch regardless of which team
    submitted it. Useful for cross-team support queries.
    """
    batch = await session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "batch_not_found", "batch_id": batch_id},
        )
    return _to_response(batch)


@router.post(
    "/batches/{batch_id}/cancel",
    response_model=BatchResponse,
)
async def admin_cancel_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BatchResponse:
    """Force-cancel any team's batch.

    Gated on ``admin:identity`` — cancelling a running batch wastes the
    cost of already-dispatched requests and terminates in-flight jobs,
    so it's treated as an operationally sensitive write.

    If the batch is already in a terminal state (completed / failed /
    expired / cancelled), the row is returned unchanged (idempotent).
    No provider call is made here — the background worker picks up the
    status change on its next poll and propagates it to the upstream
    if needed.
    """
    batch = await session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "batch_not_found", "batch_id": batch_id},
        )
    if batch.status in {"completed", "failed", "expired", "cancelled"}:
        # Already terminal — return idempotently.
        return _to_response(batch)

    batch.status = "cancelled"
    await session.commit()
    await session.refresh(batch)

    log.info(
        "admin.batches.cancelled",
        batch_id=batch_id,
        team_id=batch.team_id,
        provider=batch.provider,
    )
    return _to_response(batch)
