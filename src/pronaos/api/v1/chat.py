"""OpenAI-compatible chat completions endpoint.

Phase 2 scope:
- Request's ``model`` field is parsed by the Router into a primary provider
  and (if configured) a fallback chain.
- ``execute_with_failover`` walks the chain until one provider starts
  returning bytes, then commits to it.
- Streaming path emits OpenAI-shape SSE regardless of underlying provider.

Later phases will insert auth, quota, cache, and guardrails into this
handler. Keep this file focused on HTTP-shape translation; business logic
belongs in layers below.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db, get_quota_tracker
from pronaos.cache.base import Cache
from pronaos.core.failover import execute_with_failover
from pronaos.core.quota import CompletedCall, QuotaTracker
from pronaos.core.router import Router
from pronaos.logging import get_logger
from pronaos.observability.metrics import (
    record_cache_lookup,
    record_provider_error,
    record_provider_success,
)
from pronaos.observability.otel import get_tracer
from pronaos.providers.base import ChatCompletionChunk, Provider
from pronaos.providers.base import ChatCompletionRequest as ProviderRequest
from pronaos.providers.registry import ProviderRegistry

log = get_logger(__name__)
router = APIRouter(tags=["chat"])


# --------------------------------------------------------------------------- #
# Request model                                                               #
# --------------------------------------------------------------------------- #


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionBody(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=100_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


# --------------------------------------------------------------------------- #
# Dependency                                                                  #
# --------------------------------------------------------------------------- #


def get_registry(request: Request) -> ProviderRegistry:
    """Expose the app-scoped provider registry to handlers."""
    registry: ProviderRegistry | None = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise RuntimeError("provider registry not initialised on app.state")
    return registry


def get_router(request: Request) -> Router:
    """Expose the app-scoped router to handlers."""
    router_instance: Router | None = getattr(request.app.state, "router", None)
    if router_instance is None:
        raise RuntimeError("router not initialised on app.state")
    return router_instance


def get_cache(request: Request) -> Cache:
    """Expose the app-scoped cache to handlers.

    Falls back to a NullCache if startup didn't install one — keeps the
    handler simple (always has *something* to call) and ensures a
    misconfiguration produces "cache disabled" not "AttributeError"."""
    cache: Cache | None = getattr(request.app.state, "cache", None)
    if cache is None:
        from pronaos.cache.null import NullCache

        return NullCache()
    return cache


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionBody,
    response: Response,
    route: Annotated[Router, Depends(get_router)],
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    quota: Annotated[QuotaTracker, Depends(get_quota_tracker)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cache: Annotated[Cache, Depends(get_cache)],
) -> Any:
    # ---- Cache lookup (Phase 7) -----------------------------------------
    # Only deterministic, non-streaming requests are cache-eligible:
    # streaming defeats the purpose of token-by-token UX, and
    # temperature>0 is the user explicitly asking for variety. Both still
    # increment a ``skip`` metric so dashboards can show *why* hit-rate is
    # what it is.
    cache_eligible = (
        not body.stream and (body.temperature is None or body.temperature == 0.0)
    )
    if cache_eligible:
        lookup = await cache.get(
            tenant_id=principal.tenant_id,
            model=body.model,
            key_payload=_canonical_cache_payload(body),
        )
        if lookup.hit and lookup.response is not None:
            record_cache_lookup(tier=lookup.tier or "exact", result="hit")
            # X-Pronaos-Cache: hit:<tier>[:<similarity>] lets clients (and
            # the demo script) read the verdict directly from headers
            # rather than inferring it from latency. Mutating the cached
            # body would be a quiet correctness bug — return as-is.
            header_val = f"hit:{lookup.tier or 'exact'}"
            if lookup.similarity is not None:
                header_val += f":{lookup.similarity:.4f}"
            response.headers["X-Pronaos-Cache"] = header_val
            return lookup.response
        record_cache_lookup(tier="exact", result="miss")
        response.headers["X-Pronaos-Cache"] = "miss"
    else:
        record_cache_lookup(tier="exact", result="skip")
        response.headers["X-Pronaos-Cache"] = "skip"

    prov_req = ProviderRequest(
        model=body.model,
        messages=[m.model_dump() for m in body.messages],
        stream=body.stream,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    plan = route.resolve(body.model)

    # Time from BEFORE failover starts so the histogram includes any retry
    # cost from the failover layer — recruiters / SREs want to see the
    # whole upstream latency story, not just the winning provider's wire time.
    provider_call_start = time.monotonic()
    provider, stream = await execute_with_failover(plan, prov_req)

    log.info(
        "chat.request",
        provider=provider.name,
        model=body.model,
        tenant=principal.tenant_name,
        team=principal.team_name,
    )

    if body.stream:
        return _handle_streaming(
            stream, provider, body.model, principal, quota, session, provider_call_start
        )
    response = await _handle_non_streaming(
        stream, provider, body.model, principal, quota, session, provider_call_start
    )
    # ---- Cache write (Phase 7) ------------------------------------------
    # Only the deterministic path populates the cache. Fail-open: a cache
    # write failure is logged inside the backend, never raised here.
    if cache_eligible:
        await cache.put(
            tenant_id=principal.tenant_id,
            model=body.model,
            key_payload=_canonical_cache_payload(body),
            response=response,
        )
    return response


def _canonical_cache_payload(body: ChatCompletionBody) -> dict[str, Any]:
    """Strip the request down to the fields that affect the response.

    Anything else (stream flag is non-determinative after we've decided to
    cache; principal/auth info is in the key path) is excluded so cosmetic
    changes don't cache-miss against an otherwise identical request."""
    return {
        "messages": [m.model_dump() for m in body.messages],
        "temperature": body.temperature or 0.0,
        "max_tokens": body.max_tokens,
    }


