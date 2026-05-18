"""Unit tests for ``QdrantSemanticCache`` + ``LayeredCache``.

Real Qdrant is a Docker dependency we don't want in unit tests, so a
``FakeQdrantClient`` here implements the small slice of the client
surface ``QdrantSemanticCache`` uses (``query_points``, ``upsert``,
``get_collections``, ``create_collection``). Similarly, a
``StubEmbedder`` returns deterministic vectors keyed on input text so
similarity is predictable and we can pin the threshold-tuning test.

The integration story (real Qdrant + real sentence-transformers) is
covered by the live observability stack at ``docker compose up`` —
unit tests stay fast and hermetic.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest

from pronaos.cache.embedding import EmbeddingProvider
from pronaos.cache.exact import RedisExactCache
from pronaos.cache.layered import LayeredCache
from pronaos.cache.null import NullCache
from pronaos.cache.semantic import QdrantSemanticCache

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class StubEmbedder(EmbeddingProvider):
    """Deterministic test embedder.

    Maps each known input string to a hand-picked unit vector. Unknown
    inputs get a default orthogonal vector so similarity to anything
    known is zero. Lets every threshold-related assertion be exact.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self._fallback = [0.0, 0.0, 0.0, 1.0]

    @property
    def dimension(self) -> int:
        return 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors.get(t, list(self._fallback)) for t in texts]

    async def aclose(self) -> None:
        return None


class _FakeScored:
    def __init__(self, payload: dict[str, Any], score: float) -> None:
        self.payload = payload
        self.score = score


class _FakeQueryResult:
    def __init__(self, points: list[_FakeScored]) -> None:
        self.points = points


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollections:
    def __init__(self, names: list[str]) -> None:
        self.collections = [_FakeCollection(n) for n in names]


class FakeQdrantClient:
    """Minimum surface area to satisfy ``QdrantClientLike``.

    Stores points in a list per collection; ``query_points`` does a
    real cosine-sim scan against the matching points so threshold
    semantics behave like the real Qdrant — which is what the
    threshold-tuning test cares about.
    """

    def __init__(self) -> None:
        self._collections: dict[str, list[Any]] = {}

    async def get_collections(self) -> _FakeCollections:
        return _FakeCollections(list(self._collections.keys()))

    async def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self._collections.setdefault(collection_name, [])

    async def upsert(self, collection_name: str, points: list[Any]) -> None:
        self._collections.setdefault(collection_name, []).extend(points)

    async def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        score_threshold: float | None = None,
    ) -> _FakeQueryResult:
        # Walk all stored points, apply payload filter, score by cosine.
        # Real Qdrant indexes this; for the test we brute-force.
        results: list[_FakeScored] = []
        for p in self._collections.get(collection_name, []):
            payload = getattr(p, "payload", None) or {}
            if not _payload_passes_filter(payload, query_filter):
                continue
            score = _cosine_sim(query, getattr(p, "vector", []))
            if score_threshold is not None and score < score_threshold:
                continue
            results.append(_FakeScored(payload=payload, score=score))
        results.sort(key=lambda r: r.score, reverse=True)
        return _FakeQueryResult(points=results[:limit])

    async def close(self) -> None:
        return None


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _payload_passes_filter(payload: dict[str, Any], qfilter: Any) -> bool:
    """Tiny port of Qdrant's ``must`` AND-filter: every ``FieldCondition``
    must match by exact value. Enough for our tenant_id + model gate."""
    if qfilter is None:
        return True
    must = getattr(qfilter, "must", None) or []
    for cond in must:
        key = getattr(cond, "key", None)
        match = getattr(cond, "match", None)
        expected = getattr(match, "value", None) if match else None
        if payload.get(key) != expected:
            return False
    return True


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _payload(text: str) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.0,
        "max_tokens": 64,
    }


