"""Async batches endpoint (Phase 59).

POST /v1/batches — submit an inline batch of chat completions to
                   OpenAI or Anthropic at 50% of synchronous pricing.
GET  /v1/batches/{id} — poll status (Pronaos-normalized).
GET  /v1/batches/{id}/results — fetch result JSONL once completed.
POST /v1/batches/{id}/cancel — cancel an in-flight batch.

Routing
-------
The batch's provider is chosen from the **first request's model
prefix** (``openai/*`` → OpenAI, ``anthropic/*`` → Anthropic). All
requests in a single batch must target the same provider; mixed
batches are rejected at validate time with 422
``batch_mixed_providers``.

Per-team gate
-------------
Teams without ``batches_enabled`` get 422 ``batches_disabled``.
Operators turn it on per-team because batch quota usage is
non-trivial and the operator wants explicit opt-in.

State machine
-------------
Pronaos normalizes both providers' status onto:
    validating → in_progress → finalizing → completed
                                        ↘ failed | expired | cancelled

The background worker (``core/batch_worker.py``) polls each
in-flight batch every N minutes, updates the row, and on
``completed`` writes per-request ``usage_records`` at the half-
priced rate. Until that worker has run, ``GET /v1/batches/{id}``
returns the last-known status from the row.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db
from pronaos.config import get_settings
from pronaos.core.batches import (
    AnthropicBatchClient,
    OpenAIBatchClient,
    provider_from_model,
)
from pronaos.db.models import Batch
from pronaos.logging import get_logger
from pronaos.observability.metrics import record_batch_event

log = get_logger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response schemas                                                  #
# --------------------------------------------------------------------------- #


class BatchRequestEntry(BaseModel):
    """One inline request inside the batch."""

    custom_id: str = Field(..., min_length=1, max_length=128)
    body: dict[str, Any]


class CreateBatchBody(BaseModel):
    """POST /v1/batches request body.

    ``endpoint`` is informational in v1 (only ``/v1/chat/completions``
    is supported). The completion window passes through to the
    provider verbatim; both currently accept ``24h``.
    """

    endpoint: str = "/v1/chat/completions"
    completion_window: str = "24h"
    requests: list[BatchRequestEntry] = Field(..., min_length=1)
    metadata: dict[str, str] | None = None


class BatchResponse(BaseModel):
    """OpenAI-compatible response shape for ``GET /v1/batches/{id}``
    and the submit endpoint's return."""

    id: str
    object: str = "batch"
    provider: str
    provider_batch_id: str | None
    status: str
    endpoint: str
    completion_window: str
    request_counts: dict[str, int]
    created_at: int
    in_progress_at: int | None = None
    completed_at: int | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def _to_response(batch: Batch) -> BatchResponse:
    return BatchResponse(
        id=batch.id,
        provider=batch.provider,
        provider_batch_id=batch.provider_batch_id,
        status=batch.status,
        endpoint=batch.endpoint,
        completion_window=batch.completion_window,
        request_counts={
            "total": batch.request_count,
            "completed": batch.completed_count,
            "failed": batch.failed_count,
        },
        created_at=_epoch(batch.created_at) or 0,
        in_progress_at=_epoch(batch.in_progress_at),
        completed_at=_epoch(batch.completed_at),
        error_message=batch.error_message,
    )


