"""Budgets + usage timeseries (Phase 64).

The FinOps dashboard needs two things the existing ``/v1/admin/usage``
endpoint doesn't provide:

1. **Per-team budget config + current-period progress**. The Team row
   carries ``monthly_token_budget`` + ``monthly_cost_hcents_budget``
   (NULL = unlimited) and the running ``current_period_*`` counters
   QuotaTracker maintains. Phase 64 exposes both as GET/PUT under
   ``/v1/admin/budgets/{team_id}``.

2. **Time-bucketed aggregates** for spend-over-time charts. The
   existing /v1/admin/usage returns flat items + a single totals
   block; the dashboard needs e.g. "daily spend for the last 30 days"
   in one call. ``GET /v1/admin/usage/timeseries`` returns one row
   per time bucket with the same FinOps fields.

Scope model
-----------
Reads (GET) use ``admin:usage`` — same scope the rest of /admin
already requires. Writes (PUT on budgets) use ``admin:identity``;
changing a team's budget is sensitive enough to gate behind the
write scope introduced in Phase 63.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import Team, UsageRecord
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-budgets"])


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #


class BudgetResponse(BaseModel):
    """Per-team budget config + current-period state.

    The caps are nullable — ``None`` means "no cap" / unlimited.
    The current_period_* counters are non-null running totals.
    ``period_resets_at`` is the next rollover instant (calendar-month
    UTC); the QuotaTracker zeroes both counters together when the
    next request lands past it.
    """

    team_id: str
    monthly_token_budget: int | None
    current_period_tokens: int
    monthly_cost_hcents_budget: int | None
    current_period_cost_hcents: int
    period_resets_at: int  # unix seconds


class BudgetUpdateBody(BaseModel):
    """PATCH-style body. Any field left out is unchanged.
    Passing ``null`` explicitly clears the cap (unlimited)."""

    monthly_token_budget: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Tokens per calendar-month period. `null` = unlimited; omit field to leave unchanged"
        ),
    )
    monthly_cost_hcents_budget: int | None = Field(
        default=None,
        ge=0,
        description=("Cost in hundredths-of-a-cent per period. `null` = unlimited"),
    )

    # Distinguishing "omitted" from "null" needs model_extra metadata —
    # we look at model_fields_set on the parsed body in the handler.


@router.get(
    "/budgets/{team_id}",
    response_model=BudgetResponse,
)
async def get_budget(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BudgetResponse:
    _ = principal
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "hint": team_id},
        )
    return _budget_response(team)


@router.put(
    "/budgets/{team_id}",
    response_model=BudgetResponse,
)
async def put_budget(
    team_id: str,
    body: BudgetUpdateBody,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BudgetResponse:
    _ = principal
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "hint": team_id},
        )
    # Only touch fields the caller explicitly set in the body. This
    # lets the UI patch just one of the two caps without clobbering
    # the other.
    set_fields = body.model_fields_set
    if "monthly_token_budget" in set_fields:
        team.monthly_token_budget = body.monthly_token_budget
    if "monthly_cost_hcents_budget" in set_fields:
        team.monthly_cost_hcents_budget = body.monthly_cost_hcents_budget
    await session.commit()
    await session.refresh(team)
    log.info(
        "admin.budget.updated",
        team_id=team_id,
        token_budget=team.monthly_token_budget,
        cost_budget_hcents=team.monthly_cost_hcents_budget,
    )
    return _budget_response(team)


def _budget_response(team: Team) -> BudgetResponse:
    return BudgetResponse(
        team_id=team.id,
        monthly_token_budget=team.monthly_token_budget,
        current_period_tokens=team.current_period_tokens,
        monthly_cost_hcents_budget=team.monthly_cost_hcents_budget,
        current_period_cost_hcents=team.current_period_cost_hcents,
        period_resets_at=_epoch(team.period_resets_at) or 0,
    )


# --------------------------------------------------------------------------- #
# Usage timeseries                                                            #
# --------------------------------------------------------------------------- #


class TimeseriesPoint(BaseModel):
    """One bucket in the spend-over-time series."""

    bucket: int  # unix-seconds bucket start (UTC, aligned to bucket_size)
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_hcents: int


class TimeseriesResponse(BaseModel):
    bucket_size_seconds: int
    points: list[TimeseriesPoint]


@router.get(
    "/usage/timeseries",
    response_model=TimeseriesResponse,
)
async def get_usage_timeseries(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    start_ts: Annotated[datetime, Query(description="Inclusive lower bound")],
    end_ts: Annotated[datetime, Query(description="Exclusive upper bound")],
    bucket: Annotated[
        str,
        Query(
            description=(
                "Bucket size — 'hour' or 'day'. Determines the granularity of the response."
            ),
            pattern="^(hour|day)$",
        ),
    ] = "day",
    team_id: Annotated[str | None, Query()] = None,
) -> TimeseriesResponse:
    """Aggregate usage records into uniform time buckets.

    Returned points are densely packed — buckets with zero matching
    rows ARE included (with all counters 0), so the chart x-axis has
    no gaps. The window is half-open ``[start_ts, end_ts)`` matching
    the existing /v1/admin/usage convention.
    """
    if end_ts <= start_ts:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "invalid_window",
                "hint": "end_ts must be strictly greater than start_ts",
            },
        )

    bucket_seconds = 3600 if bucket == "hour" else 86_400
    if (end_ts - start_ts).total_seconds() / bucket_seconds > 1000:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "window_too_wide",
                "hint": (
                    f"requested window would produce >1000 buckets at "
                    f"{bucket!r} granularity; pick a smaller window or "
                    "a coarser bucket"
                ),
            },
        )

    # Group by truncated timestamp. SQLAlchemy's func.strftime works on
    # SQLite; Postgres would use date_trunc. We pick the universal
    # approach: bucket on the integer-divided epoch in Python after
    # fetching the matching rows. That's slightly more memory but
    # entirely portable + no dialect branching.
    conditions = [
        UsageRecord.tenant_id == principal.tenant_id,
        UsageRecord.ts >= start_ts,
        UsageRecord.ts < end_ts,
    ]
    if team_id is not None:
        conditions.append(UsageRecord.team_id == team_id)

    rows = (
        await session.execute(
            select(
                UsageRecord.ts,
                UsageRecord.prompt_tokens,
                UsageRecord.completion_tokens,
                UsageRecord.cost_hcents,
            ).where(*conditions)
        )
    ).all()

    # Initialise every bucket in the window to zero so the chart
    # x-axis is dense.
    start_epoch = int(start_ts.timestamp())
    end_epoch = int(end_ts.timestamp())
    bucket_start = (start_epoch // bucket_seconds) * bucket_seconds
    buckets: dict[int, dict[str, int]] = {}
    cur = bucket_start
    while cur < end_epoch:
        buckets[cur] = {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_hcents": 0,
        }
        cur += bucket_seconds

    # Bucket each row.
    for ts, pt, ct, cost in rows:
        epoch = int(ts.timestamp())
        b = (epoch // bucket_seconds) * bucket_seconds
        slot = buckets.get(b)
        if slot is None:
            # Edge case: row's ts is in our SELECT window but rounds
            # below the first or past the last bucket — shouldn't
            # happen with the half-open WHERE, but be defensive.
            continue
        slot["requests"] += 1
        slot["prompt_tokens"] += int(pt)
        slot["completion_tokens"] += int(ct)
        slot["cost_hcents"] += int(cost)

    points = [
        TimeseriesPoint(
            bucket=b,
            requests=v["requests"],
            prompt_tokens=v["prompt_tokens"],
            completion_tokens=v["completion_tokens"],
            cost_hcents=v["cost_hcents"],
        )
        for b, v in sorted(buckets.items())
    ]

    return TimeseriesResponse(
        bucket_size_seconds=bucket_seconds,
        points=points,
    )


# Suppress import-unused warnings for `func` — the dialect-portable
# version of timeseries uses Python bucketing instead. Keeping the
# import in place gives a future "use SQL date_trunc" rewrite a
# one-line landing zone.
_ = func
