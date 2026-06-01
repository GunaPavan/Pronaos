"""Agent-turn budget tracker — multi-call cost/token cap per agent execution.

Phase 30.

Why this exists
---------------
The per-team monthly token + cost budgets (Phase 4 / 5.7) cap *aggregate*
spend over a calendar period. They don't help with the actual failure
mode that burns a team's monthly budget in one go: a runaway agent
loop that calls the gateway 100x in a few minutes. Each call sits
within the team's per-key RPS limit and within the monthly budget at
that instant; only on call ~80 does the per-month total tip over —
by which point you've already spent the budget.

This module is the missing per-execution cap. The client supplies an
``X-Pronaos-Agent-Turn-ID`` header (an opaque string, typically a
UUID generated client-side) on every call belonging to the same
logical agent turn. The gateway accumulates running totals in Redis
under that turn-id and denies the call that would push the team over
either ``agent_turn_budget_tokens`` or ``agent_turn_budget_cost_hcents``.

Storage shape
-------------
Redis hash key ``pronaos:agentturn:{team_id}:{turn_id}`` with fields:

- ``tokens``    — cumulative prompt+completion tokens
- ``cost_hcents`` — cumulative cost
- ``calls``    — count of calls (informational; useful for the
                  ``X-Pronaos-Agent-Turn-Calls`` response header)

TTL is set to ``agent_turn_ttl_seconds`` on first write and refreshed
on every increment. A turn that goes silent past the TTL releases its
budget (the counters expire) — matches the semantics of "agent turns
are short-lived; we don't want a forgotten turn-id pinning budget
forever."

Fail-open semantics
-------------------
A Redis outage degrades to "no agent-turn gate" (allow_call returns
True, record_call is a no-op + log). The gateway must keep serving
under cache-storage failure. Same posture as the rate limiter and L1
cache.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from redis.asyncio import Redis

from pronaos.logging import get_logger

log = get_logger(__name__)


DEFAULT_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class AgentTurnDecision:
    """Result of a pre-call budget check.

    ``allowed=False`` carries the reason code and the remaining
    budget at the time of the deny — clients use the remaining
    numbers to plan a smaller follow-up call or to surface a
    "budget exhausted" message to the human user.
    """

    allowed: bool
    reason: str | None = None
    remaining_tokens: int | None = None
    remaining_cost_hcents: int | None = None
    used_tokens: int = 0
    used_cost_hcents: int = 0
    used_calls: int = 0


class AgentTurnTracker:
    """Redis-backed accumulator for per-agent-turn budget enforcement.

    Methods are async to match the rest of the gateway's I/O surface;
    every Redis op is wrapped in fail-open suppression so a backend
    outage degrades gracefully.

    The tracker is stateless — instantiate once per process and share.
    """

    def __init__(self, redis: Redis[bytes] | None) -> None:
        # ``None`` is a legitimate state — when the gateway runs without
        # Redis configured, the tracker is a no-op (every check
        # returns allowed=True, record is a no-op). Lets ops opt into
        # agent-turn budgets per-environment without separate boot
        # flags.
        self._redis = redis

    @staticmethod
    def _key(team_id: str, turn_id: str) -> str:
        return f"pronaos:agentturn:{team_id}:{turn_id}"

    async def check(
        self,
        *,
        team_id: str,
        turn_id: str,
        budget_tokens: int | None,
        budget_cost_hcents: int | None,
        next_estimate_tokens: int = 0,
        next_estimate_cost_hcents: int = 0,
    ) -> AgentTurnDecision:
        """Pre-call check — would this call push the team over budget?

        Reads the current running totals for ``(team_id, turn_id)``
        from Redis, adds the caller-supplied estimates for the
        next call, and returns ``allowed=False`` with the appropriate
        reason if either budget is set and the projection exceeds it.

        Returns ``allowed=True`` when:
          * Redis is unavailable (fail-open)
          * No turn-id present (means client isn't using the feature)
          * Either budget is NULL on the team (unlimited)
          * The estimate fits inside the remaining budget

        The caller passes preflight estimates so we deny BEFORE the
        upstream call when the projection is clear. Post-flight
        ``record`` adds the actual values.
        """
        if self._redis is None or not turn_id:
            return AgentTurnDecision(allowed=True)
        if budget_tokens is None and budget_cost_hcents is None:
            return AgentTurnDecision(allowed=True)

        used_tokens, used_cost, used_calls = await self._read(team_id, turn_id)

        # Project the post-call totals using the caller's estimate.
        projected_tokens = used_tokens + max(0, next_estimate_tokens)
        projected_cost = used_cost + max(0, next_estimate_cost_hcents)

        remaining_tokens: int | None = None
        remaining_cost: int | None = None

        if budget_tokens is not None:
            remaining_tokens = max(0, budget_tokens - used_tokens)
            if projected_tokens > budget_tokens:
                return AgentTurnDecision(
                    allowed=False,
                    reason="agent_turn_token_budget_exhausted",
                    remaining_tokens=remaining_tokens,
                    remaining_cost_hcents=(
                        max(0, budget_cost_hcents - used_cost) if budget_cost_hcents else None
                    ),
                    used_tokens=used_tokens,
                    used_cost_hcents=used_cost,
                    used_calls=used_calls,
                )
        if budget_cost_hcents is not None:
            remaining_cost = max(0, budget_cost_hcents - used_cost)
            if projected_cost > budget_cost_hcents:
                return AgentTurnDecision(
                    allowed=False,
                    reason="agent_turn_cost_budget_exhausted",
                    remaining_tokens=remaining_tokens,
                    remaining_cost_hcents=remaining_cost,
                    used_tokens=used_tokens,
                    used_cost_hcents=used_cost,
                    used_calls=used_calls,
                )

        return AgentTurnDecision(
            allowed=True,
            remaining_tokens=remaining_tokens,
            remaining_cost_hcents=remaining_cost,
            used_tokens=used_tokens,
            used_cost_hcents=used_cost,
            used_calls=used_calls,
        )

    async def record(
        self,
        *,
        team_id: str,
        turn_id: str,
        tokens: int,
        cost_hcents: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Post-call record — add the actual cost of the completed call
        to the per-turn counters. Refreshes the TTL on the key so a
        long-running turn keeps its budget allocation.

        Fail-open: any Redis exception is logged and swallowed. The
        next call's ``check`` will just see slightly stale totals.
        """
        if self._redis is None or not turn_id:
            return
        if tokens <= 0 and cost_hcents <= 0:
            return
        key = self._key(team_id, turn_id)
        try:
            # HINCRBY is atomic per-field. Two HINCRBYs aren't atomic
            # together — but a tiny race (one field updated, the
            # other not yet) is harmless for our purposes (the next
            # check sees a slightly conservative or slightly stale
            # number). A Lua script could make this strictly atomic
            # but it's overkill for the use case.
            pipe = self._redis.pipeline()
            pipe.hincrby(key, "tokens", max(0, tokens))
            pipe.hincrby(key, "cost_hcents", max(0, cost_hcents))
            pipe.hincrby(key, "calls", 1)
            pipe.expire(key, max(1, ttl_seconds))
            await pipe.execute()
        except Exception as e:
            log.warning("agent_turn.record_failed", error=str(e), team_id=team_id)

    async def _read(self, team_id: str, turn_id: str) -> tuple[int, int, int]:
        """Read ``(tokens, cost_hcents, calls)`` for one turn-id.

        Returns zeros on fresh turn, Redis outage, or malformed entry —
        all of which are equivalent for the caller's purpose. Wrapped
        in ``contextlib.suppress`` so the fail-open path is one branch
        rather than two.
        """
        assert self._redis is not None  # caller checked
        key = self._key(team_id, turn_id)
        with contextlib.suppress(Exception):
            raw = await self._redis.hmget(key, "tokens", "cost_hcents", "calls")
            tokens = _to_int(raw[0])
            cost = _to_int(raw[1])
            calls = _to_int(raw[2])
            return tokens, cost, calls
        return 0, 0, 0


def _to_int(raw: bytes | None) -> int:
    """Parse a Redis-returned field as int; missing or junk → 0."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0
