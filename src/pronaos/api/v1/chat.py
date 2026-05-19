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

import asyncio
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

from pronaos.audit.logger import AuditLogger
from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db, get_quota_tracker
from pronaos.cache.base import Cache
from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.core.failover import execute_with_failover
from pronaos.core.model_access import is_model_allowed
from pronaos.core.quota import CompletedCall, QuotaTracker
from pronaos.core.router import Router
from pronaos.core.scorer import (
    NoEligibleModelError,
    RoutingRequest,
    RoutingStrategy,
    select_model,
)
from pronaos.core.token_estimator import DEFAULT_MAX_COMPLETION, estimate_tokens
from pronaos.core.webhooks import (
    WebhookConfig,
    WebhookDispatcher,
    circuit_tripped_event,
)
from pronaos.guardrails.base import (
    GuardrailAction,
    GuardrailEngine,
    NullGuardrailEngine,
)
from pronaos.guardrails.policy import resolve_policy
from pronaos.logging import get_logger
from pronaos.observability.metrics import (
    record_cache_lookup,
    record_guardrail_hit,
    record_preflight_denial,
    record_provider_error,
    record_provider_success,
    record_routing_decision,
    record_stream_cancelled,
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
    """OpenAI-compatible chat message.

    Supports four roles, in order of common-case frequency:

    - ``user`` / ``system``: a plain text turn. ``content`` is the string.
    - ``assistant``: model output. When the assistant emitted tool calls,
      ``content`` is ``None`` and ``tool_calls`` carries the OpenAI-shape
      invocation list — this is what clients echo back into the next
      request to continue the agent loop.
    - ``tool``: a tool-result message paired with the prior assistant
      ``tool_calls`` entry by ``tool_call_id``. ``content`` is the JSON
      (or plain text) result the client computed for the tool.

    ``content`` must be permissive (`str | None`) so the assistant echo
    case validates; ``tool_call_id`` is required by OpenAI's spec for
    role=tool but optional everywhere else, so we accept it on every
    message and only enforce it via the upstream model's own validation.
    """

    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    # ``name`` was required for tool messages in the pre-2024 OpenAI
    # spec and is still accepted by every OpenAI-compat provider; we
    # pass it through if supplied so we don't break older client SDKs.
    name: str | None = None


class ChatCompletionBody(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=100_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # OpenAI-shape tool definitions; pronaos passes through to OpenAI-compat
    # providers verbatim and translates for Anthropic native. ``None`` (the
    # default) means "no tools, plain chat completion."
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


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


def get_circuit_registry(request: Request) -> CircuitBreakerRegistry:
    """Expose the app-scoped circuit breaker registry.

    Fail-soft: if startup didn't install one we return a fresh empty
    registry. Tests that bypass the lifespan get the right shape;
    production always has one wired in by ``main.create_app``."""
    registry: CircuitBreakerRegistry | None = getattr(
        request.app.state, "circuit_registry", None
    )
    if registry is None:
        registry = CircuitBreakerRegistry()
        request.app.state.circuit_registry = registry
    return registry


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


def get_guardrails(request: Request) -> GuardrailEngine:
    """Expose the app-scoped guardrail engine. Same null-fallback shape
    as ``get_cache`` so the handler stays guardrail-aware unconditionally."""
    engine: GuardrailEngine | None = getattr(request.app.state, "guardrails", None)
    if engine is None:
        return NullGuardrailEngine()
    return engine


def get_audit_logger(request: Request) -> AuditLogger:
    """Expose the app-scoped audit logger. Single shared instance is
    fine — the logger is stateless, all state lives in the DB."""
    logger: AuditLogger | None = getattr(request.app.state, "audit_logger", None)
    return logger if logger is not None else AuditLogger()


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionBody,
    response: Response,
    route: Annotated[Router, Depends(get_router)],
    principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
    quota: Annotated[QuotaTracker, Depends(get_quota_tracker)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cache: Annotated[Cache, Depends(get_cache)],
    guardrails: Annotated[GuardrailEngine, Depends(get_guardrails)],
    audit: Annotated[AuditLogger, Depends(get_audit_logger)],
    circuit_registry: Annotated[
        CircuitBreakerRegistry, Depends(get_circuit_registry)
    ],
) -> Any:
    # ---- Cost-aware auto-routing (Phase 21) ----------------------------
    # ``model="auto"`` is a sentinel that asks the gateway to pick the
    # cheapest (or fastest, or balanced) eligible model from the team's
    # allowlist that can satisfy the request. The scorer enforces the
    # allowlist *internally* via the candidate pool — so we don't need
    # to run the explicit allowlist gate below for an "auto" request.
    #
    # Resolution runs BEFORE the allowlist gate, BEFORE preflight, BEFORE
    # guardrails — because the picked model is what the rest of the
    # pipeline must use. Once resolved, ``body.model`` is rewritten to
    # the concrete fqmn and the request proceeds normally.
    if body.model == "auto":
        # Estimate tokens once for the scorer (used again by preflight
        # below — same heuristic, same numbers).
        estimated_total = estimate_tokens(
            [m.model_dump(exclude_none=True) for m in body.messages],
            max_completion_tokens=body.max_tokens,
        )
        estimated_output = body.max_tokens or DEFAULT_MAX_COMPLETION
        estimated_input = max(0, estimated_total - estimated_output)
        # Resolve strategy: explicit team setting wins, else cheapest.
        raw_strategy = principal.routing_strategy or RoutingStrategy.CHEAPEST.value
        try:
            strategy = RoutingStrategy(raw_strategy)
        except ValueError:
            # Stored value isn't recognised (older write, manual DB edit).
            # Fall back to cheapest rather than 500ing — operationally
            # safer to over-route to a cheap model than to fail.
            strategy = RoutingStrategy.CHEAPEST
        routing_req = RoutingRequest(
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
            requires_tools=body.tools is not None and len(body.tools) > 0,
            requires_streaming=body.stream,
        )
        try:
            selected = select_model(
                strategy=strategy,
                allowed_patterns=principal.allowed_models,
                request=routing_req,
            )
        except NoEligibleModelError as e:
            log.info(
                "routing.no_eligible_model",
                strategy=strategy.value,
                tenant=principal.tenant_name,
                team=principal.team_name,
                allowed_models=principal.allowed_models,
                reason=str(e),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "no_eligible_model",
                    "message": (
                        "no model in this team's allowlist can satisfy the "
                        "request's requirements; either widen the allowlist "
                        "or send a concrete provider/model instead of 'auto'"
                    ),
                    "strategy": strategy.value,
                },
            ) from e
        log.info(
            "routing.selected",
            strategy=strategy.value,
            selected_model=selected.fqmn,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
            tenant=principal.tenant_name,
            team=principal.team_name,
        )
        record_routing_decision(
            strategy=strategy.value, selected_model=selected.fqmn
        )
        # Rewrite body.model so the rest of the pipeline sees the
        # concrete model. Surface the decision in response headers so
        # clients can see what the gateway picked without parsing logs.
        body.model = selected.fqmn
        response.headers["X-Pronaos-Routed-Model"] = selected.fqmn
        response.headers["X-Pronaos-Routing-Strategy"] = strategy.value

    # ---- Model allowlist gate (Phase 17) -------------------------------
    # Enforced FIRST — before guardrails, cache, quota deduction, etc.
    # The pattern: cheap denials happen before any expensive work, both
    # to save compute and to keep the failure mode clean (no half-state
    # like "guardrails fired but model was denied anyway"). A 403 here
    # also doesn't count against the rate-limit budget downstream,
    # consistent with "this request never had a chance."
    # For auto-routed requests the scorer already enforced the allowlist
    # at candidate-selection time; this gate is a defense-in-depth check
    # that the rewritten body.model is still in the policy.
    if not is_model_allowed(body.model, principal.allowed_models):
        log.info(
            "model.denied",
            model=body.model,
            tenant=principal.tenant_name,
            team=principal.team_name,
            allowed_models=principal.allowed_models,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "type": "model_not_allowed",
                "model": body.model,
                "message": (
                    f"model {body.model!r} is not in this team's allowlist; "
                    "contact your tenant admin to update the policy"
                ),
            },
        )

    # ---- Pre-flight token-budget check (Phase 20) ---------------------
    # Estimate the total tokens this request will consume and reject
    # up-front if the team can't afford it. Saves the upstream call
    # cost on requests that would deny post-flight anyway. The
    # estimator is a heuristic (~±15%) — it's a guardrail, not a
    # billing oracle. The real cost is still enforced post-flight in
    # ``record_call`` after the actual prompt_tokens come back from
    # the provider.
    estimated_tokens = estimate_tokens(
        [m.model_dump(exclude_none=True) for m in body.messages],
        max_completion_tokens=body.max_tokens,
    )
    preflight = await quota.check_preflight(
        session, principal.team_id, estimated_tokens
    )
    if not preflight.allowed:
        reason = preflight.reason or "monthly_token_budget_exhausted"
        record_preflight_denial(reason)
        log.info(
            "preflight.denied",
            model=body.model,
            tenant=principal.tenant_name,
            team=principal.team_name,
            estimated_tokens=estimated_tokens,
            reason=reason,
        )
        # Surface the estimate in a response header so clients can
        # tell preflight denials apart from post-flight denials
        # (and decide whether to retry with smaller max_tokens).
        # NOTE: setting headers on ``response`` doesn't survive
        # ``raise HTTPException`` — FastAPI builds the error response
        # separately and discards mutations to the success-path
        # response object. Use the HTTPException(headers=...) kwarg.
        retry_after = int(preflight.retry_after_seconds or 0)
        error_headers = {"X-Pronaos-Preflight-Estimate": str(estimated_tokens)}
        if retry_after > 0:
            error_headers["Retry-After"] = str(retry_after)
        raise HTTPException(
            status_code=429,
            detail={
                "type": reason,
                "message": (
                    f"preflight estimate of {estimated_tokens} tokens would "
                    "exceed the team's remaining monthly budget"
                ),
                "estimated_tokens": estimated_tokens,
                "retry_after_seconds": retry_after or None,
            },
            headers=error_headers,
        )

    # ---- Ingress guardrails (Phase 8) ----------------------------------
    # Scan each user message before doing anything else. PII gets
    # redacted (so the cache key + provider call use the masked text);
    # prompt-injection patterns get logged. A BLOCK action short-circuits
    # to 422 before we touch the cache or provider.
    #
    # Phase 8.2: resolve the principal's per-team policy ONCE per request
    # and pass it to every scan call. Doing it once keeps the engine
    # stateless and avoids a per-message JSON parse.
    disabled_rules, policy_override = resolve_policy(principal.guardrail_policy)
    guardrail_summary: list[str] = []
    redacted_any = False
    for i, msg in enumerate(body.messages):
        if msg.role != "user":
            # System / assistant / tool-result turns are operator-supplied
            # or come from the client's own tool runtime — both trusted.
            # Scanning system messages risks redacting prompt templates that
            # include placeholder PII patterns; scanning tool-result content
            # would corrupt structured tool outputs the model expects.
            continue
        if msg.content is None:
            # Defensive: a user message with no content is malformed but
            # not worth crashing the request over. Skip the scan and let
            # the upstream provider's own validation reject it.
            continue
        verdict = guardrails.scan_ingress(
            msg.content,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )
        for hit in verdict.hits:
            # The action recorded in metrics is the policy outcome, not
            # the rule's default — that's the operationally relevant one.
            action_label = (
                "block" if verdict.blocked and hit.rule == verdict.block_reason
                else "redact" if redacted_any or msg.content != verdict.text
                else "log_only"
            )
            record_guardrail_hit(rule=hit.rule, action=action_label, direction="ingress")
        if verdict.blocked:
            response.headers["X-Pronaos-Guardrails"] = (
                f"blocked:{verdict.block_reason}"
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "guardrail_violation",
                    "rule": verdict.block_reason,
                    "message": "request blocked by guardrail policy",
                },
            )
        if verdict.text != msg.content:
            body.messages[i] = ChatMessage(role=msg.role, content=verdict.text)
            redacted_any = True
            guardrail_summary.extend({h.rule for h in verdict.hits})
    if redacted_any:
        response.headers["X-Pronaos-Guardrails"] = (
            "redacted:" + ",".join(sorted(set(guardrail_summary)))
        )

    # ---- Cache lookup (Phase 7) -----------------------------------------
    # Only deterministic, non-streaming requests are cache-eligible:
    # streaming defeats the purpose of token-by-token UX, and
    # temperature>0 is the user explicitly asking for variety. Both still
    # increment a ``skip`` metric so dashboards can show *why* hit-rate is
    # what it is.
    #
    # Agent-loop requests (anything containing a tool_call echo or
    # tool-result message) are also skipped. The L2 semantic cache
    # embeds only the user prompt — in a tool-result round trip, the
    # prompt is identical to turn 1 but the response MUST differ
    # (different context, different tool outputs). Bypassing the cache
    # here keeps correctness; multi-turn tool conversations are
    # naturally stateful and a cache hit would be a quiet correctness
    # bug, not a perf win.
    has_tool_turn = any(
        m.role == "tool" or m.tool_calls is not None for m in body.messages
    )
    cache_eligible = (
        not body.stream
        and (body.temperature is None or body.temperature == 0.0)
        and not has_tool_turn
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
        messages=[_dump_message(m) for m in body.messages],
        stream=body.stream,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        tools=body.tools,
        tool_choice=body.tool_choice,
    )
    plan = route.resolve(body.model)

    # Snapshot trip counts for every provider in the chain BEFORE
    # failover runs. If any breaker trips during the call, the diff
    # tells us exactly which provider transitioned (CLOSED→OPEN or
    # HALF_OPEN→OPEN). We publish a webhook for each such trip
    # AFTER the call completes — see _publish_circuit_trips below.
    # Keeps failover.py principal-agnostic; the webhook concern lives
    # here at the handler boundary.
    trip_snapshot: dict[str, int] = {
        prov.name: circuit_registry.get(prov.name).trip_count
        for prov in plan.chain()
    }

    # Time from BEFORE failover starts so the histogram includes any retry
    # cost from the failover layer — SREs and operators want to see the
    # whole upstream latency story, not just the winning provider's wire time.
    provider_call_start = time.monotonic()
    try:
        provider, stream = await execute_with_failover(
            plan, prov_req, circuit_registry=circuit_registry
        )
    finally:
        # Publish circuit-trip webhooks regardless of whether failover
        # succeeded or raised — a trip during a request that ultimately
        # 502'd is still operationally interesting.
        _publish_circuit_trips(
            request, principal, circuit_registry, trip_snapshot
        )

    log.info(
        "chat.request",
        provider=provider.name,
        model=body.model,
        tenant=principal.tenant_name,
        team=principal.team_name,
    )

    if body.stream:
        # Build the request_body snapshot HERE so the streaming generator
        # captures POST-ingress-redaction messages (body.messages was
        # mutated in-place above). The generator runs lazily as the
        # response streams, but the audit record needs the request shape
        # as it existed at handler entry.
        request_body_for_audit = {
            "model": body.model,
            "messages": [_dump_message(m) for m in body.messages],
            "temperature": body.temperature,
            "max_tokens": body.max_tokens,
        }
        # Pull the sessionmaker off app.state so the streaming generator
        # can open a fresh, short-lived session for cancellation
        # bookkeeping — the request's main session shares its
        # connection with the auth middleware and aiosqlite tears that
        # connection down when CancelledError propagates. A new
        # session gets a new connection from the pool and survives.
        sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
        return _handle_streaming(
            stream,
            provider,
            body.model,
            principal,
            quota,
            session,
            provider_call_start,
            guardrails=guardrails,
            audit=audit,
            request_body_for_audit=request_body_for_audit,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
            sessionmaker=sessionmaker,
        )
    response_body = await _handle_non_streaming(
        stream, provider, body.model, principal, quota, session, provider_call_start
    )
    # ---- Egress guardrails (Phase 8) ----------------------------------
    # Scan the assistant's response for PII leak-back. Models occasionally
    # regurgitate training-set strings, so this is a real protection
    # layer, not just symmetric paranoia. Egress can only REDACT — the
    # provider call already happened.
    _scan_response_egress(
        response_body,
        guardrails,
        policy_override=policy_override,
        disabled_rules=disabled_rules,
    )

    # ---- Cache write (Phase 7) ------------------------------------------
    # Only the deterministic path populates the cache. Fail-open: a cache
    # write failure is logged inside the backend, never raised here.
    # IMPORTANT: writes the POST-egress-scan response so cached responses
    # are also clean of PII leak-back.
    if cache_eligible:
        await cache.put(
            tenant_id=principal.tenant_id,
            model=body.model,
            key_payload=_canonical_cache_payload(body),
            response=response_body,
        )

    # ---- Audit log (Phase 10) ------------------------------------------
    # Tamper-evident hash chain. Stores hashes of (POST-redaction request,
    # POST-egress-redaction response) plus chain pointer. Fail-open:
    # audit write failure is logged inside the logger, returns None,
    # never raises to the client.
    await audit.append(
        session,
        tenant_id=principal.tenant_id,
        team_id=principal.team_id,
        key_id=principal.key_id,
        provider=response_body.get("pronaos", {}).get("provider", body.model.split("/", 1)[0]),
        model=body.model,
        request_body={
            "model": body.model,
            "messages": [_dump_message(m) for m in body.messages],
            "temperature": body.temperature,
            "max_tokens": body.max_tokens,
        },
        response_body=response_body,
        request_id=_current_request_id(),
    )

    return response_body


