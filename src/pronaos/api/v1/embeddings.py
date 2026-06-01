"""OpenAI-compatible /v1/embeddings endpoint.

Phase 31 expands the gateway's public surface from chat-only to
chat + embeddings. The endpoint reuses every existing layer:

- **Auth**: API-key or OIDC, via the same ``enforce_quotas`` dep.
- **Model allowlist**: per-team fnmatch patterns gate which embedding
  models can be called.
- **Token quota**: input tokens count against the same monthly budget
  as chat (the budget axis is "tokens consumed," not "completions
  generated"). Cost quota counts the embedding spend.
- **Ingress guardrails**: every input text passes through the PII
  scanner. By default PII is redacted before reaching the upstream —
  embedding a credit card or a phone number is a worse data-exposure
  story than embedding the redacted ``[REDACTED-PHONE]`` placeholder.
  Egress guardrails don't apply (the response is a vector, not text).
- **Cache**: deterministic per model. The L1 exact cache produces 100%
  hits on repeated inputs — that's the killer feature for RAG
  workloads where document re-ingestion is common.
- **Audit log**: one chain-linked record per call (request_hash =
  hash of input text(s); response_hash = hash of the vector list).
- **Usage records**: one row per call. ``completion_tokens=0``
  (embeddings have none); ``ab_arm=None`` (no A/B harness on
  embeddings yet).
- **Metrics**: ``pronaos_embedding_requests_total``,
  ``pronaos_embedding_request_duration_seconds``,
  ``pronaos_embedding_tokens_total``,
  ``pronaos_embedding_cache_hits_total``.

Response shape matches OpenAI exactly so OpenAI client SDKs work
unmodified:

    POST /v1/embeddings
    {
      "model": "openai/text-embedding-3-small",
      "input": "Pronaos is a gateway." OR ["array", "of", "strings"],
      "encoding_format": "float",            # optional, "float" default
      "dimensions": 512                      # optional, OpenAI v3.x only
    }

Response:

    {
      "object": "list",
      "data": [{"object": "embedding", "embedding": [...], "index": 0}, ...],
      "model": "openai/text-embedding-3-small",
      "usage": {"prompt_tokens": N, "total_tokens": N}
    }
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the dependency providers wired in chat.py — they read the same
# app.state slots we need (registry, cache, guardrails, audit).
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
    record_embedding_cache_hit,
    record_embedding_error,
    record_embedding_success,
    record_guardrail_hit,
    record_preflight_denial,
    record_singleflight_follower,
)
from pronaos.providers.base import AuthError, ProviderError
from pronaos.providers.catalog import CATALOG
from pronaos.providers.embeddings import (
    EmbeddingProviderRequest,
    normalize_input_texts,
)
from pronaos.providers.registry import (
    ProviderNotConfiguredError,
    ProviderRegistry,
    UnknownProviderError,
)

log = get_logger(__name__)
router = APIRouter(tags=["embeddings"])


# --------------------------------------------------------------------------- #
# Request / response models                                                   #
# --------------------------------------------------------------------------- #


class EmbeddingsRequest(BaseModel):
    """OpenAI-compatible embeddings request.

    ``input`` accepts both shapes OpenAI does: a single string or a list
    of strings. Internal layers always see a list (see
    :func:`pronaos.providers.embeddings.normalize_input_texts`).

    ``dimensions`` is the OpenAI v3.x extension that asks for a
    reduced-dimensionality vector. Providers that don't support it
    silently ignore it (or 400 — depends on the upstream).

    ``input_type`` is the Cohere/Voyage hint (``query`` vs ``document``
    etc.). OpenAI ignores it. We accept it on every request and pass
    through to providers that care.

    ``user`` is OpenAI's caller-supplied user identifier — we accept
    it for client-SDK compatibility but don't currently propagate it
    to the upstream (a future phase could surface it for per-user
    cost attribution inside a team).
    """

    model: str
    input: str | list[str]
    encoding_format: str = Field(default="float", pattern=r"^(float|base64)$")
    dimensions: int | None = Field(default=None, ge=1, le=4096)
    input_type: str | None = None
    user: str | None = None


class EmbeddingDatum(BaseModel):
    """One vector + its index in the input list."""

    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingsUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingDatum]
    model: str
    usage: EmbeddingsUsage


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(
    request: Request,
    body: EmbeddingsRequest,
    response: Response,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    quota: Annotated[QuotaTracker, Depends(get_quota_tracker)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cache: Annotated[Cache, Depends(get_cache)],
    guardrails: Annotated[GuardrailEngine, Depends(get_guardrails)],
    audit: Annotated[AuditLogger, Depends(get_audit_logger)],
) -> Any:
    """Compute embeddings for ``body.input``.

    Pipeline:
      1. Validate the model has a known provider prefix.
      2. Allowlist gate (same fnmatch logic as chat).
      3. Token preflight: rough estimate vs team budget; deny early if
         the input would push the team over its monthly token cap.
      4. Ingress guardrails: scan each input text for PII; apply the
         team policy's action (BLOCK / REDACT / LOG_ONLY).
      5. Cache lookup: if every input was previously embedded under
         the same (model, dimensions), serve from cache. ``X-Pronaos-Cache``
         response header reports ``hit:exact`` or ``miss``.
      6. Provider call.
      7. Cache write.
      8. Audit append + usage record write (incl. token + cost increment).
    """
    request_id = _request_id(request)

    # ---- 1. Validate provider prefix -----------------------------------
    # The "local" provider is special: it's not in the catalog (no
    # pricing, no API key) but the registry knows how to build it via
    # ``get_embedding("local")``. We accept any model name under
    # ``local/`` — the local sentence-transformers backend uses the
    # configured DEFAULT_MODEL_NAME regardless. This lets contributors
    # run the live demo without any paid API key.
    provider_key, _bare = _split_model(body.model)
    if provider_key != "local":
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
        if not catalog_entry.embedding_pricing:
            raise HTTPException(
                status_code=400,
                detail={
                    "type": "not_an_embedding_provider",
                    "message": (
                        f"provider {provider_key!r} does not offer embedding "
                        "models — try one of: openai/text-embedding-3-small, "
                        "cohere/embed-english-v3.0, voyage/voyage-3, "
                        "mistral/mistral-embed, local/all-MiniLM-L6-v2."
                    ),
                },
            )

    # ---- 2. Per-team allowlist -----------------------------------------
    if not is_model_allowed(body.model, principal.allowed_models):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "model_not_allowed",
                "message": (f"model {body.model!r} is not in this team's allowlist"),
                "allowed_patterns": principal.allowed_models,
            },
        )

    # ---- 3. Token preflight --------------------------------------------
    # Normalise input to list[str]. The provider layer always sees a list.
    input_texts = normalize_input_texts(body.input)
    if not input_texts:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "empty_input",
                "message": "embeddings request must include at least one input string",
            },
        )

    # Rough heuristic: whitespace-split tokens + 15% punctuation overhead.
    # Embedding tokenizers are model-specific so we can't be exact without
    # tiktoken-style tokeniser code per model. The preflight is a budget
    # guardrail, not a billing oracle — we only need an order-of-magnitude
    # estimate to short-circuit obvious budget breaches.
    estimated_tokens = _estimate_input_tokens(input_texts)
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
    # Scan each input text. Policy resolution mirrors chat.py — the
    # team's per-tenant overrides take effect at this layer. The engine
    # returns a single GuardrailVerdict per text: ``blocked`` plus the
    # possibly-redacted ``text`` we must use downstream.
    disabled_rules, policy_override = resolve_policy(principal.guardrail_policy)
    scanned_texts: list[str] = []
    for text in input_texts:
        verdict = guardrails.scan_ingress(
            text,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )
        for hit in verdict.hits:
            # The engine's policy resolution maps the hit to its final
            # action; we surface the rule name here (action label is
            # recorded inside the engine for hits that triggered the
            # policy — see chat handler's equivalent reporting).
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
                    "message": "input blocked by guardrail policy",
                    "rule": verdict.block_reason,
                },
            )
        scanned_texts.append(verdict.text)

    # ---- 5. Cache lookup -----------------------------------------------
    # Cache key includes model + dimensions + the canonicalised input
    # list (after redaction). Two clients embedding the same redacted
    # text under the same model share a cache entry by construction.
    cache_payload = {
        "type": "embedding",
        "input": scanned_texts,
        "dimensions": body.dimensions,
        "input_type": body.input_type,
        "encoding_format": body.encoding_format,
    }
    lookup = await cache.get(
        tenant_id=principal.tenant_id,
        model=body.model,
        key_payload=cache_payload,
    )
    if lookup.hit and lookup.response is not None:
        record_cache_lookup(tier=lookup.tier or "exact", result="hit")
        record_embedding_cache_hit(model=body.model)
        response.headers["X-Pronaos-Cache"] = f"hit:{lookup.tier or 'exact'}"
        # The cached response is a complete OpenAI-shape embeddings
        # response body. Return as-is. Don't write a usage row — the
        # original call's usage row is the authoritative spend record;
        # a cache hit is by definition zero upstream cost.
        return lookup.response
    record_cache_lookup(tier="exact", result="miss")
    response.headers["X-Pronaos-Cache"] = "miss"

    # ---- 6. Provider call (wrapped in singleflight) -------------------
    # Resolve the provider BEFORE singleflight — config errors are
    # per-request concerns; we don't want a misconfigured tenant's
    # provider lookup error to propagate to other tenants who happen
    # to share a cache key prefix.
    try:
        embedding_provider = registry.get_embedding(provider_key)
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

    provider_req = EmbeddingProviderRequest(
        model=body.model,
        input_texts=scanned_texts,
        dimensions=body.dimensions,
        input_type=body.input_type,
        encoding_format=body.encoding_format,
    )

    # Phase 33: singleflight. When N concurrent identical requests arrive
    # on a cold cache, only the first does the upstream call + cache
    # write; the rest become followers awaiting the leader's future.
    # Followers get the same response body at zero upstream cost.
    sf_key = _singleflight_key(
        endpoint="embedding",
        tenant_id=principal.tenant_id,
        model=body.model,
        cache_payload=cache_payload,
    )

    async def _do_upstream() -> dict[str, Any]:
        """Leader's work: upstream call → cache write → return body+meta."""
        start = time.monotonic()
        try:
            inner_result = await embedding_provider.embed(provider_req)
        except AuthError as e:
            record_embedding_error(provider=provider_key, model=body.model)
            raise HTTPException(
                status_code=502,
                detail={"type": "upstream_auth", "message": str(e)},
            ) from e
        except ProviderError as e:
            record_embedding_error(provider=provider_key, model=body.model)
            raise HTTPException(
                status_code=e.status if e.status >= 400 else 502,
                detail={"type": "upstream_error", "message": str(e)},
            ) from e
        inner_duration = time.monotonic() - start

        inner_cost = embedding_provider.cost_hcents(inner_result.prompt_tokens, body.model)
        inner_body: dict[str, Any] = {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": vec, "index": i}
                for i, vec in enumerate(inner_result.vectors)
            ],
            "model": body.model,
            "usage": {
                "prompt_tokens": inner_result.prompt_tokens,
                "total_tokens": inner_result.prompt_tokens,
            },
        }

        record_embedding_success(
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
            log.warning("embedding.cache_put_failed", error=str(e))

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
    # Leader charges the upstream cost; followers paid nothing — keep
    # usage_records faithful to actual upstream spend.
    effective_cost = 0 if was_follower else leader_cost

    response.headers["X-Pronaos-Provider"] = provider_key
    if was_follower:
        record_singleflight_follower(endpoint="embedding")
        response.headers["X-Pronaos-Singleflight"] = "follower"
        # Followers paid nothing — surface zero cost on the header.
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
            request_body={"input": scanned_texts, "model": body.model},
            response_body=response_body,
            request_id=request_id,
        )
    except Exception as e:
        log.warning("embedding.audit_failed", error=str(e))
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
    """Split ``provider/model`` into its components.

    Returns ``("", fqmn)`` when there's no prefix — the caller surfaces
    that as a 400, so we don't raise here.
    """
    if "/" not in fqmn:
        return "", fqmn
    head, _, tail = fqmn.partition("/")
    return head, tail


