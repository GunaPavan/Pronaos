"""Tool-call result cache — memoize (tool_name, args) → result.

Phase 49.

Why this exists
---------------
Agent loops repeatedly call the same tool with the same arguments
during exploratory chats: the user asks about a topic, the model
calls ``get_weather(city="Tokyo")``, the client executes the tool
externally, sends the result back, model synthesises an answer.
Later the user asks a follow-up that triggers the same tool call
again with the same args — but the client has to re-execute the
tool. That's a wasted round trip on deterministic-in-args tools
(``get_weather``, ``lookup_user_by_id``, ``fetch_static_doc``).

This cache memoizes ``(team_id, tool_name, canonical_args_json) →
result_content``. The chat handler:

- **populates** on every chat request that includes a ``tool``
  role message — extracts ``(name, args, result)`` and writes
- **injects** on every chat request whose trailing assistant
  message has ``tool_calls`` awaiting execution — checks the
  cache per pending call, and on hit synthesises a ``tool``
  message in the conversation before forwarding to the LLM

The result: the client's tool execution is skipped on a hit.

Storage shape
-------------
Redis hash per team::

    pronaos:toolcache:{team_id}
        field "{tool_name}|{args_hash}" → JSON-encoded
            {"result": "<content>", "n_hits": <int>, "ts": "..."}

Args canonicalisation uses key-sorted JSON so semantically-identical
calls (``{"city":"Tokyo","unit":"C"}`` vs
``{"unit":"C","city":"Tokyo"}``) collide on the same cache key.
SHA-256 hex of the canonical JSON keeps key length bounded.

TTL is applied at write time and refreshed on every successful
read so steady-state tools stay warm. Default 3600s (1 hour) —
intentionally conservative because tool results age out of
correctness fast.

When NOT to use
---------------
Tools with side effects (``send_email``, ``delete_record``) or
time-sensitive results (``get_stock_price``, ``get_now_utc``) must
NOT be cached. Operator owns the policy decision via the team-level
opt-in flag — there's no per-tool exclusion list in v1; the
operator either trusts all of the team's tools to be deterministic
or leaves the feature off.

Fail-open semantics
-------------------
A Redis outage degrades to "no caching": ``record()`` is a no-op +
log, ``lookup()`` returns None. Same posture as the rate limiter,
agent-turn tracker, prompt-cache observer, and L1 cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from pronaos.logging import get_logger

log = get_logger(__name__)


DEFAULT_TTL_SECONDS = 3600


def canonicalise_args(args: Any) -> str:
    """Return a stable JSON serialisation of tool arguments.

    Key-sorted at every nesting level so ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` produce identical strings. Strings, numbers,
    booleans, None pass through json defaults. Tool args from the
    wire come as either a parsed dict OR a JSON string (OpenAI
    serialises function arguments as a string); we accept both and
    normalise.
    """
    if isinstance(args, str):
        # Parse, then re-serialise canonically. A JSON-encoded string
        # like ``'{"city":"Tokyo"}'`` and a dict ``{"city":"Tokyo"}``
        # should produce the same cache key.
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            # Non-JSON string — treat as opaque, use as-is. (Rare;
            # only happens if the model emitted malformed arguments.)
            return args
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def _args_hash(canonical_args: str) -> str:
    """Truncated SHA-256 — 16 hex chars is enough collision-resistance for a
    per-team per-tool keyspace; keeps Redis field names readable."""
    return hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ToolResultEntry:
    """One cached tool execution result."""

    tool_name: str
    args_hash: str
    result: str
    n_hits: int


class ToolResultCache:
    """Redis-backed memoization of tool execution results.

    Stateless; instantiate once per process and share across requests.
    Both methods async to match the rest of the gateway's I/O. Every
    Redis op is wrapped in fail-open suppression — a backend outage
    quietly disables caching rather than crashing requests.
    """

    def __init__(self, redis: Redis[bytes] | None) -> None:
        self._redis = redis

    @staticmethod
    def _key(team_id: str) -> str:
        return f"pronaos:toolcache:{team_id}"

    @staticmethod
    def _field(tool_name: str, args_hash: str) -> str:
        return f"{tool_name}|{args_hash}"

    async def record(
        self,
        *,
        team_id: str,
        tool_name: str,
        args: Any,
        result: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Record a ``(tool_name, args) → result`` mapping.

        Called when a chat request includes a ``tool`` role message —
        the gateway extracts ``(name, args, result)`` from the
        assistant.tool_calls + tool message pair and writes here.
        Subsequent writes for the same key overwrite (latest result
        wins; an old cached value is never preferred to a freshly
        observed one).

        Fail-open on any Redis error.
        """
        if self._redis is None or not tool_name or not result:
            return
        canonical = canonicalise_args(args)
        ah = _args_hash(canonical)
        key = self._key(team_id)
        field = self._field(tool_name, ah)
        payload = json.dumps(
            {"result": result, "args": canonical, "n_hits": 0},
            separators=(",", ":"),
        )
        try:
            pipe = self._redis.pipeline()
            pipe.hset(key, field, payload)
            pipe.expire(key, max(1, ttl_seconds))
            await pipe.execute()
        except Exception as e:
            log.warning(
                "tool_result_cache.record_failed",
                error=str(e),
                team_id=team_id,
                tool_name=tool_name,
            )

    async def lookup(
        self,
        *,
        team_id: str,
        tool_name: str,
        args: Any,
    ) -> str | None:
        """Look up the cached result for ``(tool_name, args)``.

        Returns the cached result string on hit, ``None`` on miss
        or Redis outage. Increments the per-field hit counter as a
        side effect (best-effort; failure to increment doesn't
        affect the returned result).
        """
        if self._redis is None or not tool_name:
            return None
        canonical = canonicalise_args(args)
        ah = _args_hash(canonical)
        key = self._key(team_id)
        field = self._field(tool_name, ah)
        raw: bytes | None = None
        with contextlib.suppress(Exception):
            raw = await self._redis.hget(key, field)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            result = payload.get("result")
            if not isinstance(result, str):
                return None
            # Best-effort hit-count increment. Re-encode with n_hits+1
            # so the count survives. Not strictly atomic — if two
            # concurrent lookups race the counter, we may undercount
            # by one. Acceptable for a stats counter; we don't need
            # transactional accuracy here.
            payload["n_hits"] = int(payload.get("n_hits", 0)) + 1
            with contextlib.suppress(Exception):
                await self._redis.hset(
                    key, field, json.dumps(payload, separators=(",", ":"))
                )
            return result
        except (ValueError, TypeError):
            return None

    async def snapshot(self, team_id: str) -> list[ToolResultEntry]:
        """Return all cached entries for a team — used by the admin
        GET endpoint to surface what's in the cache.

        Empty list on outage, no observations, or TTL expiry.
        """
        if self._redis is None:
            return []
        raw: dict[bytes, bytes] = {}
        with contextlib.suppress(Exception):
            raw = await self._redis.hgetall(self._key(team_id))
        out: list[ToolResultEntry] = []
        for field, value in raw.items():
            field_str = field.decode() if isinstance(field, bytes) else str(field)
            if "|" not in field_str:
                continue
            tool_name, _, ah = field_str.rpartition("|")
            try:
                payload = json.loads(value)
                result = payload.get("result")
                n_hits = int(payload.get("n_hits", 0))
                if not isinstance(result, str):
                    continue
            except (ValueError, TypeError):
                continue
            out.append(
                ToolResultEntry(
                    tool_name=tool_name,
                    args_hash=ah,
                    result=result,
                    n_hits=n_hits,
                )
            )
        # Sort by hit count desc so the busiest tools surface first
        # in the admin UI.
        out.sort(key=lambda e: (-e.n_hits, e.tool_name, e.args_hash))
        return out

    async def reset(self, team_id: str) -> None:
        """Wipe the team's cache. Used by the admin DELETE endpoint
        + the live verify script's cleanup. Fail-open."""
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.delete(self._key(team_id))
