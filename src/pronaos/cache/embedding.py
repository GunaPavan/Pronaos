"""Embedding providers for L2 semantic cache.

The protocol is async + batched so a future API-backed provider (OpenAI,
Cohere, Voyage) drops in identically to the default local model.

Why the local default
---------------------
``sentence-transformers/all-MiniLM-L6-v2`` is the standard "good enough
for production cache lookups" small model:
- 384-dimensional vectors (Qdrant friendly, fast cosine compare)
- ~25 MB ONNX, ~90 MB PyTorch weights
- ~10 ms per short query on a single CPU core
- Trained on 1B+ sentence pairs — covers paraphrase recognition

Self-hosted gateways don't want recurring embedding API spend, so this is
the right default. A future ``OpenAIEmbedder`` will satisfy the same
protocol when teams prefer the recurring-cost / better-recall trade.

Lazy import
-----------
The sentence-transformers import (which pulls in PyTorch) lives inside
``__init__`` rather than at module top. Importing this module is cheap.
Constructing the provider is what costs the ~1 s PyTorch boot — and only
the path that actually wants embeddings should pay it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from pronaos.logging import get_logger

log = get_logger(__name__)

# The community standard "small + fast" model. 384-dim, ~90 MB.
DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingProvider(Protocol):
    """Async embedding provider.

    Implementations should batch internally when the provider supports it
    (sentence-transformers does; HTTP APIs typically have per-call cost).
    """

    @property
    def dimension(self) -> int:
        """Vector dimension — Qdrant needs this at collection creation."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input string. Same length, same order."""
        ...

    async def aclose(self) -> None:
        """Release the model / connection pool."""
        ...


class SentenceTransformerEmbedder(EmbeddingProvider):
    """Local embedder backed by sentence-transformers.

    sentence-transformers is synchronous — we wrap each ``encode`` call
    in ``asyncio.to_thread`` so the gateway's event loop isn't blocked
    by the (~10 ms / call) CPU work. That's the right call for moderate
    QPS; if a deployment needs higher throughput, the right answer is
    to move embeddings to a dedicated service, not to thread-pool around
    the model.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME) -> None:
        # Lazy import so importing this module doesn't trigger PyTorch
        # initialisation for callers that never touch semantic cache.
        from sentence_transformers import SentenceTransformer

        log.info("embedding.loading_model", model=model_name)
        self._model: Any = SentenceTransformer(model_name)
        # ``get_sentence_embedding_dimension`` returns the model's output
        # dim — pinning at construction means a misconfigured Qdrant
        # collection fails fast at startup rather than mid-request.
        self._dim: int = self._model.get_sentence_embedding_dimension()
        log.info("embedding.loaded", model=model_name, dimension=self._dim)

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        # ``normalize_embeddings=True`` returns unit vectors. Cosine
        # similarity between unit vectors reduces to a dot product, which
        # Qdrant computes faster than full cosine — and the math is
        # identical. Free speedup, zero behaviour change.
        def _encode() -> list[list[float]]:
            arr = self._model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False
            )
            # numpy ndarray.tolist() is typed as Any in the numpy stubs;
            # cast back to the concrete shape we contractually return.
            from typing import cast

            return cast(list[list[float]], arr.tolist())

        return await asyncio.to_thread(_encode)

    async def aclose(self) -> None:
        # SentenceTransformer doesn't expose an explicit close; relying on
        # GC is fine. Method exists to satisfy the Protocol.
        return None
