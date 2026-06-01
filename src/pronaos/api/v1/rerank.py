"""POST /v1/rerank endpoint (Phase 32).

Reranking is the third stage of a classic RAG pipeline:

    embed → vector-search top-K → **rerank top-K by relevance** → LLM

Where vector similarity gives roughly-relevant candidates, a
cross-encoder rerank model jointly scores each (query, document) pair
and produces highly-relevant ordering. Cohere and Voyage are the
dominant rerank providers; their wire shapes differ (top_n vs top_k,
search_units vs total_tokens billing). Pronaos hides the difference
behind one canonical endpoint shape.

Public request shape (Cohere-like — the dominant convention):

    POST /v1/rerank
    {
      "model": "cohere/rerank-english-v3.0" | "voyage/rerank-2",
      "query": "What is the capital of the United States?",
      "documents": ["Washington, D.C. is...", "Tokyo is...", ...],
      "top_n": 3,               # optional, returns all if absent
      "return_documents": true  # default true; false saves bandwidth
    }

Response:

    {
      "object": "list",
      "data": [
        {"index": 0, "relevance_score": 0.99, "document": "Washington…"},
        {"index": 5, "relevance_score": 0.42, "document": "..."}
      ],
      "model": "cohere/rerank-english-v3.0",
      "usage": {"prompt_tokens": N, "total_tokens": N}
    }

Pipeline reuse — same as embeddings and chat:
- Auth (API key or OIDC), per-team allowlist gate.
- Preflight token estimator on (query + every document).
- Ingress guardrails (PII redaction on query + each document).
- L1 exact cache (deterministic per query+documents+top_n+return_documents).
- Audit row, usage record (token + cost), Prometheus counters.

The cache angle is the killer feature: rerank scores are deterministic
per (model, query, document set), so the same RAG search re-issued
returns byte-identical scores from cache at zero upstream cost. Cohere
bills per "search unit" = per call; Voyage bills per token. Both go
to $0 on cache hit.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the dependency providers wired in chat.py — same app.state slots.
from pronaos.api.v1.chat import (
    get_audit_logger,
    get_cache,
    get_guardrails,
    get_registry,
)
from pronaos.audit.logger import AuditLogger
from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db, get_quota_tracker
from pronaos.cache.base import Cache
from pronaos.core.model_access import is_model_allowed
from pronaos.core.quota import CompletedCall, QuotaTracker
from pronaos.core.singleflight import SingleflightRegistry
from pronaos.guardrails.base import GuardrailEngine
from pronaos.guardrails.policy import resolve_policy
from pronaos.logging import get_logger
from pronaos.observability.metrics import (
    record_cache_lookup,
    record_guardrail_hit,
    record_preflight_denial,
    record_rerank_cache_hit,
    record_rerank_error,
    record_rerank_success,
    record_singleflight_follower,
)
from pronaos.providers.base import AuthError, ProviderError
from pronaos.providers.catalog import CATALOG
from pronaos.providers.registry import (
    ProviderNotConfiguredError,
    ProviderRegistry,
    UnknownProviderError,
)
from pronaos.providers.rerank import RerankProviderRequest

log = get_logger(__name__)
router = APIRouter(tags=["rerank"])


# --------------------------------------------------------------------------- #
# Request / response models                                                   #
# --------------------------------------------------------------------------- #


class RerankRequest(BaseModel):
    """Public rerank request shape.

    We expose Cohere's spelling (``top_n``) since it's the dominant
    convention in the rerank ecosystem; the Voyage adapter
    translates to ``top_k`` internally.

    ``return_documents`` defaults to True so clients see the document
    text alongside scores by default. Set to False when the client
    already has the documents indexed locally and only needs the
    ordering — saves response bandwidth on large document sets.
    """

    model: str
    query: str
    documents: list[str] = Field(..., min_length=1, max_length=1000)
    top_n: int | None = Field(default=None, ge=1, le=1000)
    return_documents: bool = True


class RerankResultItem(BaseModel):
    """One reranked document with its relevance score."""

    index: int
    relevance_score: float
    document: str | None = None


class RerankUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class RerankResponse(BaseModel):
    object: str = "list"
    data: list[RerankResultItem]
    model: str
    usage: RerankUsage


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/rerank", response_model=RerankResponse)
async def rerank(
    request: Request,
    body: RerankRequest,
    response: Response,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    quota: Annotated[QuotaTracker, Depends(get_quota_tracker)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cache: Annotated[Cache, Depends(get_cache)],
    guardrails: Annotated[GuardrailEngine, Depends(get_guardrails)],
    audit: Annotated[AuditLogger, Depends(get_audit_logger)],
) -> Any:
    """Score and order ``body.documents`` by relevance to ``body.query``.

    Pipeline:
      1. Validate provider prefix maps to a catalog entry with rerank_pricing.
      2. Allowlist gate (same fnmatch logic as chat/embeddings).
      3. Token preflight (estimate over query + all documents).
      4. Ingress guardrails on query + each document.
      5. Cache lookup (deterministic per query+documents+top_n+return_documents).
      6. Provider call.
      7. Cache write.
      8. Audit + usage record.
    """
    request_id = _request_id(request)

    # ---- 1. Validate provider prefix -----------------------------------
    provider_key, _bare = _split_model(body.model)
    if provider_key not in CATALOG:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "unknown_provider",
                "message": (
                    f"model {body.model!r} does not name a known provider "
                    f"(prefix before '/' must match a catalog entry)"
                ),
            },
        )
    catalog_entry = CATALOG[provider_key]
    if not catalog_entry.rerank_pricing:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "not_a_rerank_provider",
                "message": (
                    f"provider {provider_key!r} does not offer rerank models — "
                    "try cohere/rerank-english-v3.0 or voyage/rerank-2."
                ),
            },
        )

    # ---- 2. Per-team allowlist -----------------------------------------
    if not is_model_allowed(body.model, principal.allowed_models):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "model_not_allowed",
                "message": f"model {body.model!r} is not in this team's allowlist",
                "allowed_patterns": principal.allowed_models,
            },
        )

    # ---- 3. Token preflight --------------------------------------------
    estimated_tokens = _estimate_rerank_tokens(body.query, body.documents)
    preflight = await quota.check_preflight(
        session,
        principal.team_id,
        estimated_tokens,
    )
    response.headers["X-Pronaos-Preflight-Estimate"] = str(estimated_tokens)
    if not preflight.allowed:
        record_preflight_denial(reason=preflight.reason or "preflight_unknown")
        raise HTTPException(
            status_code=429,
            detail={
                "type": preflight.reason or "preflight_denied",
                "message": (
                    f"estimated {estimated_tokens} input tokens would "
                    "exceed this team's monthly token budget"
                ),
            },
        )

    # ---- 4. Ingress guardrails -----------------------------------------
    # Scan the query AND every document. Reranking sends them all to
    # the upstream, so PII in any of them is a leak risk.
    disabled_rules, policy_override = resolve_policy(principal.guardrail_policy)
    scanned_query: str
    scanned_documents: list[str] = []
    for label, text in [("query", body.query), *[("doc", d) for d in body.documents]]:
        verdict = guardrails.scan_ingress(
            text,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )
        for hit in verdict.hits:
            record_guardrail_hit(
                rule=hit.rule,
                action="ingress_scanned",
                direction="ingress",
            )
        if verdict.blocked:
            raise HTTPException(
                status_code=400,
                detail={
                    "type": "guardrail_blocked",
                    "message": f"input ({label}) blocked by guardrail policy",
                    "rule": verdict.block_reason,
                },
            )
        if label == "query":
            scanned_query = verdict.text
        else:
            scanned_documents.append(verdict.text)
    # MyPy needs help knowing scanned_query is bound after the loop.
    assert "scanned_query" in dir() or scanned_query is not None

    # ---- 5. Cache lookup -----------------------------------------------
    cache_payload = {
        "type": "rerank",
        "query": scanned_query,
        "documents": scanned_documents,
        "top_n": body.top_n,
        "return_documents": body.return_documents,
    }
    lookup = await cache.get(
        tenant_id=principal.tenant_id,
        model=body.model,
        key_payload=cache_payload,
    )
    if lookup.hit and lookup.response is not None:
        record_cache_lookup(tier=lookup.tier or "exact", result="hit")
        record_rerank_cache_hit(model=body.model)
        response.headers["X-Pronaos-Cache"] = f"hit:{lookup.tier or 'exact'}"
        return lookup.response
    record_cache_lookup(tier="exact", result="miss")
    response.headers["X-Pronaos-Cache"] = "miss"

    # ---- 6. Provider call (wrapped in singleflight) -------------------
    try:
        rerank_provider = registry.get_rerank(provider_key)
    except ProviderNotConfiguredError as e:
        raise HTTPException(
            status_code=503,
            detail={"type": "provider_not_configured", "message": str(e)},
        ) from e
    except UnknownProviderError as e:
        raise HTTPException(
            status_code=400,
            detail={"type": "unknown_provider", "message": str(e)},
        ) from e

    provider_req = RerankProviderRequest(
        model=body.model,
        query=scanned_query,
        documents=scanned_documents,
        top_n=body.top_n,
        return_documents=body.return_documents,
    )

    # Phase 33: singleflight. N concurrent identical rerank requests on
    # a cold cache → 1 upstream call; the rest become followers.
    sf_key = _singleflight_key(
        endpoint="rerank",
        tenant_id=principal.tenant_id,
        model=body.model,
        cache_payload=cache_payload,
    )

    async def _do_upstream() -> dict[str, Any]:
        """Leader's work: upstream rerank → cache write → return body+meta."""
        start = time.monotonic()
        try:
            inner_result = await rerank_provider.rerank(provider_req)
        except AuthError as e:
            record_rerank_error(provider=provider_key, model=body.model)
            raise HTTPException(
                status_code=502,
                detail={"type": "upstream_auth", "message": str(e)},
            ) from e
        except ProviderError as e:
            record_rerank_error(provider=provider_key, model=body.model)
            raise HTTPException(
                status_code=e.status if e.status >= 400 else 502,
                detail={"type": "upstream_error", "message": str(e)},
            ) from e
        inner_duration = time.monotonic() - start

        inner_cost = rerank_provider.cost_hcents(inner_result.prompt_tokens, body.model)
        inner_body: dict[str, Any] = {
            "object": "list",
            "data": [
                {
                    "index": item.index,
                    "relevance_score": item.relevance_score,
                    **({"document": item.document} if item.document is not None else {}),
                }
                for item in inner_result.results
            ],
            "model": body.model,
            "usage": {
                "prompt_tokens": inner_result.prompt_tokens,
                "total_tokens": inner_result.prompt_tokens,
            },
        }

        record_rerank_success(
            provider=provider_key,
            model=body.model,
            duration_seconds=inner_duration,
            prompt_tokens=inner_result.prompt_tokens,
            cost_hcents=inner_cost,
        )

        try:
            await cache.put(
                tenant_id=principal.tenant_id,
                model=body.model,
                key_payload=cache_payload,
                response=inner_body,
            )
        except Exception as e:
            log.warning("rerank.cache_put_failed", error=str(e))

        return {
            "response_body": inner_body,
            "cost_hcents": inner_cost,
            "prompt_tokens": inner_result.prompt_tokens,
        }

    singleflight = _get_singleflight(request)
    sf_result, was_follower = await singleflight.share(sf_key, _do_upstream)

    response_body = sf_result["response_body"]
    leader_cost = int(sf_result["cost_hcents"])
    prompt_tokens = int(sf_result["prompt_tokens"])
    effective_cost = 0 if was_follower else leader_cost

    response.headers["X-Pronaos-Provider"] = provider_key
    if was_follower:
        record_singleflight_follower(endpoint="rerank")
        response.headers["X-Pronaos-Singleflight"] = "follower"
        response.headers["X-Pronaos-Cost-Hcents"] = "0"
    else:
        response.headers["X-Pronaos-Cost-Hcents"] = str(leader_cost)

    # ---- 8. Audit + usage records (per-request) -----------------------
    try:
        await audit.append(
            session,
            tenant_id=principal.tenant_id,
            team_id=principal.team_id,
            key_id=principal.key_id,
            provider=provider_key,
            model=body.model,
            request_body={
                "query": scanned_query,
                "documents": scanned_documents,
                "model": body.model,
            },
            response_body=response_body,
            request_id=request_id,
        )
    except Exception as e:
        log.warning("rerank.audit_failed", error=str(e))
    await quota.record_call(
        session,
        CompletedCall(
            tenant_id=principal.tenant_id,
            team_id=principal.team_id,
            key_id=principal.key_id,
            provider=provider_key,
            model=body.model,
            prompt_tokens=prompt_tokens if not was_follower else 0,
            completion_tokens=0,
            cost_hcents=effective_cost,
            request_id=request_id,
            status="success",
            ab_arm=None,
        ),
    )
    await session.commit()

    return response_body


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _split_model(fqmn: str) -> tuple[str, str]:
    if "/" not in fqmn:
        return "", fqmn
    head, _, tail = fqmn.partition("/")
    return head, tail