def _serialize_requests_to_jsonl(
    requests: list[BatchRequestEntry],
    *,
    endpoint: str = "/v1/chat/completions",
) -> tuple[str, str]:
    """Serialize the inline requests to JSONL + figure out the
    provider from the first request's model. Validates that all
    requests target the same provider.

    Returns (jsonl, provider_key). The ``url`` field on each JSONL
    line carries the upstream endpoint (matching the batch's
    target: ``/v1/chat/completions`` or ``/v1/embeddings``).
    """
    if not requests:
        raise HTTPException(
            status_code=422,
            detail={"type": "batch_empty", "hint": "requests array is empty"},
        )
    # Provider routing from first request.
    first_model = (requests[0].body or {}).get("model")
    if not isinstance(first_model, str):
        raise HTTPException(
            status_code=422,
            detail={
                "type": "batch_missing_model",
                "hint": "every request must specify a model in its body",
            },
        )
    try:
        provider = provider_from_model(first_model)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"type": "batch_unsupported_provider", "hint": str(e)},
        ) from e

    # Every other request must agree.
    for entry in requests[1:]:
        m = (entry.body or {}).get("model")
        if not isinstance(m, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "batch_missing_model",
                    "hint": f"request {entry.custom_id!r} missing model",
                },
            )
        try:
            other_provider = provider_from_model(m)
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail={"type": "batch_unsupported_provider", "hint": str(e)},
            ) from e
        if other_provider != provider:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "batch_mixed_providers",
                    "hint": (
                        f"batch has requests for both {provider} and "
                        f"{other_provider}; all requests must target the "
                        "same provider"
                    ),
                },
            )

    # Embeddings batches are OpenAI-only (Anthropic doesn't expose
    # an embeddings API at all). Reject Anthropic + embeddings here
    # with a clear 422 — surfacing the limit before we hit the
    # provider client's stricter assert.
    if endpoint == "/v1/embeddings" and provider == "anthropic":
        raise HTTPException(
            status_code=422,
            detail={
                "type": "embeddings_batch_unsupported_provider",
                "hint": (
                    "Anthropic does not offer an embeddings API; "
                    "embedding batches must target an OpenAI model"
                ),
            },
        )

    # Serialize. We strip the provider prefix on the model field so
    # the upstream sees its native bare model name.
    lines: list[str] = []
    for entry in requests:
        body = dict(entry.body)
        m = body.get("model", "")
        if isinstance(m, str) and "/" in m:
            body["model"] = m.split("/", 1)[-1]
        lines.append(
            json.dumps(
                {
                    "custom_id": entry.custom_id,
                    "method": "POST",
                    "url": endpoint,
                    "body": body,
                },
                separators=(",", ":"),
            )
        )
    return "\n".join(lines) + "\n", provider


def _batch_id() -> str:
    """Pronaos-side opaque batch id. ``pron_batch_`` prefix
    distinguishes our id space from the upstream's."""
    return f"pron_batch_{secrets.token_urlsafe(16)}"


def _make_client(provider: str) -> OpenAIBatchClient | AnthropicBatchClient:
    settings = get_settings()
    if provider == "openai":
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=503,
                detail={
                    "type": "batches_provider_unavailable",
                    "hint": "openai api key not configured on the gateway",
                },
            )
        return OpenAIBatchClient(api_key=settings.openai_api_key)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise HTTPException(
                status_code=503,
                detail={
                    "type": "batches_provider_unavailable",
                    "hint": "anthropic api key not configured on the gateway",
                },
            )
        return AnthropicBatchClient(api_key=settings.anthropic_api_key)
    raise HTTPException(
        status_code=422,
        detail={"type": "batch_unsupported_provider", "hint": provider},
    )


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def _require_batches_enabled(principal: Principal) -> None:
    if not principal.batches_enabled:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "batches_disabled",
                "hint": (
                    "this team is not enabled for the batches API; ask "
                    "an admin to set batches_enabled=true on the team"
                ),
            },
        )


