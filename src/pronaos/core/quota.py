"""Team-level token-budget tracker.

The budget is stored on the ``teams`` table in three columns added by
migration ``0002_quotas``:

- ``monthly_token_budget`` — int, NULL means unlimited.
- ``current_period_tokens`` — running counter, incremented after each
  successful provider call.
- ``period_resets_at`` — when the counter auto-resets. Calendar-month UTC.

Two operations
--------------
- ``check_budget(team_id)`` runs before the provider call. Reads the team
  row, performs lazy rollover if the period has ended, and returns
  ``Allowed | Denied`` based on remaining budget. Cheap: one SELECT, plus
  one UPDATE in the rare case of rollover.
- ``record_usage(team_id, tokens)`` runs after the provider call. A single
  atomic ``UPDATE teams SET current_period_tokens = current_period_tokens + :n``
  — no SELECT-then-UPDATE race.

Concurrency caveat
------------------
``check_budget`` and ``record_usage`` are *not* a transactional unit. If two
in-flight requests both observe "under budget" at near-simultaneous
``check_budget`` calls and both then ``record_usage``, the team can overshoot
the budget by up to (concurrent_requests * average_tokens_per_request). For
the gateway's request rate this is bounded and acceptable. A future
reservation/commit pattern can tighten this if needed.

Failure modes
-------------
- ``check_budget`` is fail-closed (DB unavailable → deny). This matches
  ARCHITECTURE.md's policy: better to surface 503 than silently overspend.
- ``record_usage`` is fail-open (logged but doesn't raise). The provider
  call already succeeded and the client got their response; failing to
  record usage is a metrics gap, not a correctness gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.db.models import Team, next_period_reset
from pronaos.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QuotaResult:
    """Outcome of a single ``check_budget`` call.

    ``tokens_remaining`` and ``retry_after_seconds`` are meaningful only for
    bounded teams (those with a non-NULL budget). Unlimited teams always
    receive ``allowed=True``, ``tokens_remaining=None``.
    """

    allowed: bool
    tokens_remaining: int | None = None
    retry_after_seconds: float = 0.0
    reason: str | None = None

    @classmethod
    def allow_unlimited(cls) -> QuotaResult:
        return cls(allowed=True, tokens_remaining=None)

    @classmethod
    def allow_bounded(cls, remaining: int) -> QuotaResult:
        return cls(allowed=True, tokens_remaining=remaining)

    @classmethod
    def deny_exhausted(cls, retry_after_seconds: float) -> QuotaResult:
        return cls(
            allowed=False,
            tokens_remaining=0,
            retry_after_seconds=retry_after_seconds,
            reason="monthly_budget_exhausted",
        )


# --------------------------------------------------------------------------- #
# Tracker                                                                     #
# --------------------------------------------------------------------------- #


class QuotaTracker:
    """Postgres-authoritative budget tracker. Stateless — one instance per
    process is enough."""

    async def check_budget(
        self,
        session: AsyncSession,
        team_id: str,
        *,
        now: datetime | None = None,
    ) -> QuotaResult:
        """Pre-flight check before a provider call.

        Returns ``Allowed`` if the team is unlimited or still has budget.
        Returns ``Denied`` if the budget is exhausted for the current
        period. Triggers lazy rollover when the period has ended.
        """
        now = now or datetime.now(tz=UTC)

        stmt = select(
            Team.monthly_token_budget,
            Team.current_period_tokens,
            Team.period_resets_at,
        ).where(Team.id == team_id)
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            # Team disappeared between auth and now — fail closed.
            return QuotaResult(
                allowed=False,
                tokens_remaining=0,
                reason="team_not_found",
                retry_after_seconds=0.0,
            )

        budget, used, resets_at = row

        # Normalise: SQLite drops tz on read-back. Treat naive as UTC.
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=UTC)

        # Lazy rollover — first request past the boundary resets the period.
        if now >= resets_at:
            new_resets_at = next_period_reset(now)
            await session.execute(
                update(Team)
                .where(Team.id == team_id)
                .values(current_period_tokens=0, period_resets_at=new_resets_at)
            )
            used, resets_at = 0, new_resets_at

        if budget is None:
            return QuotaResult.allow_unlimited()

        if used >= budget:
            retry_after = max(0.0, (resets_at - now).total_seconds())
            return QuotaResult.deny_exhausted(retry_after_seconds=retry_after)

        return QuotaResult.allow_bounded(remaining=budget - used)

    async def record_usage(
        self,
        session: AsyncSession,
        team_id: str,
        tokens: int,
    ) -> None:
        """Record tokens consumed after a successful provider call.

        Atomic SQL increment — no SELECT race. Fail-open on errors: a
        recording failure is a metrics gap, not a correctness gap, and
        we'd rather not surface a 5xx to the client after the upstream
        already returned 200.
        """
        if tokens <= 0:
            return  # nothing to record

        try:
            await session.execute(
                update(Team)
                .where(Team.id == team_id)
                .values(current_period_tokens=Team.current_period_tokens + tokens)
            )
        except Exception as e:
            log.warning(
                "quota.record_usage_failed",
                team_id=team_id,
                tokens=tokens,
                error=str(e),
            )
