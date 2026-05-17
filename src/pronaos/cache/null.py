"""No-op cache for when caching is disabled or unavailable.

Used:
- in tests that don't care about the cache path
- in production when Redis isn't configured
- as a fallback when cache construction fails at startup

Always returns miss; ``put`` is silently dropped. Pass-through semantics
let every call site stay cache-aware without conditional branches.
"""

from __future__ import annotations

from typing import Any

from pronaos.cache.base import Cache, CacheLookup


class NullCache(Cache):
    """Drop-in cache that never stores anything."""

    async def get(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
    ) -> CacheLookup:
        return CacheLookup(hit=False)

    async def put(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None
