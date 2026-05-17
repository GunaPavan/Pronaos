"""Cache protocol + result types shared by every backend.

Why a Protocol and not an ABC: the cache is wired via duck typing at
``app.state.cache``. Production uses Redis (+ Qdrant in L2); tests use
``NullCache`` or a fake; both satisfy this Protocol without inheritance.
That keeps the test surface narrow and the production code path
single-dispatch.

Why a ``key_payload`` rather than the raw ``ChatCompletionRequest``: the
cache needs to be deterministic across cosmetic JSON-shape variations
(extra fields, key ordering, equivalent ``None``s). The chat handler is
the one place that knows which fields are semantically meaningful — it
canonicalises the request into a stable dict-of-primitives before
asking the cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """Outcome of a cache GET.

    ``hit=False`` and ``response is None`` is the cache-miss case.
    ``hit=True`` carries the cached response dict ready to return to the
    client (same shape as a fresh chat completion's JSON body).

    ``tier`` says which layer produced the hit — useful for metrics
    (``exact`` vs ``semantic``) and for the response's ``X-Pronaos-Cache``
    debug header.
    """

    hit: bool
    response: dict[str, Any] | None = None
    tier: str | None = None  # "exact" | "semantic" | None on miss
    similarity: float | None = None  # only meaningful on semantic hits


class Cache(Protocol):
    """Async key-value cache with per-tenant isolation.

    Implementations are stateless from the caller's perspective — they may
    hold their own connection pools internally. ``aclose()`` releases any
    such state.
    """

    async def get(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
    ) -> CacheLookup:
        """Try to serve the response from cache.

        ``key_payload`` is the canonicalised request shape (the chat
        handler builds this). Implementations decide how to derive a key
        / embedding from it.

        MUST fail-open: backend errors return a miss, never raise. A
        cache outage should never break the gateway.
        """
        ...

    async def put(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """Store a successful response. Fail-open on backend errors."""
        ...

    async def aclose(self) -> None:
        """Release connection pools / executors."""
        ...
