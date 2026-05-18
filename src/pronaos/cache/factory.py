"""Cache construction at startup.

Three modes:

- ``settings.redis_url`` unset → ``NullCache`` (cache disabled).
- Redis set, ``semantic_cache_enabled=False`` → L1-only (``RedisExactCache``).
- Redis set, ``semantic_cache_enabled=True`` → ``LayeredCache(L1, L2)``
  where L2 is ``QdrantSemanticCache`` backed by sentence-transformers.

The semantic layer is opt-in because the local embedding model boots
PyTorch (~1-2 s startup, ~250 MB RAM). CI runs and dev sessions that
don't exercise semantic cache keep it off.

Tests build caches directly; this factory is only the startup path.
"""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from pronaos.cache.base import Cache
from pronaos.cache.exact import RedisExactCache
from pronaos.cache.null import NullCache
from pronaos.config import Settings
from pronaos.logging import get_logger

log = get_logger(__name__)


async def make_cache(settings: Settings) -> Cache:
    """Build the active cache backend based on configuration.

    Async because the semantic-cache path needs to await
    ``ensure_ready()`` (Qdrant collection check / creation).
    """
    if not settings.redis_url:
        log.info("cache.disabled", reason="no_redis_url")
        return NullCache()

    # Build L1.
    try:
        redis_client: Redis[bytes] = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=False,
        )
    except Exception as e:
        log.warning("cache.l1_construct_failed", error=str(e))
        return NullCache()

    l1 = RedisExactCache(redis_client)
    log.info("cache.l1_enabled", backend="redis_exact", redis_url=settings.redis_url)

    if not settings.semantic_cache_enabled:
        return l1

    # Build L2 (semantic). On any failure, fall back to L1-only so a
    # broken Qdrant doesn't take down the gateway.
    try:
        from qdrant_client import AsyncQdrantClient

        from pronaos.cache.embedding import SentenceTransformerEmbedder
        from pronaos.cache.layered import LayeredCache
        from pronaos.cache.semantic import QdrantSemanticCache

        embedder = SentenceTransformerEmbedder()
        qdrant = AsyncQdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key
        )
        # ``AsyncQdrantClient`` has a richer signature than our
        # ``QdrantClientLike`` Protocol declares (kwargs we don't use),
        # so mypy structurally narrows past it. ``cast`` here is honest
        # — the runtime class implements every method we need.
        from typing import cast

        from pronaos.cache.semantic import QdrantClientLike

        l2 = QdrantSemanticCache(
            client=cast(QdrantClientLike, qdrant),
            embedder=embedder,
            similarity_threshold=settings.semantic_cache_threshold,
        )
        await l2.ensure_ready()
        log.info(
            "cache.l2_enabled",
            backend="qdrant_semantic",
            qdrant_url=settings.qdrant_url,
            threshold=settings.semantic_cache_threshold,
        )
        return LayeredCache(l1=l1, l2=l2)
    except Exception as e:
        log.warning("cache.l2_construct_failed_falling_back_to_l1", error=str(e))
        return l1