def _canonical_cache_payload(body: ChatCompletionBody) -> dict[str, Any]:
    """Strip the request down to the fields that affect the response.

    Anything else (stream flag is non-determinative after we've decided to
    cache; principal/auth info is in the key path) is excluded so cosmetic
    changes don't cache-miss against an otherwise identical request."""
    return {
        "messages": [_dump_message(m) for m in body.messages],
        "temperature": body.temperature or 0.0,
        "max_tokens": body.max_tokens,
    }


def _publish_circuit_trips(
    request: Request,
    principal: Principal,
    circuit_registry: CircuitBreakerRegistry,
    trip_snapshot: dict[str, int],
) -> None:
    """Compare current trip_count for each provider against the snapshot
    taken before failover ran. For each provider whose count increased,
    fire a ``circuit.tripped`` webhook event.

    Called from a ``finally`` so trips during a failing request still
    surface to the operator — a 502 that's the SYMPTOM of a circuit
    trip is exactly when you want the page to fire.

    Reads the dispatcher off ``app.state``; if it's not installed
    (test fixtures sometimes bypass the lifespan), the function is a
    no-op. Also a no-op if the tenant has no webhook configured.
    """
    dispatcher: WebhookDispatcher | None = getattr(
        request.app.state, "webhook_dispatcher", None
    )
    if dispatcher is None:
        return
    config = WebhookConfig(
        url=principal.webhook_url,
        secret=principal.webhook_secret,
    )
    for provider_name, before_count in trip_snapshot.items():
        breaker = circuit_registry.get(provider_name)
        if breaker.trip_count > before_count:
            dispatcher.publish(
                config,
                circuit_tripped_event(
                    tenant_id=principal.tenant_id,
                    provider=provider_name,
                    trip_count=breaker.trip_count,
                ),
            )


