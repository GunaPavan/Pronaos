"""Prompt-cache hit-rate observer — runtime input to `prompt-cache-aware-cheapest`.

Phase 47.

Why this exists
---------------
Phases 34 (Anthropic) and 35 (OpenAI) extract per-call prompt-cache
token counts from upstream responses and surface them on the response
body + headers. The data is there but it's per-call — useful for
FinOps audit, not for routing decisions.

Phase 46 (tool-use-aware routing) showed how a per-team per-model
*stored* signal can feed `select_model`. The signal in Phase 46 came
from a manual eval run (`eval_tool_use_accuracy.py`); operators
PUT scores to the team via the admin API.

Prompt-cache hit rates are different: they emerge continuously from
live traffic. RAG / agent workloads with stable system prompts will
show high cache hit rates; chat workloads with random prompts will
show ~0%. Hard-coding a snapshot doesn't fit. So we observe.

This module is the observer: per `(team_id, fqmn)` rolling totals
in Redis, written on every chat response that carries cache token
counts, read at routing time by the scorer's
`PROMPT_CACHE_AWARE_CHEAPEST` branch.

Storage shape
-------------
One Redis hash per team::

    pronaos:pcache:{team_id}
        field "{fqmn}:prompt"  -> cumulative prompt_tokens (non-cached portion)
        field "{fqmn}:cached"  -> cumulative cache_read_tokens
        field "{fqmn}:n"       -> sample count
        field "{fqmn}:saved"   -> cumulative cache_saved_hcents (informational)

A team-level TTL is applied on first write and refreshed on every
update; a team that goes silent past the TTL releases its observation
state (sample counts reset to zero). The TTL defaults to 14 days —
long enough that a steady RAG workload's signal survives a weekend
without traffic, short enough that abandoned teams don't fill Redis
indefinitely.

Fail-open semantics
-------------------
A Redis outage degrades to "no observations" (record is a no-op + log;
snapshot returns empty). The scorer's `PROMPT_CACHE_AWARE_CHEAPEST`
branch then falls through to plain `cheapest`. Same posture as the
rate limiter, agent-turn tracker, and L1 cache.

Why a single hash per team (not per-fqmn keys)
-----------------------------------------------
A snapshot for one team is one HGETALL — atomic, one round trip,
no SCAN. The hash grows at most O(N_models_in_catalog * 4 fields),
which is small (< 100 fields per team in practice). Per-fqmn keys
would multiply the round-trip cost on snapshot, which is on the
hot routing path.
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
class PromptCacheStat:
    """One per-model observation snapshot for a single team.

    ``hit_rate`` is ``cached_tokens / (cached_tokens + prompt_tokens)``
    where ``prompt_tokens`` is the non-cached input portion
    (Phases 34/35 already normalise: Anthropic reports them separately;
    OpenAI subtracts in the adapter). When ``cached + prompt == 0`` the
    rate is 0.0 — the model was observed but never paid cache cost.
    """

    fqmn: str
    n_samples: int
    prompt_tokens: int
    cached_tokens: int
    saved_hcents: int

    @property
    def hit_rate(self) -> float:
        denom = self.prompt_tokens + self.cached_tokens
        if denom <= 0:
            return 0.0
        return self.cached_tokens / denom


class PromptCacheObserver:
    """Redis-backed rolling-totals observer for prompt-cache hit rates.

    Stateless; instantiate once per process and share. Both methods are
    async to match the rest of the gateway's I/O surface; every Redis
    op is wrapped in fail-open suppression so a backend outage doesn't
    poison the routing path.
    """

    def __init__(self, redis: Redis[bytes] | None) -> None:
        # ``None`` is legitimate: when Redis isn't configured, both
        # methods are no-ops. The scorer then sees an empty snapshot
        # and falls through to plain cheapest.
        self._redis = redis

    @staticmethod
    def _key(team_id: str) -> str:
        return f"pronaos:pcache:{team_id}"

    async def record(
        self,
        *,
        team_id: str,
        fqmn: str,
        prompt_tokens: int,
        cached_tokens: int,
        saved_hcents: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Add one observation for ``(team_id, fqmn)``.

        Called from the chat handler after every successful upstream
        response. ``prompt_tokens`` is the NON-cached input token count
        (the same number the adapter normalised for billing); the
        observer's job is to learn what fraction of total input tokens
        was served from cache for this model over time.

        ``saved_hcents`` is informational — what the cache saved on
        THIS call vs. paying full input rate. Reported via snapshot so
        operators can see "this team saved $X via Anthropic caching
        last 14 days." Routing math uses ``cached_tokens / total`` (the
        hit rate), not the saved-cost figure.

        Fail-open: any Redis exception is logged and swallowed. The
        next record-or-snapshot just sees one missing observation.
        """
        if self._redis is None:
            return
        if prompt_tokens <= 0 and cached_tokens <= 0:
            # Nothing to learn from — either the call carried no input
            # (impossible in practice) or the provider didn't return
            # token counts. Either way, no signal.
            return
        key = self._key(team_id)
        try:
            pipe = self._redis.pipeline()
            pipe.hincrby(key, f"{fqmn}:prompt", max(0, prompt_tokens))
            pipe.hincrby(key, f"{fqmn}:cached", max(0, cached_tokens))
            pipe.hincrby(key, f"{fqmn}:n", 1)
            pipe.hincrby(key, f"{fqmn}:saved", max(0, saved_hcents))
            pipe.expire(key, max(1, ttl_seconds))
            await pipe.execute()
        except Exception as e:
            log.warning(
                "prompt_cache_observer.record_failed",
                error=str(e),
                team_id=team_id,
                fqmn=fqmn,
            )

    async def snapshot(self, team_id: str) -> dict[str, PromptCacheStat]:
        """Return ``{fqmn: PromptCacheStat}`` for every observed model.

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
        # kind is one of prompt/cached/n/saved.
        by_fqmn: dict[str, dict[str, int]] = {}
        for k, v in raw.items():
            field = k.decode() if isinstance(k, bytes) else str(k)
            if ":" not in field:
                continue
            fqmn, _, kind = field.rpartition(":")
            if not fqmn or kind not in ("prompt", "cached", "n", "saved"):
                continue
            by_fqmn.setdefault(fqmn, {})[kind] = _to_int(v)
        out: dict[str, PromptCacheStat] = {}
        for fqmn, fields in by_fqmn.items():
            out[fqmn] = PromptCacheStat(
                fqmn=fqmn,
                n_samples=fields.get("n", 0),
                prompt_tokens=fields.get("prompt", 0),
                cached_tokens=fields.get("cached", 0),
                saved_hcents=fields.get("saved", 0),
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
