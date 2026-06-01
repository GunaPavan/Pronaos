"""Reasoning-ratio observer — runtime input to ``reasoning-aware-cheapest``.

Phase 57.

Why this exists
---------------
Phase 56 surfaced reasoning tokens uniformly across five deployment
paths (Anthropic direct + Bedrock + Vertex, OpenAI o1/o3, DeepSeek R1,
Vertex Gemini). The count lands on every chunk and feeds the
``X-Pronaos-Reasoning-Tokens`` header + the
``pronaos_reasoning_tokens_total`` Prometheus counter, but the
data is per-call — useful for FinOps audit, not for routing.

Phase 47 (prompt-cache-aware routing) showed how a per-team per-model
*observation* of an upstream-reported signal can feed ``select_model``
via a Redis-backed observer. This module is the parallel: per
``(team_id, fqmn)`` rolling totals of completion tokens vs reasoning
tokens, written on every chat response that surfaces a non-zero
reasoning count, read at routing time by the scorer's
``REASONING_AWARE_CHEAPEST`` branch.

Why an observation and not a stored score
-----------------------------------------
Reasoning behaviour is *workload-dependent*, not model-static. The
same Anthropic Claude 4 Opus call with "what's 2+2?" burns ~0
reasoning tokens; the same call with "prove the Riemann hypothesis"
burns thousands. A static "reasoning rate" stored per-model would
overweight or underweight depending on the team's actual workload.

A rolling observation captures the team's *specific* traffic
profile: how much reasoning each model actually emits on the kind
of prompts THIS team sends. The router then picks the cheapest
model under the team's real workload, not a synthetic benchmark.

Storage shape
-------------
One Redis hash per team::

    pronaos:reasoning:{team_id}
        field "{fqmn}:completion"  -> cumulative completion_tokens
        field "{fqmn}:reasoning"   -> cumulative reasoning_tokens
        field "{fqmn}:n"           -> sample count

A team-level TTL is applied on first write and refreshed on every
update; a team that goes silent past the TTL releases its
observation state. The TTL defaults to 14 days — same as Phase 47.

Fail-open semantics
-------------------
A Redis outage degrades to "no observations" (record is a no-op + log;
snapshot returns empty). The scorer's ``REASONING_AWARE_CHEAPEST``
branch then falls through to plain ``cheapest``. Same posture as
``PromptCacheObserver``, the rate limiter, and the L1 cache.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from pronaos.logging import get_logger

log = get_logger(__name__)


DEFAULT_TTL_SECONDS = 14 * 24 * 3600  # 14 days


@dataclass(frozen=True, slots=True)
class ReasoningStat:
    """One per-model reasoning-ratio observation for a single team.

    ``ratio`` is ``reasoning_tokens / completion_tokens`` (the fraction
    of billable output that was reasoning content). ``completion_tokens``
    in Pronaos's schema is the post-Phase-56 figure — for Gemini that
    means it INCLUDES ``thoughtsTokenCount``; for Anthropic the
    estimate goes on ``reasoning_tokens`` but not on completion_tokens
    (Anthropic counts thinking IN output already). So the ratio is
    "reasoning-fraction of billable output," directly meaningful for
    routing math.

    When ``completion_tokens == 0`` the ratio is 0.0 — the model was
    observed but never paid output cost.
    """

    fqmn: str
    n_samples: int
    completion_tokens: int
    reasoning_tokens: int

    @property
    def ratio(self) -> float:
        if self.completion_tokens <= 0:
            return 0.0
        return self.reasoning_tokens / self.completion_tokens


class ReasoningObserver:
    """Redis-backed rolling-totals observer for per-model reasoning ratios.

    Stateless; instantiate once per process and share. Both methods
    are async to match the rest of the gateway's I/O surface; every
    Redis op is wrapped in fail-open suppression so a backend outage
    doesn't poison the routing path.
    """

    def __init__(self, redis: Redis[bytes] | None) -> None:
        # ``None`` is legitimate: when Redis isn't configured, both
        # methods are no-ops. The scorer then sees an empty snapshot
        # and falls through to plain cheapest.
        self._redis = redis

    @staticmethod
    def _key(team_id: str) -> str:
        return f"pronaos:reasoning:{team_id}"

    async def record(
        self,
        *,
        team_id: str,
        fqmn: str,
        completion_tokens: int,
        reasoning_tokens: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Add one observation for ``(team_id, fqmn)``.

        Called from the chat handler after every successful chat
        completion. ``completion_tokens`` is the per-chunk billable
        output (the same number cost_cents uses); ``reasoning_tokens``
        is the portion that was reasoning.

        Fail-open: any Redis exception is logged and swallowed. The
        next record-or-snapshot just sees one missing observation.

        We deliberately record every call, including reasoning_tokens=0
        cases — that's part of the signal. A team that runs Claude 4
        with thinking mode disabled for 90% of its calls should see
        Claude's observed ratio drift toward 0 over time, NOT stay
        artificially high from the 10% of thinking-mode samples.
        """
        if self._redis is None:
            return
        if completion_tokens <= 0:
            # No billable output to attribute reasoning to. Skipping
            # avoids polluting the ratio denominator with zero-output
            # calls (auth errors, content filtered, etc.).
            return
        key = self._key(team_id)
        try:
            pipe = self._redis.pipeline()
            pipe.hincrby(key, f"{fqmn}:completion", max(0, completion_tokens))
            pipe.hincrby(key, f"{fqmn}:reasoning", max(0, reasoning_tokens))
            pipe.hincrby(key, f"{fqmn}:n", 1)
            pipe.expire(key, max(1, ttl_seconds))
            await pipe.execute()
        except Exception as e:
            log.warning(
                "reasoning_observer.record_failed",
                error=str(e),
                team_id=team_id,
                fqmn=fqmn,
            )

    async def snapshot(self, team_id: str) -> dict[str, ReasoningStat]:
        """Return ``{fqmn: ReasoningStat}`` for every observed model.

        One HGETALL — atomic, one round trip. The dict is empty when:

        - Redis is unavailable (fail-open path)
        - The team has never produced an observation
        - The team's observations TTL'd out

        In all three cases the scorer sees an empty dict and falls
        through to plain ``cheapest`` for that request.
        """
        if self._redis is None:
            return {}
        key = self._key(team_id)
        raw: dict[bytes, bytes] = {}
        with contextlib.suppress(Exception):
            raw = await self._redis.hgetall(key)
        if not raw:
            return {}
        # Group fields by fqmn. Each field is "{fqmn}:{kind}" where
        # kind is one of completion/reasoning/n.
        by_fqmn: dict[str, dict[str, int]] = {}
        for k, v in raw.items():
            field = k.decode() if isinstance(k, bytes) else str(k)
            if ":" not in field:
                continue
            fqmn, _, kind = field.rpartition(":")
            if not fqmn or kind not in ("completion", "reasoning", "n"):
                continue
            by_fqmn.setdefault(fqmn, {})[kind] = _to_int(v)
        out: dict[str, ReasoningStat] = {}
        for fqmn, fields in by_fqmn.items():
            out[fqmn] = ReasoningStat(
                fqmn=fqmn,
                n_samples=fields.get("n", 0),
                completion_tokens=fields.get("completion", 0),
                reasoning_tokens=fields.get("reasoning", 0),
            )
        return out

    async def reset(self, team_id: str) -> None:
        """Drop all observations for a team. Used by admin endpoint +
        live-verify cleanup. Fail-open like the other methods."""
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.delete(self._key(team_id))


def _to_int(raw: Any) -> int:
    """Parse a Redis-returned field as int; missing or junk → 0."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0
