"""L1 cache — exact-match keyed on a canonical hash of the request.

Key shape
---------
``cache:exact:{tenant_id}:{model}:{sha256_hex}``

The hash is computed over a JSON-serialised, sort-keyed dump of the
canonicalised key payload (typically ``{messages, temperature, max_tokens}``).
Tenant + model are NOT in the hash — they're separate path segments so a
human inspecting Redis can grep for one tenant's keys without computing
hashes.

Why SHA-256 and not a faster hash: the key universe is small (sub-million
per tenant) and a fast hash like xxhash would save us microseconds we
never spend. SHA-256 is in the stdlib and has zero collision worry.

Tenant isolation
----------------
The tenant_id is a literal path segment in the key. There is **no API
shape** that lets one tenant's request derive another tenant's key —
this is enforced by construction, not by runtime check.

Fail-open
---------
Every Redis operation is wrapped in a try/except that logs and returns
miss / drops the write. The gateway must keep serving when Redis is
down — a cache outage is an availability event, not a correctness one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from pronaos.cache.base import Cache, CacheLookup
from pronaos.logging import get_logger

log = get_logger(__name__)

# Default TTL: 1 hour. Tunable per tenant in a later phase.
DEFAULT_TTL_SECONDS = 3600


class RedisExactCache(Cache):
    """Exact-match cache backed by Redis.

    One client instance per process. The Redis client is async-safe and
    pools connections internally.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    # ------------------------------------------------------------------ #
    # Cache protocol                                                     #
    # ------------------------------------------------------------------ #

    async def get(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
    ) -> CacheLookup:
        key = _build_key(tenant_id=tenant_id, model=model, payload=key_payload)
        try:
            raw = await self._redis.get(key)
        except Exception as e:
            # Fail-open: cache outage → miss, not error.
            log.warning("cache.exact.get_failed", error=str(e))
            return CacheLookup(hit=False)

        if raw is None:
            return CacheLookup(hit=False)

        try:
            response: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            # Corrupted entry — drop it so a future write can replace it.
            log.warning("cache.exact.decode_failed", error=str(e))
            try:
                await self._redis.delete(key)
            except Exception:  # noqa: BLE001 — best effort cleanup
                pass
            return CacheLookup(hit=False)

        return CacheLookup(hit=True, response=response, tier="exact")

    async def put(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        key = _build_key(tenant_id=tenant_id, model=model, payload=key_payload)
        try:
            payload = json.dumps(response, separators=(",", ":"), default=str)
            await self._redis.set(key, payload, ex=self._ttl)
        except Exception as e:
            log.warning("cache.exact.put_failed", error=str(e))

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Key derivation                                                              #
# --------------------------------------------------------------------------- #


def _build_key(*, tenant_id: str, model: str, payload: dict[str, Any]) -> str:
    """Build the canonical Redis key for an (tenant, model, payload) triple.

    The payload is serialised with ``sort_keys=True`` so any equivalent
    Python dict produces the same hash regardless of insertion order or
    Python's hash-randomisation seed. This is the property that makes
    cache hits reproducible across processes and restarts.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cache:exact:{tenant_id}:{model}:{digest}"
