"""Embedding provider interface + adapters.

Phase 31 ships the public ``/v1/embeddings`` endpoint. Unlike chat
completions — where every modern upstream speaks the same OpenAI
shape — embedding APIs diverged. Four shapes exist in the wild:

- **OpenAI** (and openrouter/together for embedding pass-through):
  ``{"model": ..., "input": "..." | [...], "dimensions": int?}``
- **Cohere**: ``{"model": ..., "texts": [...], "input_type": "..."}``
  — single string is NOT accepted; you must wrap in a list.
- **Voyage**: ``{"model": ..., "input": "..." | [...], "input_type": "..."}``
  — accepts both shapes, the ``input_type`` hint changes the embedding
  produced (the same text embedded with ``input_type="query"`` vs
  ``"document"`` returns DIFFERENT vectors).
- **Mistral**: same as OpenAI (lucky for us).
- **Local** (sentence-transformers): no HTTP, no auth, no usage block.

This module:

- Defines a canonical ``EmbeddingProviderRequest`` / ``EmbeddingProviderResult``
  that all four adapters convert to/from.
- Provides four concrete adapters.
- All adapters share the existing :mod:`pronaos.providers.base` error
  types (``ProviderError``, ``RateLimitError``, ``AuthError``,
  ``UpstreamTimeoutError``) — same retry classification as chat.

Why a separate file, not a method on ``Provider``: embedding is a
different request shape (text → vector list, no streaming, no tools,
no completion tokens). Bolting it onto the chat ``Provider`` abstract
would force every existing chat-only adapter to either implement a
no-op embed or raise NotImplemented. A sibling base class is cleaner.

Cost accounting
---------------
Embeddings are billed per million **input** tokens. Output cost is zero
(the response is a vector, not generated text). Catalog pricing for
embedding models populates ``input_hcents_per_mtok`` only; the
``cost_hcents`` helper reads it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from pronaos.providers.base import (
    AuthError,
    ProviderError,
    RateLimitError,
    UpstreamTimeoutError,
)
from pronaos.providers.openai_compat import Pricing

# --------------------------------------------------------------------------- #
# Canonical request / result                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmbeddingProviderRequest:
    """One embedding request, normalised across provider shapes.

    ``input_texts`` is always a list — the API handler wraps a single
    string before calling into the adapter. Adapters serialize back to
    the upstream's preferred shape (single string vs list) as needed.

    ``dimensions`` is the OpenAI v3.x extension that lets clients ask
    for a reduced-dimensionality vector. Adapters whose backend doesn't
    support it pass it as a no-op (or raise if dimensions is set and
    the model can't honour it).

    ``input_type`` is the Cohere / Voyage hint (``"query"``,
    ``"document"``, ``"classification"``, ...). OpenAI ignores it.
    Default ``None`` = adapter chooses provider-appropriate default.
    """

    model: str
    input_texts: list[str]
    dimensions: int | None = None
    input_type: str | None = None
    encoding_format: str = "float"  # "float" or "base64"


@dataclass(frozen=True, slots=True)
class EmbeddingProviderResult:
    """One embedding result, normalised across provider shapes.

    ``vectors`` parallels the request's ``input_texts`` (same length,
    same order). ``prompt_tokens`` is what the provider counts (when
    available); ``0`` if the provider doesn't report it (local).

    ``raw`` carries the unparsed upstream JSON for adapters that want
    to surface extra metadata in the public response (e.g. OpenAI's
    ``object``/``id`` fields). The API handler does NOT echo ``raw``
    to the client — it builds the OpenAI-shape response from
    ``vectors`` + the request's metadata so the response shape is
    provider-independent.
    """

    vectors: list[list[float]]
    prompt_tokens: int
    model: str
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Abstract base                                                               #
# --------------------------------------------------------------------------- #


class EmbeddingProvider(ABC):
    """Abstract upstream embedding provider.

    Mirrors the shape of :class:`pronaos.providers.base.Provider` but
    for embeddings. One method (no streaming, no async-iterator
    machinery) + cost accounting + lifecycle.
    """

    name: str

    @abstractmethod
    async def embed(self, req: EmbeddingProviderRequest) -> EmbeddingProviderResult:
        """Compute embeddings for the request and return all vectors."""
        ...

    @abstractmethod
    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        """Return cost in hundredths of a cent; ``0`` for unknown / free models."""
        ...

    async def aclose(self) -> None:
        """Release provider-owned resources. Default no-op."""
        return None


# --------------------------------------------------------------------------- #
# Helper: strip provider prefix                                               #
# --------------------------------------------------------------------------- #


def _strip_prefix(model: str, provider_key: str) -> str:
    prefix = f"{provider_key}/"
    return model.removeprefix(prefix)


def _common_raise_for_status(resp: httpx.Response, *, provider_key: str) -> None:
    """Shared error classification across all HTTP embedding adapters."""
    if resp.status_code < 400:
        return
    try:
        payload = resp.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type") or str(err)
        else:
            detail = str(err) if err is not None else (resp.text[:200] if resp.text else "")
    except Exception:
        detail = resp.text[:200] if resp.text else ""

    status = resp.status_code
    tag = provider_key
    if status in (401, 403):
        raise AuthError(f"{tag}: auth failed: {detail}")
    if status == 429:
        raise RateLimitError(f"{tag}: rate limited: {detail}")
    if status >= 500:
        raise ProviderError(
            f"{tag}: upstream {status}: {detail}",
            status=502,
            retryable=True,
        )
    raise ProviderError(
        f"{tag}: {status}: {detail}",
        status=400,
        retryable=False,
    )


# --------------------------------------------------------------------------- #
# OpenAI-compatible adapter (OpenAI, OpenRouter, Together, Mistral)           #
# --------------------------------------------------------------------------- #


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """One adapter for every OpenAI-compatible embedding endpoint.

    Covers ``api.openai.com``, ``openrouter.ai``, ``api.together.xyz``,
    ``api.mistral.ai`` — all four speak the same shape. Differences are
    in pricing (the catalog handles that) and supported models.
    """

    def __init__(
        self,
        *,
        provider_key: str,
        base_url: str,
        api_key: str,
        pricing: dict[str, Pricing],
        default_headers: dict[str, str] | None = None,
        auth_header_name: str = "Authorization",
        auth_header_format: str = "Bearer {key}",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise AuthError(f"{provider_key}: missing api key")
        self._provider_key = provider_key
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._pricing = pricing
        self._default_headers = dict(default_headers or {})
        self._auth_header_name = auth_header_name
        self._auth_header_format = auth_header_format
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._provider_key

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def embed(self, req: EmbeddingProviderRequest) -> EmbeddingProviderResult:
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, self._provider_key),
            "input": req.input_texts,
            "encoding_format": req.encoding_format,
        }
        if req.dimensions is not None:
            body["dimensions"] = req.dimensions
        headers = {
            "content-type": "application/json",
            **self._default_headers,
            self._auth_header_name: self._auth_header_format.format(key=self._api_key),
        }
        url = f"{self._base_url}/embeddings"

        try:
            resp = await self._http.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"{self._provider_key}: upstream timeout") from e
        except httpx.RequestError as e:
            raise ProviderError(
                f"{self._provider_key}: network error: {e!s}",
                status=502,
                retryable=True,
            ) from e

        _common_raise_for_status(resp, provider_key=self._provider_key)
        data = resp.json()

        # OpenAI response shape:
        # { "object":"list", "data":[{"object":"embedding","embedding":[...], "index":0}, ...],
        #   "model":"text-embedding-3-small",
        #   "usage":{"prompt_tokens":N,"total_tokens":N} }
        data_list = data.get("data") or []
        # data may not be sorted by index — sort defensively.
        sorted_entries = sorted(
            data_list,
            key=lambda e: int(e.get("index", 0)) if isinstance(e, dict) else 0,
        )
        vectors = [
            list(entry.get("embedding") or [])
            for entry in sorted_entries
            if isinstance(entry, dict)
        ]
        usage = data.get("usage") or {}
        return EmbeddingProviderResult(
            vectors=vectors,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            model=str(data.get("model") or req.model),
            raw=data if isinstance(data, dict) else None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        bare_model = _strip_prefix(model, self._provider_key)
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        return prompt_tokens * price.input_hcents_per_mtok // 1_000_000


# --------------------------------------------------------------------------- #
# Cohere adapter                                                              #
# --------------------------------------------------------------------------- #


class CohereEmbeddingProvider(EmbeddingProvider):
    """Cohere's ``/v2/embed`` endpoint.

    Cohere uses ``texts`` (not ``input``), requires ``input_type``, and
    returns vectors under ``embeddings.float`` (or other format keys
    depending on ``embedding_types``).
    """

    def __init__(
        self,
        *,
        api_key: str,
        pricing: dict[str, Pricing],
        base_url: str = "https://api.cohere.com",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise AuthError("cohere: missing api key")
        self._api_key = api_key
        self._pricing = pricing
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    name = "cohere"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def embed(self, req: EmbeddingProviderRequest) -> EmbeddingProviderResult:
        # Cohere's API expects ``input_type`` to be set on every call.
        # Default to "search_document" — the most common embedding use
        # case (indexing a corpus). Callers wanting query-time embeddings
        # set ``input_type="search_query"``.
        input_type = req.input_type or "search_document"
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, "cohere"),
            "texts": req.input_texts,
            "input_type": input_type,
            # ``embedding_types`` lets us pin float; Cohere otherwise
            # returns a different shape with ``int8``/``binary``.
            "embedding_types": ["float"],
        }
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/v2/embed"

        try:
            resp = await self._http.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("cohere: upstream timeout") from e
        except httpx.RequestError as e:
            raise ProviderError(
                f"cohere: network error: {e!s}",
                status=502,
                retryable=True,
            ) from e

        _common_raise_for_status(resp, provider_key="cohere")
        data = resp.json()

        # v2 response: { "embeddings": {"float": [[...], [...]]},
        #                "meta": {"billed_units": {"input_tokens": N}} }
        embeddings_block = data.get("embeddings") or {}
        vectors: list[list[float]]
        if isinstance(embeddings_block, dict):
            vectors = [list(v) for v in (embeddings_block.get("float") or [])]
        else:
            # Older v1-style fallback: flat list
            vectors = [list(v) for v in embeddings_block]
        meta = data.get("meta") or {}
        billed = meta.get("billed_units") if isinstance(meta, dict) else {}
        prompt_tokens = int(billed.get("input_tokens") or 0) if isinstance(billed, dict) else 0
        return EmbeddingProviderResult(
            vectors=vectors,
            prompt_tokens=prompt_tokens,
            model=req.model,
            raw=data if isinstance(data, dict) else None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        bare_model = _strip_prefix(model, "cohere")
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        return prompt_tokens * price.input_hcents_per_mtok // 1_000_000


# --------------------------------------------------------------------------- #
# Voyage adapter                                                              #
# --------------------------------------------------------------------------- #


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI's embedding endpoint.

    Voyage accepts both single-string and list ``input``, and the
    ``input_type`` hint changes the produced vector (``"query"`` vs
    ``"document"`` — same text yields DIFFERENT vectors). Callers who
    care about retrieval quality must thread this through.
    """

    def __init__(
        self,
        *,
        api_key: str,
        pricing: dict[str, Pricing],
        base_url: str = "https://api.voyageai.com/v1",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise AuthError("voyage: missing api key")
        self._api_key = api_key
        self._pricing = pricing
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    name = "voyage"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def embed(self, req: EmbeddingProviderRequest) -> EmbeddingProviderResult:
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, "voyage"),
            "input": req.input_texts,
        }
        # Voyage's input_type is optional. Pass through when the client
        # supplied one; otherwise let Voyage use its untyped default.
        if req.input_type is not None:
            body["input_type"] = req.input_type
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/embeddings"

        try:
            resp = await self._http.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError("voyage: upstream timeout") from e
        except httpx.RequestError as e:
            raise ProviderError(
                f"voyage: network error: {e!s}",
                status=502,
                retryable=True,
            ) from e

        _common_raise_for_status(resp, provider_key="voyage")
        data = resp.json()

        # Voyage response:
        # { "object":"list",
        #   "data":[{"object":"embedding","embedding":[...], "index":0}, ...],
        #   "model":"voyage-3", "usage":{"total_tokens":N} }
        data_list = data.get("data") or []
        sorted_entries = sorted(
            data_list,
            key=lambda e: int(e.get("index", 0)) if isinstance(e, dict) else 0,
        )
        vectors = [
            list(entry.get("embedding") or [])
            for entry in sorted_entries
            if isinstance(entry, dict)
        ]
        usage = data.get("usage") or {}
        # Voyage reports total_tokens (no prompt/completion split — embeddings
        # have no completion). Treat that as the prompt-token count.
        prompt_tokens = int(usage.get("total_tokens") or 0)
        return EmbeddingProviderResult(
            vectors=vectors,
            prompt_tokens=prompt_tokens,
            model=str(data.get("model") or req.model),
            raw=data if isinstance(data, dict) else None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        bare_model = _strip_prefix(model, "voyage")
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        return prompt_tokens * price.input_hcents_per_mtok // 1_000_000


# --------------------------------------------------------------------------- #
# Local adapter (sentence-transformers, no HTTP, no auth, no cost)            #
# --------------------------------------------------------------------------- #


class LocalSentenceTransformerEmbeddingProvider(EmbeddingProvider):
    """Local embedder via sentence-transformers, exposed as a provider.

    Reuses the same SentenceTransformerEmbedder the L2 semantic cache
    already loads — so when the gateway runs with semantic cache
    enabled, the local embeddings endpoint costs zero extra RAM.

    Token accounting: there's no real "tokens" for a local model in
    the billing sense (it's local CPU work). We report a heuristic
    ``len(text.split())`` per text so usage_records still gets a
    non-zero number for FinOps queries. Cost is always 0.
    """

    name = "local-st"

    def __init__(self, model_name: str | None = None) -> None:
        # Lazy import so this module doesn't pull PyTorch unless the
        # local provider is actually constructed.
        from pronaos.cache.embedding import (
            DEFAULT_MODEL_NAME,
            SentenceTransformerEmbedder,
        )

        self._embedder = SentenceTransformerEmbedder(model_name or DEFAULT_MODEL_NAME)

    async def aclose(self) -> None:
        await self._embedder.aclose()

    async def embed(self, req: EmbeddingProviderRequest) -> EmbeddingProviderResult:
        vectors = await self._embedder.embed(req.input_texts)
        # Heuristic token count — for the local model there's no
        # tokenizer-derived billing number; we report whitespace-split
        # token-equivalents so the usage_record row carries something
        # meaningful for "how much text did this team embed."
        approx_tokens = sum(len(t.split()) for t in req.input_texts)
        return EmbeddingProviderResult(
            vectors=[list(v) for v in vectors],
            prompt_tokens=approx_tokens,
            model=req.model,
            raw=None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        # Local model: own hardware, no per-token cost.
        return 0


# --------------------------------------------------------------------------- #
# Convenience: normalize a raw OpenAI-shape ``input`` field                   #
# --------------------------------------------------------------------------- #


def normalize_input_texts(raw: str | Sequence[str]) -> list[str]:
    """Normalise the public ``input`` field (str | list[str]) to a list.

    Used by the API handler so the rest of the pipeline always sees a
    list — even if the client sent a single string.
    """
    if isinstance(raw, str):
        return [raw]
    return list(raw)