# --------------------------------------------------------------------------- #
# Non-streaming                                                               #
# --------------------------------------------------------------------------- #


async def _handle_non_streaming(
    stream: AsyncIterator[ChatCompletionChunk],
    provider: Provider,
    model: str,
    principal: Principal,
    quota: QuotaTracker,
    session: AsyncSession,
    provider_call_start: float,
) -> dict[str, Any]:
    # Phase 6.3: explicit span for the provider call so trace exploration
    # can pivot on provider/model/tokens without filtering through FastAPI's
    # auto-generated parent span. Streaming path is intentionally not yet
    # span-wrapped — wrapping an async generator across yields gets fiddly
    # and the metrics already cover the streaming latency story.
    tracer = get_tracer("pronaos.provider")
    with tracer.start_as_current_span("pronaos.provider.call") as span:
        span.set_attribute("pronaos.provider", provider.name)
        span.set_attribute("pronaos.model", model)

        chunk: ChatCompletionChunk | None = None
        async for c in stream:
            chunk = c
            break

        if chunk is None:
            # Provider failed to emit anything. Count it before raising so the
            # Prometheus error counter still moves under degraded upstreams.
            record_provider_error(provider.name, model)
            span.set_attribute("pronaos.provider.error", "no_response")
            raise HTTPException(status_code=502, detail="provider produced no response")

        prompt_tokens = chunk.prompt_tokens or 0
        completion_tokens = chunk.completion_tokens or 0
        cost_hcents = provider.cost_cents(prompt_tokens, completion_tokens, model)
        duration = time.monotonic() - provider_call_start

        # Hot span attributes — these are what an SRE asks of a trace.
        span.set_attribute("pronaos.prompt_tokens", prompt_tokens)
        span.set_attribute("pronaos.completion_tokens", completion_tokens)
        span.set_attribute("pronaos.cost_hcents", cost_hcents)
        span.set_attribute("pronaos.duration_seconds", duration)

    # Phase 6: provider counters & histogram. Recorded BEFORE the DB write so
    # a write failure doesn't blank out the operational metric.
    record_provider_success(
        provider=provider.name,
        model=model,
        duration_seconds=duration,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_hcents=cost_hcents,
    )

    # Phase 5: persist a per-call audit row + increment the team budget.
    # Failure is logged but never raises — the response has already been
    # constructed and we won't 5xx the client over a metrics gap.
    await quota.record_call(
        session,
        CompletedCall(
            tenant_id=principal.tenant_id,
            team_id=principal.team_id,
            key_id=principal.key_id,
            provider=provider.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_hcents=cost_hcents,
            request_id=_current_request_id(),
        ),
    )

    return {
        "id": _chat_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": chunk.content_delta},
                "finish_reason": chunk.finish_reason or "stop",
            }
        ],
        "usage": _usage(prompt_tokens, completion_tokens),
        "pronaos": {
            "provider": provider.name,
            "cost_hcents": cost_hcents,
        },
    }