def _dump_message(m: ChatMessage) -> dict[str, Any]:
    """Serialise a ChatMessage for the provider wire / cache key / audit log.

    Strips ``None`` from optional fields (``tool_call_id``, ``tool_calls``,
    ``name``) so plain user/system turns produce the canonical
    ``{"role":..., "content":...}`` shape that providers and the cache
    fingerprint expect — but preserves ``content: None`` on the
    assistant-with-tool_calls echo case, because OpenAI's spec mandates
    the content key be present (null) when tool_calls is set, and some
    strict providers reject the message if it's missing entirely.
    """
    payload = m.model_dump(exclude_none=True)
    if m.role == "assistant" and m.tool_calls is not None and m.content is None:
        # Restore the null content slot the assistant-echo case requires.
        payload["content"] = None
    return payload


def _scan_response_egress(
    response_body: dict[str, Any],
    guardrails: GuardrailEngine,
    *,
    policy_override: dict[str, GuardrailAction] | None = None,
    disabled_rules: set[str] | None = None,
) -> None:
    """Scan + redact the assistant's content in-place on the response dict.

    Walks the OpenAI-shape ``choices[].message.content`` slots. If any
    egress rule fires the field is overwritten with the redacted text
    and a metric is incremented. Errors are swallowed by the engine
    (fail-open) and a guardrail bug must not break the response.

    ``policy_override`` and ``disabled_rules`` are passed through from
    the caller (chat handler), which resolves them once per request
    from the principal's team policy."""
    choices = response_body.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        verdict = guardrails.scan_egress(
            content,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )
        for hit in verdict.hits:
            # Egress can never BLOCK (engine downgrades to REDACT), so
            # the only meaningful actions here are redact / log_only.
            action_label = "redact" if verdict.text != content else "log_only"
            record_guardrail_hit(rule=hit.rule, action=action_label, direction="egress")
        if verdict.text != content:
            msg["content"] = verdict.text


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

    # Build assistant message. ``tool_calls`` is included when the model
    # emitted one (Anthropic ``tool_use`` block or OpenAI-compat
    # ``tool_calls`` field); OpenAI's spec sets ``content`` to null in
    # that case but it's also valid to ship the empty string. We choose
    # null when tool_calls are present so the wire shape matches the
    # OpenAI reference clients pin against.
    assistant_msg: dict[str, Any] = {"role": "assistant"}
    if chunk.tool_calls:
        assistant_msg["tool_calls"] = chunk.tool_calls
        assistant_msg["content"] = chunk.content_delta or None
        finish = chunk.finish_reason or "tool_calls"
    else:
        assistant_msg["content"] = chunk.content_delta
        finish = chunk.finish_reason or "stop"

    return {
        "id": _chat_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": assistant_msg,
                "finish_reason": finish,
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
    *,
    guardrails: GuardrailEngine,
    audit: AuditLogger,
    request_body_for_audit: dict[str, Any],
    policy_override: dict[str, GuardrailAction] | None = None,
    disabled_rules: set[str] | None = None,
    sessionmaker: Any = None,
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
        guardrails=guardrails,
        audit=audit,
        request_body_for_audit=request_body_for_audit,
        policy_override=policy_override,
        disabled_rules=disabled_rules,
        sessionmaker=sessionmaker,
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
    guardrails: GuardrailEngine,
    audit: AuditLogger,
    request_body_for_audit: dict[str, Any],
    policy_override: dict[str, GuardrailAction] | None = None,
    disabled_rules: set[str] | None = None,
    sessionmaker: Any = None,
) -> AsyncIterator[str]:
    request_id = _chat_id()
    created = int(time.time())
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason: str | None = None
    # Phase 11: accumulate the full assistant content as chunks arrive so we
    # can run egress guardrails + write audit at stream close. The client
    # has already received the raw chunks by the time we scan — this is
    # a known limitation documented in observability/README.md. The audit
    # record captures the POST-scan content; metrics + dashboards see the
    # rule firings. Real-time per-chunk redaction would risk PII spanning
    # chunk boundaries and adds non-trivial cost; the post-stream scan is
    # the honest middle ground for now.
    content_buffer: list[str] = []
    # Phase 12: streaming tools. Providers accumulate per-chunk tool_call
    # fragments internally and emit the assembled list on a single
    # "tail" ChatCompletionChunk. We capture it here and re-shape into
    # OpenAI streaming-tools format on the wire (one ``delta.tool_calls``
    # event per tool — index, id, name, full arguments string).
    accumulated_tool_calls: list[dict[str, Any]] | None = None

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
                content_buffer.append(chunk.content_delta)
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
            if chunk.tool_calls:
                # Providers may emit tool_calls on any chunk, but in our
                # current adapters they arrive once on the tail chunk
                # with everything accumulated. Either way, last-write-wins:
                # the tail chunk's tool_calls is authoritative.
                accumulated_tool_calls = chunk.tool_calls
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            if chunk.prompt_tokens is not None:
                prompt_tokens = chunk.prompt_tokens
            if chunk.completion_tokens is not None:
                completion_tokens = chunk.completion_tokens

        # After the provider stream finishes, emit one SSE event per tool
        # call with the assembled OpenAI streaming-tools shape. OpenAI's
        # reference format puts each tool call in its own delta event,
        # indexed; we follow that convention so client libraries pinned
        # to OpenAI handle Pronaos streams identically.
        if accumulated_tool_calls:
            for i, tc in enumerate(accumulated_tool_calls):
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": i,
                                            "id": tc.get("id"),
                                            "type": tc.get("type", "function"),
                                            "function": tc.get("function", {}),
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            # When tools fired, OpenAI uses ``finish_reason: "tool_calls"``.
            if finish_reason is None:
                finish_reason = "tool_calls"
    except asyncio.CancelledError:
        # Phase 18: client tore down the connection before the upstream
        # finished. CancelledError is a BaseException subclass so the
        # generic ``except Exception`` below would NOT catch it — handle
        # it explicitly here.
        #
        # What we DO on cancel:
        #   1. Tick the metric (process-local, always works)
        #   2. Emit a structured log line (always works)
        #   3. Re-raise so Starlette's response runner can do its own
        #      cleanup — swallowing CancelledError would orphan the
        #      task and confuse asyncio
        #
        # What we DON'T do on cancel:
        #   - DB-level bookkeeping (audit row, usage_record). The request's
        #     aiosqlite connection is torn down DURING cancellation
        #     propagation by SQLAlchemy's async cleanup hooks. Any
        #     subsequent DB op (even on a fresh session pulled from the
        #     same sessionmaker) hits "no active connection." Tried
        #     fixing this with asyncio.shield + fresh session and it
        #     STILL races with engine teardown. Future improvement:
        #     fire-and-forget the bookkeeping into a long-lived task
        #     bound to app.state with its own engine. Documented in
        #     observability/README.md.
        #
        # The COST-SAVING claim of cancellation propagation still holds:
        # httpx's ``async with self._http.stream(...)`` in the provider
        # adapter closes the upstream connection on cancellation
        # automatically, before this except even runs. That's the
        # actual savings — no more provider tokens get generated.
        record_stream_cancelled(provider=provider.name, model=model)
        log.info(
            "stream.cancelled",
            provider=provider.name,
            model=model,
            partial_completion_tokens=completion_tokens,
        )
        # Re-raise so Starlette knows the stream was aborted.
        raise
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

        await _record_stream_completion(
            content_buffer=content_buffer,
            accumulated_tool_calls=accumulated_tool_calls,
            finish_reason=finish_reason,
            provider=provider,
            model=model,
            principal=principal,
            quota=quota,
            session=session,
            audit=audit,
            request_body_for_audit=request_body_for_audit,
            request_id=request_id,
            created=created,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            guardrails=guardrails,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )

    # Final chunk: finish reason + usage. The ``usage`` field is an additive
    # extension (OpenAI emits it only when stream_options.include_usage=True);
    # we always emit so downstream FinOps has the number for free. Note this
    # only runs on clean completion (cancelled path re-raised above; error
    # path falls through with the original except).
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


