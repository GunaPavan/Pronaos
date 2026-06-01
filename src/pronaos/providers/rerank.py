"""Rerank provider interface + adapters.

Phase 32 ships ``POST /v1/rerank``. Reranking is the third stage of a
classic RAG pipeline:

    embed → vector-search top-K → **rerank top-K by relevance** → feed to LLM

Where vector similarity gets you roughly-relevant candidates, a
dedicated cross-encoder rerank model produces *highly* relevant
ordering by scoring each (query, document) pair jointly. Cohere and
Voyage are the dominant rerank-as-a-service providers; their wire
shapes differ:

- **Cohere** (``POST /v2/rerank``):
  ``{"model": ..., "query": "...", "documents": [...], "top_n": N,
      "return_documents": true}``
  Billing: per "search unit" — one rerank call up to 100 documents
  counts as one unit regardless of document length.

- **Voyage** (``POST /v1/rerank``):
  ``{"model": ..., "query": "...", "documents": [...], "top_k": N,
      "return_documents": true, "truncation": true}``
  Billing: per token — sum of query + all documents.

We expose a Cohere-like public shape (``top_n``) and adapters translate
to the upstream's preferred field name. Both adapters share the
existing error classes (``ProviderError`` / ``RateLimitError`` /
``AuthError`` / ``UpstreamTimeoutError``).

Why a separate file, not bolted onto ``EmbeddingProvider``: rerank's
input shape (query + documents) and output shape (scored index list)
are fundamentally different from embeddings (text → vector). A
sibling abstract base class avoids cluttering either with a no-op
method on every adapter.

Cost accounting
---------------
Two billing models:

- **Per-call** (Cohere): one fixed cost per rerank call (up to 100
  docs). Pricing pseudo-field: ``input_hcents_per_mtok`` in catalog
  is reused with a sentinel — see :func:`CohereRerankProvider.cost_hcents`.
- **Per-token** (Voyage): cost = total_tokens * hcents/Mtok.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
class RerankProviderRequest:
    """One rerank request, normalised across provider shapes.

    ``top_n`` is the public spelling (matches Cohere); adapters
    translate to Voyage's ``top_k`` internally. When ``None`` the
    upstream returns every document with its score (which is sometimes
    what you want — e.g. when you're ranking a small candidate set).

    ``return_documents`` echoes the original document text in the
    response. False saves bandwidth when the client already has the
    documents and only needs the relevance scores.
    """

    model: str
    query: str
    documents: list[str]
    top_n: int | None = None
    return_documents: bool = True


@dataclass(frozen=True, slots=True)
class RerankResultItem:
    """One reranked document with its relevance score.

    ``index`` is the position of the document in the *original*
    request list (so the client can map back to whatever metadata
    they tracked alongside). ``relevance_score`` is the
    cross-encoder's confidence, typically in [0, 1] but provider-
    specific (Cohere normalises to [0, 1]; Voyage returns the raw
    logit-like score).
    """

    index: int
    relevance_score: float
    document: str | None = None


@dataclass(frozen=True, slots=True)
class RerankProviderResult:
    """One rerank result, normalised across provider shapes.

    ``results`` is sorted descending by ``relevance_score`` (the
    upstream returns them that way; adapters trust it). When
    ``top_n`` was set, only the top-N items are returned.

    ``prompt_tokens`` is what the provider reports in usage. For
    Cohere this is computed from ``search_units`` (a search unit
    represents one rerank call); for Voyage it's the actual token
    sum.
    """

    results: list[RerankResultItem]
    prompt_tokens: int
    model: str
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Abstract base                                                               #
# --------------------------------------------------------------------------- #


class RerankProvider(ABC):
    """Abstract upstream rerank provider.

    Mirrors :class:`pronaos.providers.embeddings.EmbeddingProvider`.
    One method + cost accounting + lifecycle.
    """

    name: str

    @abstractmethod
    async def rerank(self, req: RerankProviderRequest) -> RerankProviderResult:
        """Score all documents against the query and return ordered results."""
        ...

    @abstractmethod
    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        """Return cost in hundredths of a cent. Pricing model differs per
        provider — Cohere is per-call (ignore ``prompt_tokens``), Voyage
        is per-token."""
        ...

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _strip_prefix(model: str, provider_key: str) -> str:
    prefix = f"{provider_key}/"
    return model.removeprefix(prefix)


def _raise_for_status(resp: httpx.Response, *, provider_key: str) -> None:
    """Shared error classification across all HTTP rerank adapters.

    Same shape as the embedding adapter's helper — kept duplicated to
    avoid coupling the two modules. If we add a third HTTP-backed
    sibling (e.g. /v1/audio/transcriptions in a future phase), this
    becomes a refactor candidate.
    """
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
# Cohere rerank                                                               #
# --------------------------------------------------------------------------- #


class CohereRerankProvider(RerankProvider):
    """Cohere's ``POST /v2/rerank`` endpoint.

    Cohere's rerank API uses ``top_n`` (not ``top_k``) and reports
    billing in ``search_units`` rather than tokens. One search unit
    represents one rerank call processing up to 100 documents.

    Pricing model: per-call. Our catalog stores the per-call hcents
    in ``input_hcents_per_mtok`` (the field is misnamed for rerank
    but we reuse the Pricing dataclass for catalog uniformity — the
    rerank cost helper ignores ``prompt_tokens`` and returns the
    flat per-call value).
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

    async def rerank(self, req: RerankProviderRequest) -> RerankProviderResult:
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, "cohere"),
            "query": req.query,
            "documents": req.documents,
            "return_documents": req.return_documents,
        }
        if req.top_n is not None:
            body["top_n"] = req.top_n
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/v2/rerank"

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

        _raise_for_status(resp, provider_key="cohere")
        data = resp.json()

        # v2 response:
        # { "id": "...",
        #   "results": [{"index": 2, "relevance_score": 0.99, "document": {"text": "..."}}, ...],
        #   "meta": {"billed_units": {"search_units": 1}} }
        results_list = data.get("results") or []
        items: list[RerankResultItem] = []
        for entry in results_list:
            if not isinstance(entry, dict):
                continue
            doc_field = entry.get("document")
            # Cohere returns document as {"text": "..."} when return_documents=true;
            # we surface the plain string downstream.
            doc_text: str | None = None
            if isinstance(doc_field, dict):
                doc_text = doc_field.get("text") if isinstance(doc_field.get("text"), str) else None
            elif isinstance(doc_field, str):
                doc_text = doc_field
            items.append(
                RerankResultItem(
                    index=int(entry.get("index", 0)),
                    relevance_score=float(entry.get("relevance_score", 0.0)),
                    document=doc_text,
                )
            )

        # Token reporting: Cohere reports search_units (one per call,
        # not per document). We surface that as a synthetic
        # "prompt_tokens" so usage_records carries SOMETHING. The cost
        # helper ignores this and uses the per-call price.
        meta = data.get("meta") or {}
        billed = meta.get("billed_units") if isinstance(meta, dict) else {}
        search_units = int(billed.get("search_units") or 0) if isinstance(billed, dict) else 0
        return RerankProviderResult(
            results=items,
            prompt_tokens=search_units,
            model=req.model,
            raw=data if isinstance(data, dict) else None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        """Per-call pricing. The catalog stores the per-call hcents
        in ``Pricing.input_hcents_per_mtok`` — for rerank that field
        means "per-call" not "per-Mtok". Misnaming the field is the
        least-bad choice over adding a third Pricing variant just for
        rerank.

        ``prompt_tokens`` here is the search_units count from the
        Cohere response, which is always 1 for one rerank call (up
        to 100 docs). We multiply by that count for safety even
        though it's effectively a passthrough.
        """
        bare_model = _strip_prefix(model, "cohere")
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        # search_units * per-call-price.
        return max(1, prompt_tokens) * price.input_hcents_per_mtok


# --------------------------------------------------------------------------- #
# Voyage rerank                                                               #
# --------------------------------------------------------------------------- #


class VoyageRerankProvider(RerankProvider):
    """Voyage's ``POST /v1/rerank`` endpoint.

    Voyage uses ``top_k`` (not ``top_n``) and reports billing in
    ``total_tokens``. We translate ``top_n`` to ``top_k`` on the way
    out. Per-token pricing follows the same hcents/Mtok model as
    embeddings.
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

    async def rerank(self, req: RerankProviderRequest) -> RerankProviderResult:
        body: dict[str, Any] = {
            "model": _strip_prefix(req.model, "voyage"),
            "query": req.query,
            "documents": req.documents,
            "return_documents": req.return_documents,
        }
        if req.top_n is not None:
            body["top_k"] = req.top_n  # voyage spelling
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/rerank"

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

        _raise_for_status(resp, provider_key="voyage")
        data = resp.json()

        # Voyage response:
        # { "object": "list",
        #   "data": [{"index": 2, "relevance_score": 0.95, "document": "..."}, ...],
        #   "model": "rerank-2", "usage": {"total_tokens": 38} }
        data_list = data.get("data") or []
        items: list[RerankResultItem] = []
        for entry in data_list:
            if not isinstance(entry, dict):
                continue
            doc_field = entry.get("document")
            doc_text = doc_field if isinstance(doc_field, str) else None
            items.append(
                RerankResultItem(
                    index=int(entry.get("index", 0)),
                    relevance_score=float(entry.get("relevance_score", 0.0)),
                    document=doc_text,
                )
            )

        usage = data.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        return RerankProviderResult(
            results=items,
            prompt_tokens=total_tokens,
            model=str(data.get("model") or req.model),
            raw=data if isinstance(data, dict) else None,
        )

    def cost_hcents(self, prompt_tokens: int, model: str) -> int:
        """Per-token pricing. Same hcents/Mtok model as embeddings."""
        bare_model = _strip_prefix(model, "voyage")
        price = self._pricing.get(bare_model)
        if price is None:
            return 0
        return prompt_tokens * price.input_hcents_per_mtok // 1_000_000
