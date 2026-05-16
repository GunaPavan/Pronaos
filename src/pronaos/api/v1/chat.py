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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db, get_quota_tracker
from pronaos.core.failover import execute_with_failover
from pronaos.core.quota import QuotaTracker
from pronaos.core.router import Router
from pronaos.logging import get_logger
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


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionBody,
    route: Annotated[Router, Depends(get_router)],
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    quota: Annotated[QuotaTracker, Depends(get_quota_tracker)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    prov_req = ProviderRequest(
        model=body.model,
        messages=[m.model_dump() for m in body.messages],
        stream=body.stream,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    plan = route.resolve(body.model)

    provider, stream = await execute_with_failover(plan, prov_req)

    log.info(
        "chat.request",
        provider=provider.name,
        model=body.model,
        tenant=principal.tenant_name,
        team=principal.team_name,
    )

    if body.stream:
        return _handle_streaming(stream, provider, body.model, principal, quota, session)
    return await _handle_non_streaming(stream, provider, body.model, principal, quota, session)


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
) -> dict[str, Any]:
    chunk: ChatCompletionChunk | None = None
    async for c in stream:
        chunk = c
        break

    if chunk is None:
        raise HTTPException(status_code=502, detail="provider produced no response")

    prompt_tokens = chunk.prompt_tokens or 0
    completion_tokens = chunk.completion_tokens or 0
    total_tokens = prompt_tokens + completion_tokens

    # Record usage *after* a successful provider call. Failure here is logged
    # but never raises — the response is already constructed and we don't
    # want to 5xx the client over a metrics gap.
    await quota.record_usage(session, principal.team_id, total_tokens)

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
            "cost_hcents": provider.cost_cents(prompt_tokens, completion_tokens, model),
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

    # Record usage post-stream (best-effort; failures are logged, not raised).
    await quota.record_usage(session, principal.team_id, prompt_tokens + completion_tokens)

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


def _usage(prompt: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