async def _record_stream_completion(
    *,
    content_buffer: list[str],
    accumulated_tool_calls: list[dict[str, Any]] | None,
    finish_reason: str | None,
    provider: Provider,
    model: str,
    principal: Principal,
    quota: QuotaTracker,
    session: AsyncSession,
    audit: AuditLogger,
    request_body_for_audit: dict[str, Any],
    request_id: str,
    created: int,
    prompt_tokens: int,
    completion_tokens: int,
    guardrails: GuardrailEngine,
    policy_override: dict[str, GuardrailAction] | None,
    disabled_rules: set[str] | None,
) -> None:
    """Run the egress scan + audit write + usage_record persistence at
    the END of a streaming response.

    Called from both the clean-finish path and the cancelled path
    inside ``_sse_openai_chunks``. Factored out so the bookkeeping
    is identical regardless of HOW the stream ended — the only
    differences between a clean stop and a cancellation are:

    - ``finish_reason`` (caller passes "stop" / "tool_calls" / "cancelled")
    - The ``streams_cancelled_total`` metric is bumped only by the
      cancel branch (not this helper's responsibility)

    Everything else — egress scan, audit row, usage_record with partial
    tokens — runs uniformly. Cancelled streams that produced K tokens
    DO get billed for K, because the upstream actually generated them
    and we're going to be billed for them by the provider — the tenant
    should see the cost too.
    """
    # Egress scan over whatever was streamed so far.
    full_content = "".join(content_buffer)
    if full_content:
        egress_verdict = guardrails.scan_egress(
            full_content,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )
        for hit in egress_verdict.hits:
            action_label = (
                "redact" if egress_verdict.text != full_content else "log_only"
            )
            record_guardrail_hit(rule=hit.rule, action=action_label, direction="egress")
        audited_content = egress_verdict.text
    else:
        audited_content = ""

    # OpenAI-shape body for the audit row.
    assistant_msg: dict[str, Any] = {"role": "assistant"}
    if accumulated_tool_calls:
        assistant_msg["tool_calls"] = accumulated_tool_calls
        assistant_msg["content"] = audited_content or None
    else:
        assistant_msg["content"] = audited_content
    audited_response_body = {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": assistant_msg,
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": _usage(prompt_tokens, completion_tokens),
        "pronaos": {
            "provider": provider.name,
            "cost_hcents": provider.cost_cents(prompt_tokens, completion_tokens, model),
            "streamed": True,
        },
    }

    # Fail-open audit write — same semantics as the non-streaming path.
    await audit.append(
        session,
        tenant_id=principal.tenant_id,
        team_id=principal.team_id,
        key_id=principal.key_id,
        provider=provider.name,
        model=model,
        request_body=request_body_for_audit,
        response_body=audited_response_body,
        request_id=_current_request_id(),
    )

    # Persist a usage_record + bump the team budget. Best-effort;
    # failures logged inside the tracker, never raised.
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

    # If this completion fired on the cancelled path, the calling
    # handler is about to re-raise CancelledError out to Starlette —
    # which will trigger ``get_db``'s rollback branch and undo the
    # audit + usage_record writes we just made. Commit explicitly
    # before that happens, so the bookkeeping survives cancellation.
    # On the clean-finish path this commit is a no-op (the dependency
    # commits at the end anyway), but doing it here is harmless.
    if finish_reason == "cancelled":
        try:
            await session.commit()
        except Exception as e:
            # If commit itself fails, log and move on; the alternative
            # (raise) would mask the original CancelledError the caller
            # is about to re-raise. Lost audit on a DB error is the
            # lesser evil here.
            log.warning(
                "stream.cancelled.commit_failed",
                error=str(e),
                provider=provider.name,
            )