def _estimate_input_tokens(texts: list[str]) -> int:
    """Whitespace-split heuristic for embeddings preflight.

    Embedding tokenisers vary per model (BPE, WordPiece, SentencePiece).
    Without bundling tiktoken / each model's vocab the gateway can't
    be exact. This estimator hits within ~30% of the actual count for
    English text — good enough for budget guardrailing.
    """
    total = 0
    for t in texts:
        # words * ~1.15 to account for sub-word splits + punctuation.
        words = len(t.split())
        total += max(1, int(words * 1.15))
    return total


def _request_id(request: Request) -> str:
    """Pull the request_id middleware stashed on the request scope, or
    invent one if (somehow) it's missing — we never want audit rows
    with NULL request_id."""
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

    Same shape as the cache key: tenant isolation is preserved (two
    tenants embedding the same text do NOT share the singleflight
    leader), and the model + canonicalised payload determine
    uniqueness. JSON-canonical so dict ordering doesn't matter.
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
    fresh one if startup didn't install it (test fixtures sometimes
    bypass the lifespan)."""
    sf: SingleflightRegistry[dict[str, Any]] | None = getattr(
        request.app.state, "singleflight", None
    )
    if sf is None:
        sf = SingleflightRegistry[dict[str, Any]]()
        request.app.state.singleflight = sf
    return sf