# --------------------------------------------------------------------------- #
# Streaming                                                                   #
# --------------------------------------------------------------------------- #


def _handle_streaming(
    provider_stream: AsyncIterator[ChatCompletionChunk],
    provider: Provider,
    model: str,
    principal: Principal,
    quota: QuotaTracker,
    session: AsyncSession,
    provider_call_start: float,
) -> StreamingResponse:
    # The stream has already been resolved by the failover executor — any
    # construction-time error was surfaced there and converted to a JSON
    # response by the global error handler. From here on, errors are
    # mid-stream and can only be logged.
    generator = _sse_openai_chunks(
        provider_stream,
        provider=provider,
        model=model,
        principal=principal,
        quota=quota,
        session=session,
        provider_call_start=provider_call_start,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_openai_chunks(
    provider_stream: AsyncIterator[ChatCompletionChunk],
    *,
    provider: Provider,
    model: str,
    principal: Principal,
    quota: QuotaTracker,
    session: AsyncSession,
    provider_call_start: float,
) -> AsyncIterator[str]:
    request_id = _chat_id()
    created = int(time.time())
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason: str | None = None

    # Opening chunk: role marker (OpenAI convention).
    yield _sse(
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    try:
        async for chunk in provider_stream:
            if chunk.content_delta:
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk.content_delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            if chunk.prompt_tokens is not None:
                prompt_tokens = chunk.prompt_tokens
            if chunk.completion_tokens is not None:
                completion_tokens = chunk.completion_tokens
    except Exception as e:
        # Once the stream has started, all we can do is log — headers are sent
        # and the client already got 200 OK. The next phase adds an SSE error
        # event so clients can distinguish a clean finish from a torn stream.
        log.error("stream.error", error=str(e), provider=provider.name, model=model)
        finish_reason = finish_reason or "error"
        record_provider_error(provider.name, model)
    else:
        duration = time.monotonic() - provider_call_start
        record_provider_success(
            provider=provider.name,
            model=model,
            duration_seconds=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_hcents=provider.cost_cents(prompt_tokens, completion_tokens, model),
        )

    # Phase 5: persist a per-call audit row + increment the team budget.
    # Best-effort; failures are logged, not raised.
    await quota.record_call(
        session,
        CompletedCall(
            tenant_id=principal.tenant_id,
            team_id=principal.team_id,
            key_id=principal.key_id,
            provider=provider.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_hcents=provider.cost_cents(prompt_tokens, completion_tokens, model),
            request_id=_current_request_id(),
            status=finish_reason or "success",
        ),
    )

    # Final chunk: finish reason + usage. The ``usage`` field is an additive
    # extension (OpenAI emits it only when stream_options.include_usage=True);
    # we always emit so downstream FinOps has the number for free.
    yield _sse(
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}],
            "usage": _usage(prompt_tokens, completion_tokens),
            "pronaos": {
                "provider": provider.name,
                "cost_hcents": provider.cost_cents(prompt_tokens, completion_tokens, model),
            },
        }
    )
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _chat_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _current_request_id() -> str | None:
    """Return the request_id bound by RequestContextMiddleware, if any.

    Read from structlog's contextvars so we don't need to thread a Request
    object through every handler. Returns None outside of a request scope
    (handler unit tests that bypass the middleware).
    """
    return structlog.contextvars.get_contextvars().get("request_id")


def _usage(prompt: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