def _response(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


@pytest.fixture
def vectors() -> dict[str, list[float]]:
    """Two near-paraphrase vectors (cosine ≈ 0.99), one distant one (≈ 0)."""
    return {
        "hello there": [1.0, 0.0, 0.0, 0.0],
        "hi there": [0.999, 0.045, 0.0, 0.0],  # ~0.999 cosine with above
        "what is the weather": [0.0, 1.0, 0.0, 0.0],  # ~0 cosine with above
    }


# --------------------------------------------------------------------------- #
# Semantic cache — happy path + threshold                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_semantic_paraphrase_hits_above_threshold(vectors) -> None:  # type: ignore[no-untyped-def]
    """The whole reason semantic cache exists: a paraphrase ('hi there')
    should hit a stored 'hello there' response above the threshold. This
    is the test that proves the L2 layer adds real value over L1."""
    cache = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(vectors),
        similarity_threshold=0.95,
    )
    await cache.ensure_ready()

    await cache.put(
        tenant_id="t1",
        model="anthropic/claude-opus-4-7",
        key_payload=_payload("hello there"),
        response=_response("greetings"),
    )

    # Different prompt, same intent — should hit.
    lookup = await cache.get(
        tenant_id="t1",
        model="anthropic/claude-opus-4-7",
        key_payload=_payload("hi there"),
    )
    assert lookup.hit is True
    assert lookup.tier == "semantic"
    assert lookup.response == _response("greetings")
    assert lookup.similarity is not None and lookup.similarity >= 0.95


@pytest.mark.asyncio
async def test_dissimilar_query_misses(vectors) -> None:  # type: ignore[no-untyped-def]
    """A semantically different question must NOT hit. Otherwise the
    cache would silently confuse "hi there" with "what is the weather"
    — the worst kind of cache bug: looks fine, returns nonsense."""
    cache = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(vectors),
        similarity_threshold=0.95,
    )
    await cache.ensure_ready()
    await cache.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("hello there"),
        response=_response("greetings"),
    )

    lookup = await cache.get(
        tenant_id="t1", model="m", key_payload=_payload("what is the weather")
    )
    assert lookup.hit is False


@pytest.mark.asyncio
async def test_threshold_strictness_changes_outcome(vectors) -> None:  # type: ignore[no-untyped-def]
    """The same pair of queries can hit OR miss depending on threshold.
    Confirms the threshold is wired through and that operators tuning
    it down get more hits, up get fewer. This is the FinOps lever."""
    near_dup_vectors = {
        "a": [1.0, 0.0, 0.0, 0.0],
        "b": [0.9, 0.43589, 0.0, 0.0],  # cos ≈ 0.9
    }

    # Strict — 0.95 threshold > 0.9 similarity → miss.
    strict = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(near_dup_vectors),
        similarity_threshold=0.95,
    )
    await strict.ensure_ready()
    await strict.put(tenant_id="t1", model="m", key_payload=_payload("a"), response=_response("A"))
    strict_lookup = await strict.get(tenant_id="t1", model="m", key_payload=_payload("b"))
    assert strict_lookup.hit is False

    # Permissive — 0.85 threshold < 0.9 similarity → hit.
    permissive = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(near_dup_vectors),
        similarity_threshold=0.85,
    )
    await permissive.ensure_ready()
    await permissive.put(
        tenant_id="t1", model="m", key_payload=_payload("a"), response=_response("A")
    )
    permissive_lookup = await permissive.get(
        tenant_id="t1", model="m", key_payload=_payload("b")
    )
    assert permissive_lookup.hit is True


# --------------------------------------------------------------------------- #
# Semantic cache — isolation                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_semantic_tenant_isolation(vectors) -> None:  # type: ignore[no-untyped-def]
    """Tenant A's L2 entries must not be retrievable by tenant B even
    when the paraphrase would otherwise score above threshold. This is
    the security invariant — payload filter MUST work."""
    cache = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(vectors),
        similarity_threshold=0.5,  # very permissive so the filter is the only gate
    )
    await cache.ensure_ready()
    await cache.put(
        tenant_id="tenant-a",
        model="m",
        key_payload=_payload("hello there"),
        response=_response("for a"),
    )

    # Same prompt, different tenant. Without the filter this would hit.
    cross = await cache.get(
        tenant_id="tenant-b",
        model="m",
        key_payload=_payload("hello there"),
    )
    assert cross.hit is False