@router.post("/batches", response_model=BatchResponse)
async def create_batch(
    body: CreateBatchBody,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> BatchResponse:
    """Submit an inline batch of chat completions.

    The provider is chosen from the first request's model prefix.
    All requests in the batch must target the same provider.
    """
    _require_batches_enabled(principal)
    if body.endpoint not in ("/v1/chat/completions", "/v1/embeddings"):
        raise HTTPException(
            status_code=422,
            detail={
                "type": "batch_endpoint_unsupported",
                "hint": ("supported endpoints: /v1/chat/completions, /v1/embeddings (Phase 60)"),
            },
        )

    jsonl, provider = _serialize_requests_to_jsonl(body.requests, endpoint=body.endpoint)

    # Submit to the upstream provider before persisting the DB row
    # so a provider rejection surfaces as 422 to the caller (rather
    # than leaving a half-built row in 'validating' state).
    client = _make_client(provider)
    try:
        submission = await client.submit(requests_jsonl=jsonl, endpoint=body.endpoint)
    finally:
        await client.aclose()

    now = datetime.now(UTC)
    batch = Batch(
        id=_batch_id(),
        tenant_id=principal.tenant_id,
        team_id=principal.team_id,
        key_id=principal.key_id,
        provider=provider,
        provider_batch_id=submission.provider_batch_id,
        status=submission.initial_status,
        endpoint=body.endpoint,
        completion_window=body.completion_window,
        request_count=len(body.requests),
        completed_count=0,
        failed_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_hcents=0,
        created_at=now,
        input_payload=jsonl,
        output_payload="",
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    # Phase 59 metric counter — track submitted batches by provider.
    record_batch_event(provider=provider, status=submission.initial_status)

    response.headers["X-Pronaos-Batch-Id"] = batch.id
    response.headers["X-Pronaos-Batch-Provider"] = provider
    return _to_response(batch)


@router.get("/batches/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BatchResponse:
    """Return the team's batch row by id. Status reflects the last
    poll; the background worker keeps it fresh."""
    _require_batches_enabled(principal)
    batch = await _load_batch_for_caller(session, batch_id, principal)
    return _to_response(batch)


@router.get("/batches/{batch_id}/results", response_class=Response)
async def get_batch_results(
    batch_id: str,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Stream the result JSONL once the batch has completed.
    Returns 409 ``batch_not_completed`` if the batch is still
    in flight."""
    _require_batches_enabled(principal)
    batch = await _load_batch_for_caller(session, batch_id, principal)
    if batch.status != "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "type": "batch_not_completed",
                "hint": f"batch is in state {batch.status!r}; results "
                "are only available after it reaches 'completed'",
            },
        )
    return Response(
        content=batch.output_payload,
        media_type="application/jsonl",
    )


@router.post("/batches/{batch_id}/cancel", response_model=BatchResponse)
async def cancel_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BatchResponse:
    """Best-effort cancel. If the batch already terminal, the row
    is returned unchanged. If in flight, the provider is asked to
    cancel and the row is marked ``cancelled`` immediately —
    subsequent polls confirm the state with the upstream."""
    _require_batches_enabled(principal)
    batch = await _load_batch_for_caller(session, batch_id, principal)
    if batch.status in {"completed", "failed", "expired", "cancelled"}:
        # Idempotent: return current state without touching the upstream.
        return _to_response(batch)

    if batch.provider_batch_id:
        client = _make_client(batch.provider)
        try:
            await client.cancel(provider_batch_id=batch.provider_batch_id)
        finally:
            await client.aclose()

    batch.status = "cancelled"
    batch.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(batch)
    return _to_response(batch)


# --------------------------------------------------------------------------- #
# Internal: row loader with tenant/team enforcement                           #
# --------------------------------------------------------------------------- #


async def _load_batch_for_caller(
    session: AsyncSession, batch_id: str, principal: Principal
) -> Batch:
    """Load a batch row by id, enforcing that it belongs to the
    caller's tenant + team. 404 on either miss — same shape as
    other admin-side scoped reads."""
    result = await session.execute(select(Batch).where(Batch.id == batch_id))
    batch = result.scalar_one_or_none()
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "batch_not_found", "hint": batch_id},
        )
    if batch.tenant_id != principal.tenant_id or batch.team_id != principal.team_id:
        # Same posture as the admin endpoints: a caller from a different
        # tenant gets a 404 to avoid leaking existence.
        raise HTTPException(
            status_code=404,
            detail={"type": "batch_not_found", "hint": batch_id},
        )
    return batch
