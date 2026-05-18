"""L2 cache — semantic similarity lookup via Qdrant.

Collection layout
-----------------
A single collection (``pronaos_semantic_cache``) for the whole gateway,
with ``tenant_id`` and ``model`` carried in the payload. Every query
filters on both, so tenant A cannot retrieve tenant B's cached
responses even if a paraphrase would have matched.

Why one collection, not per-tenant: per-tenant collections force a
``collection.exists?`` check on every request, racy creation, and grow
unbounded with customer count. Payload filtering is Qdrant's native
fast path — the cost is one indexed scan, the win is operational
simplicity.

Threshold tuning
----------------
Default cosine similarity threshold is **0.95** — empirically the value
where paraphrases ("hi" ≈ "hello") hit but distinct intents
("what's the weather" ≠ "what's the time") miss for all-MiniLM-L6-v2.
A future per-tenant override (in ``teams.semantic_cache_threshold``)
lets each customer trade hit-rate for precision; for now it's a fixed
process-level constant tuned by experiment.

Vector normalisation
--------------------
The embedder returns unit vectors, so Qdrant cosine === dot product
internally. We still declare the distance as COSINE because that's the
semantically-meaningful name; the math is identical and the read path
stays correct if a future embedder forgets to normalise.

Fail-open
---------
Every Qdrant call is wrapped in try/except → miss / no-op. Gateway must
serve traffic when Qdrant is down or slow.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any, Protocol

from pronaos.cache.base import Cache, CacheLookup
from pronaos.cache.embedding import EmbeddingProvider
from pronaos.logging import get_logger

log = get_logger(__name__)

COLLECTION_NAME = "pronaos_semantic_cache"
DEFAULT_SIMILARITY_THRESHOLD = 0.95


class QdrantClientLike(Protocol):
    """Duck-typed Qdrant client.

    Real production: ``qdrant_client.AsyncQdrantClient`` against the
    Qdrant server. Tests: an in-process fake that implements the same
    method shape (``ensure_collection``, ``upsert``, ``query_points``).
    Both satisfy this Protocol without inheritance.
    """

    async def get_collections(self) -> Any: ...

    async def create_collection(self, collection_name: str, vectors_config: Any) -> Any: ...

    async def upsert(self, collection_name: str, points: Any) -> Any: ...

    async def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        score_threshold: float | None = None,
    ) -> Any: ...

    async def close(self) -> None: ...


class QdrantSemanticCache(Cache):
    """Embedding-based cache on top of Qdrant.

    Construction is async (collection creation requires a network call)
    so the factory awaits ``ensure_ready()`` once at startup.
    """

    def __init__(
        self,
        *,
        client: QdrantClientLike,
        embedder: EmbeddingProvider,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._collection = COLLECTION_NAME

    # ------------------------------------------------------------------ #
    # Startup                                                            #
    # ------------------------------------------------------------------ #

    async def ensure_ready(self) -> None:
        """Create the collection if it doesn't exist.

        Idempotent — called once at app startup. ``get_collections``
        plus check is cheaper than a try-then-create that would
        normally race on multi-replica deploys; for a single-process
        gateway this is fine, and Qdrant's own create is idempotent
        anyway (it errors on duplicate, which we swallow)."""
        try:
            from qdrant_client.models import Distance, VectorParams

            collections = await self._client.get_collections()
            existing = {c.name for c in collections.collections}
            if self._collection in existing:
                return
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=self._embedder.dimension, distance=Distance.COSINE
                ),
            )
            log.info(
                "cache.semantic.collection_created",
                collection=self._collection,
                dimension=self._embedder.dimension,
            )
        except Exception as e:
            log.warning("cache.semantic.ensure_ready_failed", error=str(e))

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
        text = _extract_query_text(key_payload)
        if not text:
            return CacheLookup(hit=False)

        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            vectors = await self._embedder.embed([text])
            if not vectors:
                return CacheLookup(hit=False)

            results = await self._client.query_points(
                collection_name=self._collection,
                query=vectors[0],
                query_filter=Filter(
                    must=[
                        FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                        FieldCondition(key="model", match=MatchValue(value=model)),
                    ]
                ),
                limit=1,
                score_threshold=self._threshold,
            )
        except Exception as e:
            log.warning("cache.semantic.get_failed", error=str(e))
            return CacheLookup(hit=False)

        points = getattr(results, "points", None) or []
        if not points:
            return CacheLookup(hit=False)

        top = points[0]
        payload: dict[str, Any] = top.payload or {}
        response = payload.get("response")
        if not isinstance(response, dict):
            return CacheLookup(hit=False)

        return CacheLookup(
            hit=True,
            response=response,
            tier="semantic",
            similarity=float(top.score),
        )

    async def put(
        self,
        *,
        tenant_id: str,
        model: str,
        key_payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        text = _extract_query_text(key_payload)
        if not text:
            return

        try:
            from qdrant_client.models import PointStruct

            vectors = await self._embedder.embed([text])
            if not vectors:
                return

            point = PointStruct(
                id=uuid.uuid4().hex,
                vector=vectors[0],
                payload={
                    "tenant_id": tenant_id,
                    "model": model,
                    "response": response,
                    "created_at": time.time(),
                },
            )
            await self._client.upsert(collection_name=self._collection, points=[point])
        except Exception as e:
            log.warning("cache.semantic.put_failed", error=str(e))

    async def aclose(self) -> None:
        # Best-effort teardown; a flaky Qdrant on shutdown shouldn't
        # block the gateway from stopping cleanly.
        with contextlib.suppress(Exception):
            await self._client.close()
        await self._embedder.aclose()


# --------------------------------------------------------------------------- #
# Query-text extraction                                                       #
# --------------------------------------------------------------------------- #


def _extract_query_text(payload: dict[str, Any]) -> str:
    """Pull the user-facing query text from the cache payload.

    Embedding the FULL conversation would let cache hits from one
    conversation leak into a different one with the same final user
    turn — which is the worst kind of bug (looks identical, semantically
    different context). So we embed only the LAST user message; that
    matches the FAQ-style traffic where semantic caching is most useful.

    Future refinement: include a hash of the prior conversation as a
    secondary filter so true "same question, same context" benefits from
    L2 while "same question, different context" still misses cleanly.
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return ""
    # Walk backwards to find the latest user turn.
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""