def _estimate_rerank_tokens(query: str, documents: list[str]) -> int:
    """Whitespace-split heuristic over the query + every document.

    Same shape as the embedding estimator. The reranker upstream's
    actual token count comes back in the response's usage block — this
    is only for the preflight budget guardrail.
    """
    total = max(1, int(len(query.split()) * 1.15))
    for d in documents:
        total += max(1, int(len(d.split()) * 1.15))
    return total


def _request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id")
    if rid:
        return rid
    return uuid.uuid4().hex


def _singleflight_key(
    *,
    endpoint: str,
    tenant_id: str,
    model: str,
    cache_payload: dict[str, Any],
) -> str:
    """Build a stable string key for the singleflight registry.

    Same shape as embedding's helper — tenant isolation preserved,
    JSON-canonical so dict ordering doesn't matter.
    """
    import hashlib
    import json

    digest_input = json.dumps(
        {"t": tenant_id, "m": model, "p": cache_payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{endpoint}:{hashlib.sha256(digest_input.encode()).hexdigest()}"


def _get_singleflight(request: Request) -> SingleflightRegistry[dict[str, Any]]:
    """Pull the app-scoped singleflight registry, falling back to a
    fresh one if startup didn't install it."""
    sf: SingleflightRegistry[dict[str, Any]] | None = getattr(
        request.app.state, "singleflight", None
    )
    if sf is None:
        sf = SingleflightRegistry[dict[str, Any]]()
        request.app.state.singleflight = sf
    return sf