@pytest.mark.asyncio
async def test_semantic_model_isolation(vectors) -> None:  # type: ignore[no-untyped-def]
    """Like tenant isolation but for model — same paraphrase under a
    different model is a miss. Prevents Opus-cached responses leaking
    into Haiku traffic."""
    cache = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(vectors),
        similarity_threshold=0.5,
    )
    await cache.ensure_ready()
    await cache.put(
        tenant_id="t1",
        model="anthropic/claude-opus-4-7",
        key_payload=_payload("hello there"),
        response=_response("opus answer"),
    )
    other = await cache.get(
        tenant_id="t1",
        model="anthropic/claude-haiku-4-5",
        key_payload=_payload("hi there"),
    )
    assert other.hit is False


# --------------------------------------------------------------------------- #
# LayeredCache — composition behaviour                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def layered_pair(vectors) -> AsyncIterator[tuple[LayeredCache, QdrantSemanticCache, RedisExactCache]]:  # type: ignore[no-untyped-def]
    """L1 (real fakeredis-backed exact) + L2 (real semantic against
    FakeQdrant) composed via LayeredCache. Returns the layered cache
    plus its inner layers so tests can poke at either tier directly."""
    l1 = RedisExactCache(fakeredis.aioredis.FakeRedis())
    l2 = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder(vectors),
        similarity_threshold=0.95,
    )
    await l2.ensure_ready()
    yield LayeredCache(l1=l1, l2=l2), l2, l1
    await l1.aclose()
    await l2.aclose()


@pytest.mark.asyncio
async def test_layered_l1_hit_short_circuits_l2(layered_pair) -> None:  # type: ignore[no-untyped-def]
    """An L1 hit must NOT trigger any L2 work — that's the whole point
    of a layered cache. Confirmed by writing to L1 directly, then
    looking up via the composer and checking the tier is exact."""
    layered, _l2, l1 = layered_pair
    await l1.put(
        tenant_id="t1", model="m", key_payload=_payload("hello there"), response=_response("L1")
    )
    lookup = await layered.get(tenant_id="t1", model="m", key_payload=_payload("hello there"))
    assert lookup.hit is True
    assert lookup.tier == "exact"
    assert lookup.response == _response("L1")


@pytest.mark.asyncio
async def test_layered_l1_miss_l2_hit_returns_semantic(layered_pair) -> None:  # type: ignore[no-untyped-def]
    """L1 miss + L2 hit must return the L2 result tagged ``semantic``.
    The promotion-into-L1 happens as a side effect and is tested next."""
    layered, l2, _l1 = layered_pair
    # Seed L2 only.
    await l2.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("hello there"),
        response=_response("L2"),
    )

    lookup = await layered.get(
        tenant_id="t1", model="m", key_payload=_payload("hi there")  # paraphrase
    )
    assert lookup.hit is True
    assert lookup.tier == "semantic"
    assert lookup.response == _response("L2")


@pytest.mark.asyncio
async def test_layered_l2_hit_promotes_into_l1(layered_pair) -> None:  # type: ignore[no-untyped-def]
    """After an L2 hit, the SAME paraphrase the second time should hit
    L1 (faster path). That's the promotion contract — locality compounds."""
    layered, l2, l1 = layered_pair
    await l2.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("hello there"),
        response=_response("L2"),
    )

    # First lookup with the paraphrase: L2 hit + promote.
    first = await layered.get(
        tenant_id="t1", model="m", key_payload=_payload("hi there")
    )
    assert first.tier == "semantic"

    # Now an exact lookup on the SAME paraphrase should hit L1 directly.
    second = await l1.get(tenant_id="t1", model="m", key_payload=_payload("hi there"))
    assert second.hit is True
    assert second.tier == "exact"


