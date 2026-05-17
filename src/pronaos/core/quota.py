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
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.db.models import Team, UsageRecord, next_period_reset
from pronaos.logging import get_logger
from pronaos.observability.otel import get_tracer

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# CompletedCall                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CompletedCall:
    """Everything we know about a successful chat completion.

    Passed to ``QuotaTracker.record_call`` so we can both increment the team's
    running counter AND write a full per-call audit row in one shot.
    """

    tenant_id: str
    team_id: str
    key_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_hcents: int
    request_id: str | None = None
    status: str = "success"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QuotaResult:
    """Outcome of a single ``check_budget`` call.

    ``tokens_remaining`` and ``retry_after_seconds`` are meaningful only for
    bounded teams (those with a non-NULL budget). Unlimited teams always
    receive ``allowed=True``, ``tokens_remaining=None``.

    Note that as of Phase 5.7 a denial may originate from either the token
    budget or the cost budget — the ``reason`` field carries the specific
    code so clients and operators can tell them apart.
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
    def deny_token_exhausted(cls, retry_after_seconds: float) -> QuotaResult:
        return cls(
            allowed=False,
            tokens_remaining=0,
            retry_after_seconds=retry_after_seconds,
            reason="monthly_token_budget_exhausted",
        )

    @classmethod
    def deny_cost_exhausted(cls, retry_after_seconds: float) -> QuotaResult:
        return cls(
            allowed=False,
            tokens_remaining=0,
            retry_after_seconds=retry_after_seconds,
            reason="monthly_cost_budget_exhausted",
        )

    # Back-compat alias for any external callers that still reference the
    # old single-reason form. Treated as the token-exhausted variant.
    @classmethod
    def deny_exhausted(cls, retry_after_seconds: float) -> QuotaResult:
        return cls.deny_token_exhausted(retry_after_seconds)


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

        Returns ``Allowed`` if the team is unlimited *on both* the token
        and cost budgets, or has remaining budget on the bounded ones.
        Returns ``Denied`` if EITHER budget is exhausted — with a reason
        code distinguishing the two so callers can surface the right
        message and dashboards can track them separately.

        Triggers lazy rollover when the period has ended; both counters
        reset together on the calendar-month boundary.

        Emits a ``pronaos.quota.check`` span with the team's headroom
        utilisation as attributes — usable in trace exploration to ask
        "show me requests where the team was over 90% of cost budget".
        """
        # Lazy fetch so tests that swap the tracer provider get the
        # currently-installed one, not whatever was set at import time.
        tracer = get_tracer("pronaos.quota")
        with tracer.start_as_current_span("pronaos.quota.check") as span:
            result = await self._check_budget_inner(session, team_id, now=now)
            span.set_attribute("pronaos.quota.allowed", result.allowed)
            if result.reason:
                span.set_attribute("pronaos.quota.reason", result.reason)
            return result

    async def _check_budget_inner(
        self,
        session: AsyncSession,
        team_id: str,
        *,
        now: datetime | None = None,
    ) -> QuotaResult:
        """The actual budget logic. Split out so the span wrapper stays
        tiny and the inner logic is easy to reason about line-by-line."""
        now = now or datetime.now(tz=UTC)

        stmt = select(
            Team.monthly_token_budget,
            Team.current_period_tokens,
            Team.monthly_cost_hcents_budget,
            Team.current_period_cost_hcents,
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

        token_budget, tokens_used, cost_budget, cost_used, resets_at = row

        # Normalise: SQLite drops tz on read-back. Treat naive as UTC.
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=UTC)

        # Lazy rollover — first request past the boundary resets both
        # counters atomically. Doing them in one UPDATE keeps them from
        # drifting out of sync if a request fails between halves.
        if now >= resets_at:
            new_resets_at = next_period_reset(now)
            await session.execute(
                update(Team)
                .where(Team.id == team_id)
                .values(
                    current_period_tokens=0,
                    current_period_cost_hcents=0,
                    period_resets_at=new_resets_at,
                )
            )
            tokens_used, cost_used, resets_at = 0, 0, new_resets_at

        retry_after = max(0.0, (resets_at - now).total_seconds())

        # ---- Cost budget check (Phase 5.7) ----
        # Cost first: if a team is over its $ limit, deny regardless of
        # token headroom. The token check below would otherwise quietly
        # let an expensive provider through when cheap providers had
        # room left.
        if cost_budget is not None and cost_used >= cost_budget:
            return QuotaResult.deny_cost_exhausted(retry_after_seconds=retry_after)

        # ---- Token budget check (Phase 4) ----
        if token_budget is not None and tokens_used >= token_budget:
            return QuotaResult.deny_token_exhausted(retry_after_seconds=retry_after)

        # Both budgets either NULL (unlimited) or have headroom.
        if token_budget is None and cost_budget is None:
            return QuotaResult.allow_unlimited()

        # For the ``tokens_remaining`` hint, return token-budget headroom
        # if bounded, otherwise NULL — keeps the existing contract intact.
        if token_budget is not None:
            return QuotaResult.allow_bounded(remaining=token_budget - tokens_used)
        return QuotaResult.allow_unlimited()

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

        Phase 5 callers should use :meth:`record_call` instead, which also
        writes a per-call audit row. This method remains for callers that
        only care about the team budget counter.
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

    async def record_call(
        self,
        session: AsyncSession,
        call: CompletedCall,
    ) -> None:
        """Persist a per-call audit row AND bump the team budget counter.

        Two effects in one method because they always happen together on a
        successful response:

        1. Insert one row into ``usage_records`` capturing provider, model,
           tokens, cost, status — the data that drives FinOps dashboards
           and per-tenant chargeback.
        2. Atomically increment the team's running token counter.

        Both are fail-open: a recording failure is logged but does not
        propagate — the chat response has already been sent to the client.
        """
        try:
            session.add(
                UsageRecord(
                    tenant_id=call.tenant_id,
                    team_id=call.team_id,
                    key_id=call.key_id,
                    provider=call.provider,
                    model=call.model,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    cost_hcents=call.cost_hcents,
                    request_id=call.request_id,
                    status=call.status,
                )
            )
            # Build the increment payload once. Skip the UPDATE entirely if
            # both deltas are zero — saves a no-op SQL roundtrip per call.
            updates: dict[str, Any] = {}
            if call.total_tokens > 0:
                updates["current_period_tokens"] = (
                    Team.current_period_tokens + call.total_tokens
                )
            if call.cost_hcents > 0:
                updates["current_period_cost_hcents"] = (
                    Team.current_period_cost_hcents + call.cost_hcents
                )
            if updates:
                await session.execute(
                    update(Team).where(Team.id == call.team_id).values(**updates)
                )
        except Exception as e:
            log.warning(
                "quota.record_call_failed",
                team_id=call.team_id,
                error=str(e),
            )