@pytest.mark.asyncio
async def test_layered_put_writes_to_both_tiers(layered_pair) -> None:  # type: ignore[no-untyped-def]
    """A composer put must populate L1 AND L2 in parallel — otherwise
    future identical requests would miss L1 and future paraphrases
    would miss L2."""
    layered, l2, l1 = layered_pair
    await layered.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("hello there"),
        response=_response("both"),
    )

    # L1 read.
    l1_lookup = await l1.get(
        tenant_id="t1", model="m", key_payload=_payload("hello there")
    )
    assert l1_lookup.hit is True

    # L2 read via paraphrase — confirms the put reached L2.
    l2_lookup = await l2.get(
        tenant_id="t1", model="m", key_payload=_payload("hi there")
    )
    assert l2_lookup.hit is True


@pytest.mark.asyncio
async def test_layered_both_miss_returns_miss(layered_pair) -> None:  # type: ignore[no-untyped-def]
    """Nothing seeded → composer returns miss. Trivial but defends
    against a bug where the composer returns ``hit=True`` with a None
    response, which would crash the caller."""
    layered, _l2, _l1 = layered_pair
    lookup = await layered.get(
        tenant_id="t1", model="m", key_payload=_payload("anything")
    )
    assert lookup.hit is False
    assert lookup.response is None


@pytest.mark.asyncio
async def test_layered_fails_open_when_l2_raises() -> None:
    """L2 backend dying must NOT prevent L1 from serving. Cache layering
    only adds value if it's strictly more reliable than the L1 alone."""

    class ExplodingCache:
        async def get(self, **_: Any) -> Any:
            raise RuntimeError("L2 exploded")

        async def put(self, **_: Any) -> None:
            raise RuntimeError("L2 exploded")

        async def aclose(self) -> None:
            return None

    l1 = RedisExactCache(fakeredis.aioredis.FakeRedis())
    layered = LayeredCache(l1=l1, l2=ExplodingCache())

    # Seed L1 directly so the L1 path is what answers.
    await l1.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("ping"),
        response=_response("L1 only"),
    )
    lookup = await layered.get(
        tenant_id="t1", model="m", key_payload=_payload("ping")
    )
    assert lookup.hit is True
    assert lookup.tier == "exact"

    # Put: should write L1 even if L2 raises.
    await layered.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("pong"),
        response=_response("through L1"),
    )
    pong = await l1.get(tenant_id="t1", model="m", key_payload=_payload("pong"))
    assert pong.hit is True


# --------------------------------------------------------------------------- #
# Misc                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_semantic_extracts_latest_user_message_only(vectors) -> None:  # type: ignore[no-untyped-def]
    """``_extract_query_text`` must use the LAST user turn, not the
    whole conversation. Otherwise a same-final-question chat under a
    different context would falsely hit."""
    from pronaos.cache.semantic import _extract_query_text

    payload = {
        "messages": [
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "second turn (LATEST)"},
        ]
    }
    assert _extract_query_text(payload) == "second turn (LATEST)"


@pytest.mark.asyncio
async def test_semantic_returns_miss_for_empty_query() -> None:
    """A payload with no user message is a miss without consulting
    Qdrant — defends against a malformed request shape."""
    cache = QdrantSemanticCache(
        client=FakeQdrantClient(),
        embedder=StubEmbedder({}),
    )
    await cache.ensure_ready()
    lookup = await cache.get(
        tenant_id="t1", model="m", key_payload={"messages": []}
    )
    assert lookup.hit is False


@pytest.mark.asyncio
async def test_null_l2_makes_layered_behave_like_l1_only() -> None:
    """A LayeredCache with a NullCache L2 must behave exactly like
    L1 alone — useful pattern when semantic is disabled but the
    composition path stays the same."""
    l1 = RedisExactCache(fakeredis.aioredis.FakeRedis())
    layered = LayeredCache(l1=l1, l2=NullCache())

    await layered.put(
        tenant_id="t1",
        model="m",
        key_payload=_payload("hello"),
        response=_response("only L1"),
    )
    lookup = await layered.get(
        tenant_id="t1", model="m", key_payload=_payload("hello")
    )
    assert lookup.hit is True
    assert lookup.tier == "exact"
