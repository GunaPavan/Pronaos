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
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.audit.logger import AuditLogger
from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import enforce_quotas, get_db, get_quota_tracker
from pronaos.cache.base import Cache
from pronaos.config import get_settings
from pronaos.core.circuit import CircuitBreakerRegistry
from pronaos.core.failover import execute_with_failover, hedge_outcome_var
from pronaos.core.model_access import is_model_allowed
from pronaos.core.multimodal import estimate_image_tokens, inventory_images
from pronaos.core.pii_tokens import (
    DEFAULT_TTL_SECONDS as PII_TOKEN_DEFAULT_TTL,
)
from pronaos.core.pii_tokens import (
    StreamingDetokenizer,
    TokenStore,
)
from pronaos.core.quality_monitor import (
    check_degradation,
    judge_response,
    record_sample,
)
from pronaos.core.quota import CompletedCall, QuotaTracker
from pronaos.core.router import Router
from pronaos.core.scorer import (
    NoEligibleModelError,
    RoutingRequest,
    RoutingStrategy,
    select_model,
)
from pronaos.core.structured_output import (
    build_correction_messages,
    build_schema_system_message,
    extract_schema,
    validate_response_content,
)
from pronaos.core.token_estimator import DEFAULT_MAX_COMPLETION, estimate_tokens
from pronaos.core.tool_budgets import (
    strip_over_budget_tools,
    tool_names_from_calls,
)
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
from pronaos.guardrails.llama_guard import (
    is_llama_guard_enabled_for_team,
    llama_guard_team_action,
)
from pronaos.guardrails.policy import resolve_policy
from pronaos.logging import get_logger
from pronaos.mcp.client_federation import open_federation
from pronaos.observability.metrics import (
    record_ab_decision,
    record_agent_turn_denial,
    record_cache_lookup,
    record_cache_stream_replay,
    record_guardrail_hit,
    record_image_input,
    record_image_rejection,
    record_mcp_federated_tool_call,
    record_mcp_federation_session,
    record_mcp_streaming_federation_session,
    record_pii_token_created,
    record_pii_token_orphaned,
    record_pii_token_reversed,
    record_preflight_denial,
    record_prompt_cache_tokens,
    record_provider_error,
    record_provider_success,
    record_quality_degradation,
    record_quality_sample,
    record_reasoning_tokens,
    record_routing_decision,
    record_schema_retry,
    record_schema_validation,
    record_stream_cancelled,
    record_tool_call_emitted,
    record_tool_call_stripped,
    record_tool_result_cache,
)
from pronaos.observability.otel import get_tracer
from pronaos.observability.otel_gen_ai import (
    GEN_AI_OPERATION_CHAT,
    apply_gen_ai_request_attrs,
    apply_gen_ai_response_attrs,
    gen_ai_system_for,
    span_name_for,
)
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

    - ``user`` / ``system``: a plain text turn. ``content`` is the string,
      OR a list of multi-modal parts (Phase 41) when the request includes
      images: ``[{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"..."}}]``
    - ``assistant``: model output. When the assistant emitted tool calls,
      ``content`` is ``None`` and ``tool_calls`` carries the OpenAI-shape
      invocation list — this is what clients echo back into the next
      request to continue the agent loop.
    - ``tool``: a tool-result message paired with the prior assistant
      ``tool_calls`` entry by ``tool_call_id``. ``content`` is the JSON
      (or plain text) result the client computed for the tool.

    ``content`` is permissive (``str | list[dict] | None``):
    - ``str`` for the common single-text-turn case (backward compat).
    - ``list[dict]`` for multi-modal turns (text + images).
    - ``None`` for the assistant-tool-call echo case.

    ``tool_call_id`` is required by OpenAI's spec for role=tool but
    optional everywhere else, so we accept it on every message and only
    enforce it via the upstream model's own validation.
    """

    role: str
    # Phase 41: relaxed to allow list[dict] for multi-modal turns.
    # The chat handler treats list content as opaque on the wire path
    # (pass-through to OpenAI-compat / translate for Anthropic). For
    # guardrails / cache-key derivation we extract the text parts.
    content: str | list[dict[str, Any]] | None = None
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
    # Phase 39 — OpenAI-shape response_format. When type=json_schema, the
    # gateway extracts the inner schema, forwards it to the upstream via
    # native structured-output if supported, and validates the response
    # against the schema with auto-retry on violation. Pass-through for
    # other shapes (json_object / text) — we don't validate without a
    # schema; that's the client's responsibility.
    response_format: dict[str, Any] | None = None
    # Phase 54 — MCP client federation. List of external MCP servers
    # whose tools should be federated into this chat completion. When
    # non-empty AND the team's ``mcp_client_enabled`` flag is true, the
    # gateway opens connections to each server (stdio transport in v1),
    # discovers their tools, namespace-prefixes them as
    # ``{name}.{tool_name}``, augments the LLM's tools array with them,
    # and routes any tool_calls back through the right server in a
    # bounded multi-turn loop. Each entry is ``{name, command, args,
    # env}``. Off-by-default: teams without the per-team flag get a
    # 422 with ``mcp_client_disabled`` detail.
    pronaos_mcp_servers: list[dict[str, Any]] | None = None


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
    registry: CircuitBreakerRegistry | None = getattr(request.app.state, "circuit_registry", None)
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
    circuit_registry: Annotated[CircuitBreakerRegistry, Depends(get_circuit_registry)],
) -> Any:
    # ---- MCP client federation (Phase 54) ------------------------------
    # If the request references external MCP servers, dispatch to the
    # federation wrapper. The wrapper drives a multi-turn loop:
    # discovers each server's tools, augments body.tools, fires the
    # request back at THIS endpoint with the federation field stripped
    # (no recursion — inner calls have body.pronaos_mcp_servers=None),
    # routes any federated tool_calls in the response back through the
    # right server, loops until no federated tool_calls remain or the
    # max iteration cap is hit. v1 ships non-streaming only; a request
    # with both ``stream=true`` AND ``pronaos_mcp_servers`` is rejected.
    if body.pronaos_mcp_servers:
        if not principal.mcp_client_enabled:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "mcp_client_disabled",
                    "hint": (
                        "this team is not enabled for MCP client "
                        "federation; ask an admin to set "
                        "mcp_client_enabled=true on the team or remove "
                        "the pronaos_mcp_servers field from the request"
                    ),
                },
            )
        if body.stream:
            # Phase 58: streaming MCP federation. The non-streaming
            # federation loop runs end-to-end + its final response is
            # delivered as a synthesized SSE stream. See
            # ``_run_mcp_streaming_federation`` for the design + honest
            # limit (TTFT equals full-loop latency, not first-token).
            return await _run_mcp_streaming_federation(
                request=request,
                body=body,
                principal=principal,
            )
        return await _run_mcp_federation_loop(
            request=request,
            body=body,
            response=response,
            principal=principal,
        )

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
        # Phase 40: build the degraded set from the team's monitor
        # state. The scorer filters degraded fqmns out of the
        # candidate pool regardless of strategy — "model is broken"
        # is orthogonal to the team's pricing preference.
        from pronaos.core.quality_monitor import degraded_models as _degraded_models

        currently_degraded = set(_degraded_models(principal.model_degradation_state))
        # Phase 47: snapshot the team's prompt-cache observations once at
        # routing time. The scorer only consults this on
        # ``prompt-cache-aware-cheapest``; for other strategies the dict
        # is built but unused (cheap — one Redis HGETALL or empty dict).
        prompt_cache_observations: dict[str, dict[str, int]] = {}
        if strategy == RoutingStrategy.PROMPT_CACHE_AWARE_CHEAPEST:
            observer = getattr(request.app.state, "prompt_cache_observer", None)
            if observer is not None:
                raw_snapshot = await observer.snapshot(principal.team_id)
                # Map PromptCacheStat → plain dict for the scorer (which
                # is intentionally decoupled from the observer's type).
                prompt_cache_observations = {
                    fqmn: {
                        "n_samples": stat.n_samples,
                        "prompt_tokens": stat.prompt_tokens,
                        "cached_tokens": stat.cached_tokens,
                    }
                    for fqmn, stat in raw_snapshot.items()
                }
        # Phase 57: same shape as prompt-cache snapshot above. Only
        # consumed by ``reasoning-aware-cheapest``; other strategies see
        # the empty dict and pay no extra Redis cost.
        reasoning_observations: dict[str, dict[str, int]] = {}
        if strategy == RoutingStrategy.REASONING_AWARE_CHEAPEST:
            r_observer = getattr(request.app.state, "reasoning_observer", None)
            if r_observer is not None:
                r_raw = await r_observer.snapshot(principal.team_id)
                reasoning_observations = {
                    fqmn: {
                        "n_samples": stat.n_samples,
                        "completion_tokens": stat.completion_tokens,
                        "reasoning_tokens": stat.reasoning_tokens,
                    }
                    for fqmn, stat in r_raw.items()
                }
        try:
            selected = select_model(
                strategy=strategy,
                allowed_patterns=principal.allowed_models,
                request=routing_req,
                # Phase 24: pass the team's quality data; the scorer
                # uses it only when ``strategy == QUALITY_AWARE_CHEAPEST``.
                # For other strategies these are ignored — no behavioural
                # change for teams that don't opt in to quality routing.
                quality_scores=principal.quality_scores,
                quality_threshold=principal.quality_threshold,
                degraded_models_set=currently_degraded or None,
                # Phase 46: tool-use-aware-cheapest reads these. Same
                # opt-in semantics — ignored for other strategies. The
                # filter only applies when the request carries tools.
                tool_use_scores=principal.tool_use_scores,
                tool_use_threshold=principal.tool_use_threshold,
                # Phase 47: prompt-cache-aware-cheapest reads these.
                # Same opt-in semantics — empty dict + None thresholds
                # → no-op for all other strategies.
                prompt_cache_observations=prompt_cache_observations,
                prompt_cache_min_samples=principal.prompt_cache_min_samples,
                prompt_cache_min_hit_rate=principal.prompt_cache_min_hit_rate,
                # Phase 57: reasoning-aware-cheapest reads these. Same
                # opt-in semantics — empty dict + None values = no-op
                # for all other strategies.
                reasoning_observations=reasoning_observations,
                reasoning_min_samples=principal.reasoning_aware_min_samples,
                reasoning_max_ratio=principal.reasoning_aware_max_ratio,
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
        record_routing_decision(strategy=strategy.value, selected_model=selected.fqmn)
        # Rewrite body.model so the rest of the pipeline sees the
        # concrete model. Surface the decision in response headers so
        # clients can see what the gateway picked without parsing logs.
        body.model = selected.fqmn
        response.headers["X-Pronaos-Routed-Model"] = selected.fqmn
        response.headers["X-Pronaos-Routing-Strategy"] = strategy.value
        # Phase 40: surface which models were excluded by the quality
        # monitor on this decision. Header is omitted when nothing was
        # excluded — keeps the response clean for healthy fleets.
        if currently_degraded:
            response.headers["X-Pronaos-Routing-Excluded-Models"] = ",".join(
                sorted(currently_degraded)
            )
        # Phase 24: when the team has a stored quality score for the
        # selected model, surface it so clients can audit the
        # quality-aware decision without round-tripping to the eval
        # store. Absent header = no score on record for this model.
        if principal.quality_scores:
            entry = principal.quality_scores.get(selected.fqmn)
            if isinstance(entry, dict):
                stored_score = entry.get("score")
                if isinstance(stored_score, int | float):
                    response.headers["X-Pronaos-Quality-Score"] = f"{float(stored_score):.3f}"

    # ---- A/B test substitution (Phase 29) -------------------------------
    # When the team has an active A/B test and the request's model
    # (after auto-routing if applicable) matches one of the arms, the
    # gateway substitutes per a deterministic-by-request_id bucket.
    # Retries of the same logical request land in the same arm so
    # per-arm attribution stays clean.
    #
    # A/B substitution runs BEFORE the allowlist gate so the substituted
    # arm still has to clear the allowlist (defence in depth — operators
    # can't accidentally route around the allowlist via an A/B test).
    ab_arm: str | None = None
    if principal.ab_test is not None:
        from pronaos.core.abtest import parse_ab_test, resolve_arm, should_apply

        parsed_ab = parse_ab_test(principal.ab_test)
        if parsed_ab is not None and should_apply(parsed_ab, body.model):
            req_id = _current_request_id() or "unknown"
            ab_arm, ab_model = resolve_arm(
                test=parsed_ab,
                team_id=principal.team_id,
                request_id=req_id,
            )
            if ab_model != body.model:
                log.info(
                    "abtest.substitute",
                    test_id=parsed_ab.id,
                    test_name=parsed_ab.name,
                    arm=ab_arm,
                    from_model=body.model,
                    to_model=ab_model,
                )
                body.model = ab_model
            # Surface decisions on the wire so clients can audit which
            # arm served their request without round-tripping to the
            # admin API.
            response.headers["X-Pronaos-AB-Test"] = parsed_ab.id
            response.headers["X-Pronaos-AB-Arm"] = ab_arm
            response.headers["X-Pronaos-AB-Model"] = ab_model
            record_ab_decision(test_id=parsed_ab.id, arm=ab_arm)

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

    # ---- Multi-modal image inventory + size cap (Phase 41) ------------
    # Walk the request once to extract image parts. The result feeds:
    # 1. The per-team byte-size cap (reject 422 before upstream call).
    # 2. The preflight token estimator (image tokens count toward budget).
    # 3. The X-Pronaos-Image-Tokens response header.
    # 4. The metrics (counted per provider/model when the request succeeds).
    #
    # Inventory is cheap: list walk + base64 length math, no decoding.
    # The function tolerates both OpenAI shape (image_url) and
    # Anthropic-native shape (image with source) so clients that
    # already speak Anthropic don't surprise the gateway.
    image_inventory = inventory_images([_dump_message(m) for m in body.messages])
    if image_inventory.parts:
        cap = principal.max_image_bytes
        if cap is not None and image_inventory.total_base64_bytes > cap:
            record_image_rejection(reason="too_large")
            log.info(
                "multimodal.image_too_large",
                tenant=principal.tenant_name,
                team=principal.team_name,
                bytes=image_inventory.total_base64_bytes,
                cap=cap,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "image_too_large",
                    "message": (
                        f"total base64 image payload "
                        f"({image_inventory.total_base64_bytes} bytes) exceeds "
                        f"this team's max_image_bytes ({cap}); resize images or "
                        "raise the cap via admin"
                    ),
                    "image_bytes": image_inventory.total_base64_bytes,
                    "cap": cap,
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
    preflight = await quota.check_preflight(session, principal.team_id, estimated_tokens)
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

    # ---- Agent-turn budget gate (Phase 30) -----------------------------
    # Clients building tool-using agent loops pass the same
    # ``X-Pronaos-Agent-Turn-ID`` header on every call belonging to
    # one logical turn. The gateway accumulates running token + cost
    # totals per turn-id in Redis and denies the call that would
    # push the team over either ``agent_turn_budget_tokens`` or
    # ``agent_turn_budget_cost_hcents``. Saves the team's monthly
    # budget from being burned in one runaway loop.
    #
    # Header absent or team has no budgets set → gate is a no-op
    # (existing behaviour preserved for clients not using the
    # feature). Redis outage → gate fails open (logged, allowed).
    agent_turn_id: str | None = request.headers.get("X-Pronaos-Agent-Turn-ID") or None
    agent_turn_tracker = getattr(request.app.state, "agent_turn_tracker", None)
    if (
        agent_turn_id
        and agent_turn_tracker is not None
        and (
            principal.agent_turn_budget_tokens is not None
            or principal.agent_turn_budget_cost_hcents is not None
        )
    ):
        decision = await agent_turn_tracker.check(
            team_id=principal.team_id,
            turn_id=agent_turn_id,
            budget_tokens=principal.agent_turn_budget_tokens,
            budget_cost_hcents=principal.agent_turn_budget_cost_hcents,
            next_estimate_tokens=estimated_tokens,
            next_estimate_cost_hcents=0,
        )
        if not decision.allowed:
            record_agent_turn_denial(reason=decision.reason or "agent_turn_budget_exhausted")
            log.info(
                "agent_turn.denied",
                team=principal.team_name,
                turn_id=agent_turn_id,
                reason=decision.reason,
                used_tokens=decision.used_tokens,
                used_calls=decision.used_calls,
            )
            err_headers = {
                "X-Pronaos-Agent-Turn-ID": agent_turn_id,
                "X-Pronaos-Agent-Turn-Used-Tokens": str(decision.used_tokens),
                "X-Pronaos-Agent-Turn-Used-Cost-Hcents": str(decision.used_cost_hcents),
                "X-Pronaos-Agent-Turn-Calls": str(decision.used_calls),
            }
            if decision.remaining_tokens is not None:
                err_headers["X-Pronaos-Agent-Turn-Remaining-Tokens"] = str(
                    decision.remaining_tokens
                )
            if decision.remaining_cost_hcents is not None:
                err_headers["X-Pronaos-Agent-Turn-Remaining-Cost-Hcents"] = str(
                    decision.remaining_cost_hcents
                )
            raise HTTPException(
                status_code=429,
                detail={
                    "type": decision.reason or "agent_turn_budget_exhausted",
                    "message": (
                        "this call would push the agent-turn over the team's "
                        "per-execution budget; start a new turn or raise the "
                        "team's agent_turn_budget_* limits"
                    ),
                    "agent_turn_id": agent_turn_id,
                    "used_tokens": decision.used_tokens,
                    "used_cost_hcents": decision.used_cost_hcents,
                    "used_calls": decision.used_calls,
                    "remaining_tokens": decision.remaining_tokens,
                    "remaining_cost_hcents": decision.remaining_cost_hcents,
                },
                headers=err_headers,
            )
        # Allowed — surface remaining-budget headers on the success
        # response so clients can plan the next call without
        # re-deriving them.
        if decision.remaining_tokens is not None:
            response.headers["X-Pronaos-Agent-Turn-Remaining-Tokens"] = str(
                decision.remaining_tokens
            )
        if decision.remaining_cost_hcents is not None:
            response.headers["X-Pronaos-Agent-Turn-Remaining-Cost-Hcents"] = str(
                decision.remaining_cost_hcents
            )
        response.headers["X-Pronaos-Agent-Turn-Calls"] = str(decision.used_calls)

    # ---- Per-tool budget enforcement (Phase 37) ------------------------
    # Strip-by-removal: for any tool whose ``current_calls >= limit_calls``
    # on the team's ``tool_budgets`` JSON, remove it from ``body.tools``
    # BEFORE forwarding to the upstream provider. The LLM never sees the
    # tool, never attempts to call it, never wastes reasoning on a
    # budget-exhausted operation. This is strictly better than a
    # post-emission "deny the tool call" approach — the LLM might still
    # have produced text output we'd then have to discard, billing the
    # team for a wasted call.
    #
    # Headers: ``X-Pronaos-Tool-Stripped`` lists the stripped tool names
    # (comma-separated). Clients use this to surface "tool X is over
    # budget this month" in their UX without re-querying admin/usage.
    # Absent header = nothing stripped, normal forwarding.
    if body.tools and principal.tool_budgets:
        new_tools, stripped_names = strip_over_budget_tools(body.tools, principal.tool_budgets)
        if stripped_names:
            for sname in stripped_names:
                record_tool_call_stripped(tool_name=sname)
            log.info(
                "tool_budget.stripped",
                tenant=principal.tenant_name,
                team=principal.team_name,
                stripped=stripped_names,
            )
            response.headers["X-Pronaos-Tool-Stripped"] = ",".join(stripped_names)
            # Empty list after stripping means the request asked for ONLY
            # tools that are all over budget. Pass an empty list to the
            # upstream — most providers treat that the same as "no tools"
            # (the LLM will respond with plain text). Setting to None
            # would lose the "client explicitly wanted tools" signal,
            # which can matter for provider-side validation.
            body.tools = new_tools

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

    # Phase 44 — Llama Guard ML jailbreak / unsafe-content pre-check.
    # Runs BEFORE the regex/Presidio guardrails because it's a separate
    # async network call to the classifier. Only fires when (a) the
    # operator opted in (``llama_guard`` on app.state), and (b) the
    # team's guardrail_policy has ``llama_guard.enabled = true``. On
    # ``unsafe`` + BLOCK action the request short-circuits to 422 with
    # the firing category as the reason — same shape as a regex
    # injection BLOCK. On LOG_ONLY the request continues; categories
    # are recorded as guardrail hits + metrics.
    llama_guard_classifier = getattr(request.app.state, "llama_guard", None)
    if llama_guard_classifier is not None and is_llama_guard_enabled_for_team(
        principal.guardrail_policy
    ):
        lg_prompt = "\n".join(
            _flatten_text_content(m.content)
            for m in body.messages
            if m.role == "user" and m.content is not None
        )
        if lg_prompt:
            lg_verdict = await llama_guard_classifier.classify(lg_prompt)
            if not lg_verdict.safe:
                lg_action = llama_guard_team_action(
                    principal.guardrail_policy,
                    fallback=llama_guard_classifier.default_action,
                )
                # Surface a hit per category for metrics + audit. The
                # rule name is the dotted category form
                # (``llama_guard.violent_crimes``) so dashboards can
                # break out hits by category.
                for rule_name in lg_verdict.rule_names:
                    record_guardrail_hit(rule=rule_name, action=str(lg_action), direction="ingress")
                if lg_action == GuardrailAction.BLOCK:
                    # 422 mirrors a regex/Presidio BLOCK exactly.
                    first_rule = (
                        lg_verdict.rule_names[0] if lg_verdict.rule_names else "llama_guard"
                    )
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "type": "guardrail_blocked",
                            "rule": first_rule,
                            "categories": list(lg_verdict.categories),
                            "message": (
                                "Llama Guard flagged the prompt as unsafe under "
                                f"category {first_rule}; team policy is BLOCK."
                            ),
                        },
                    )
                # LOG_ONLY (or any non-block action) — continue but mark
                # the response so audit + observability see it.
    # Phase 38: tokenization is active only if (a) the team has the
    # flag flipped AND (b) Redis is available (no store -> can't
    # reverse later -> safer to redact one-way). The engine respects
    # the flag and silently degrades TOKENIZE → REDACT when off.
    pii_token_store: TokenStore | None = getattr(request.app.state, "pii_token_store", None)
    tokenization_active = bool(principal.pii_tokenization_enabled and pii_token_store is not None)
    pii_token_ttl = principal.pii_token_ttl_seconds or PII_TOKEN_DEFAULT_TTL
    guardrail_summary: list[str] = []
    redacted_any = False
    tokenized_any = False
    pending_tokenizations: list[tuple[str, str]] = []
    pending_token_rules: list[str] = []  # rule short suffix per token, for metrics
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
        # Phase 41: for multi-modal content (list of parts), scan only
        # the TEXT parts. Image bytes don't carry PII the regex
        # detectors can read, and they'd false-positive on random
        # base64 chunks. We rejoin the text parts with newlines so
        # cross-part references (rare but possible) still trip
        # detectors that need both halves of a value.
        scan_text = _flatten_text_content(msg.content)
        if not scan_text:
            # Image-only message — nothing for the text scanner to do.
            continue
        verdict = guardrails.scan_ingress(
            scan_text,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
            tenant_id=principal.tenant_id,
            tokenization_enabled=tokenization_active,
        )
        for hit in verdict.hits:
            # The action recorded in metrics is the policy outcome, not
            # the rule's default — that's the operationally relevant one.
            action_label = (
                "block"
                if verdict.blocked and hit.rule == verdict.block_reason
                else "redact"
                if redacted_any or scan_text != verdict.text
                else "log_only"
            )
            record_guardrail_hit(rule=hit.rule, action=action_label, direction="ingress")
        if verdict.blocked:
            response.headers["X-Pronaos-Guardrails"] = f"blocked:{verdict.block_reason}"
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "guardrail_violation",
                    "rule": verdict.block_reason,
                    "message": "request blocked by guardrail policy",
                },
            )
        if verdict.text != scan_text:
            # Phase 41: rebuild the message content preserving image
            # parts. For plain string content we replace the whole
            # thing; for multi-modal we splice the redacted text into
            # the text parts (one combined text part — collapsing is
            # the simpler semantic and matches what humans expect on
            # the UI side).
            new_content = _replace_text_in_content(msg.content, verdict.text)
            body.messages[i] = ChatMessage(role=msg.role, content=new_content)
            # Phase 38: ``redacted_any`` should ONLY fire when the
            # change came from a one-way REDACT, not from TOKENIZE.
            # If the verdict has tokenizations AND no rule went
            # through the redact path (no replacement-token markers
            # in the output), treat this as a pure tokenize change.
            # When BOTH happened on different rules within the same
            # message, both markers fire. We detect the redact path
            # by checking for the rule's replacement token literal in
            # the post-tokenize text.
            tokenized_text_values = {tok for tok, _val in verdict.tokenizations}
            had_pure_redact = any(
                h.replacement_token in verdict.text
                and h.replacement_token not in tokenized_text_values
                for h in verdict.hits
            )
            if had_pure_redact or not verdict.tokenizations:
                redacted_any = True
                guardrail_summary.extend({h.rule for h in verdict.hits})
        # Phase 38: collect tokenization mappings from this message.
        # Each (token, original) pair will be written to Redis below
        # in a single pipeline. Per-rule metric labels come from the
        # token shape itself (``[EMAIL_...]`` -> ``email``).
        if verdict.tokenizations:
            tokenized_any = True
            pending_tokenizations.extend(verdict.tokenizations)
            for token, _value in verdict.tokenizations:
                # Token shape ``[TYPE_HASH]`` — extract TYPE, lowercase
                # for the metric label.
                if token.startswith("[") and "_" in token:
                    rule_suffix = token[1:].split("_", 1)[0].lower()
                else:
                    rule_suffix = "unknown"
                pending_token_rules.append(rule_suffix)

    # Phase 38: persist (token -> original) mappings to Redis so the
    # egress detokenizer can reverse them when the upstream responds.
    # Failure is logged inside store_many and returns the partial count;
    # we do NOT fall back to redaction on partial write — the verdict
    # text already carries the tokens (engine substituted in-place), and
    # any unreversible token in the response will surface as orphaned
    # via the egress metric. That's the lesser evil vs trying to roll
    # back to plaintext at this point.
    if tokenized_any and pii_token_store is not None and pending_tokenizations:
        # Deduplicate: same value across multiple messages produces the
        # same token (determinism). One Redis write per unique token.
        unique_mappings = list(dict(pending_tokenizations).items())
        await pii_token_store.store_many(
            tenant_id=principal.tenant_id,
            mappings=unique_mappings,
            ttl_seconds=pii_token_ttl,
        )
        # Per-rule metric increments — count UNIQUE tokens minted.
        unique_rules: dict[str, int] = {}
        seen_tokens: set[str] = set()
        for token, rule_suffix in zip(
            (t for t, _ in pending_tokenizations), pending_token_rules, strict=False
        ):
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            unique_rules[rule_suffix] = unique_rules.get(rule_suffix, 0) + 1
        for rule_suffix, count in unique_rules.items():
            record_pii_token_created(rule=rule_suffix, count=count)
    if redacted_any:
        response.headers["X-Pronaos-Guardrails"] = "redacted:" + ",".join(
            sorted(set(guardrail_summary))
        )
    if tokenized_any:
        # Distinct from "redacted:" so dashboards can split one-way
        # redactions from reversible tokenizations.
        existing = response.headers.get("X-Pronaos-Guardrails", "")
        marker = "tokenized:" + ",".join(sorted(set(pending_token_rules)))
        response.headers["X-Pronaos-Guardrails"] = f"{existing};{marker}" if existing else marker

    # ---- Tool-result cache (Phase 49) -----------------------------------
    # When the team has opted in, the gateway memoizes
    # (tool_name, args) → result extracted from past ``tool`` role
    # messages, and injects cached results into requests whose
    # trailing ``assistant.tool_calls`` are awaiting execution.
    #
    # Two-direction wiring:
    #   (a) RECORD: every ``tool`` role message in the inbound
    #       request gets its (name, args, content) tuple stamped
    #       into Redis, indexed by team_id. The result becomes
    #       available to future requests.
    #   (b) INJECT: if the last assistant message has
    #       ``tool_calls`` AND there's no matching ``tool`` follow-up
    #       for some of those tool_call_ids, the gateway looks up
    #       each pending call in the cache. On hit, a synthetic
    #       ``tool`` message is appended to the conversation before
    #       the upstream call — the client's tool re-execution is
    #       skipped.
    #
    # The X-Pronaos-Tool-Cache-Hits header surfaces the count of
    # injected results so clients can audit what came from cache.
    # The feature is fail-open: any Redis hiccup degrades to plain
    # passthrough (cached + uncached requests differ only in whether
    # the LLM saw the injected tool result).
    tool_cache_hits = 0
    if principal.tool_result_cache_enabled:
        trc = getattr(request.app.state, "tool_result_cache", None)
        if trc is not None:
            trc_ttl = (
                principal.tool_result_cache_ttl_seconds
                if principal.tool_result_cache_ttl_seconds is not None
                else 3600
            )
            # ---- (a) RECORD pass: scan for (assistant.tool_calls,
            # tool: result) pairs and persist them.
            # The OpenAI wire shape pairs them by ``tool_call_id``;
            # we walk the message list once to build a map.
            tool_calls_by_id: dict[str, dict[str, Any]] = {}
            for m in body.messages:
                if m.role == "assistant" and m.tool_calls:
                    for tc in m.tool_calls:
                        tc_id = tc.get("id")
                        if isinstance(tc_id, str):
                            tool_calls_by_id[tc_id] = tc
            for m in body.messages:
                if m.role != "tool":
                    continue
                if not m.tool_call_id or not isinstance(m.content, str):
                    continue
                matched_tc = tool_calls_by_id.get(m.tool_call_id)
                if matched_tc is None:
                    continue
                fn = matched_tc.get("function") or {}
                tool_name = fn.get("name")
                args = fn.get("arguments")
                if not isinstance(tool_name, str) or args is None:
                    continue
                try:
                    await trc.record(
                        team_id=principal.team_id,
                        tool_name=tool_name,
                        args=args,
                        result=m.content,
                        ttl_seconds=trc_ttl,
                    )
                except Exception as e:  # observer is fail-open
                    log.warning(
                        "tool_result_cache.record_failed",
                        error=str(e),
                        tool_name=tool_name,
                    )
            # ---- (b) INJECT pass: find the trailing
            # ``assistant.tool_calls`` with no matching ``tool``
            # follow-up; look up each pending call; append synthetic
            # ``tool`` messages on hit.
            fulfilled_call_ids: set[str] = set()
            for m in body.messages:
                if m.role == "tool" and m.tool_call_id:
                    fulfilled_call_ids.add(m.tool_call_id)
            # Find the LAST assistant message with tool_calls — it's
            # the only one whose results could still be pending.
            last_pending_assistant: ChatMessage | None = None
            for m in reversed(body.messages):
                if m.role == "assistant" and m.tool_calls:
                    last_pending_assistant = m
                    break
            if last_pending_assistant is not None:
                injected: list[ChatMessage] = []
                injected_tool_names: list[str] = []
                for tc in last_pending_assistant.tool_calls or []:
                    tc_id = tc.get("id")
                    if not isinstance(tc_id, str) or tc_id in fulfilled_call_ids:
                        continue
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name")
                    args = fn.get("arguments")
                    if not isinstance(tool_name, str) or args is None:
                        continue
                    cached_result: str | None = None
                    try:
                        cached_result = await trc.lookup(
                            team_id=principal.team_id,
                            tool_name=tool_name,
                            args=args,
                        )
                    except Exception as e:  # observer is fail-open
                        log.warning(
                            "tool_result_cache.lookup_failed",
                            error=str(e),
                            tool_name=tool_name,
                        )
                        cached_result = None
                    if cached_result is None:
                        record_tool_result_cache(tool_name=tool_name, result="miss")
                        continue
                    injected.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=tc_id,
                            content=cached_result,
                        )
                    )
                    injected_tool_names.append(tool_name)
                    tool_cache_hits += 1
                    record_tool_result_cache(tool_name=tool_name, result="hit")
                if injected:
                    # Append AFTER the trailing assistant message —
                    # that's where the LLM expects to see tool
                    # follow-ups. We don't remove any existing
                    # messages; injection is additive.
                    body.messages.extend(injected)
                    response.headers["X-Pronaos-Tool-Cache-Hits"] = str(tool_cache_hits)
                    response.headers["X-Pronaos-Tool-Cache-Tools"] = ",".join(
                        sorted(set(injected_tool_names))
                    )

    # ---- Cache lookup (Phase 7 / extended Phase 28) ---------------------
    # ``temperature > 0`` is the user explicitly asking for variety →
    # always bypass. Agent-loop requests (any message with
    # ``role="tool"`` or assistant ``tool_calls`` echo) also bypass
    # because the L2 semantic cache embeds only the user prompt: in a
    # tool-result round trip, the prompt is identical to turn 1 but
    # the response MUST differ. Bypassing keeps correctness.
    #
    # Phase 28: streaming requests are NO LONGER bypassed. They share
    # the same cache entries as non-streaming requests for the same
    # ``(messages, temperature, max_tokens)`` triplet. On a hit we
    # replay the stored chunks as SSE (with original cadence if
    # captured); on a miss the streaming completion handler captures
    # chunk timing and writes the cache.
    has_tool_turn = any(m.role == "tool" or m.tool_calls is not None for m in body.messages)
    cache_eligible = (body.temperature is None or body.temperature == 0.0) and not has_tool_turn
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
            if body.stream:
                # Phase 28: serve the cached response as SSE. The replay
                # generator walks ``pronaos.stream_chunks`` (when captured
                # on the original streaming call) or falls back to a
                # single-chunk emit if the entry came from a non-streaming
                # call. Either way the client sees a normal SSE response.
                record_cache_stream_replay(tier=lookup.tier or "exact")
                return _stream_cached_response(lookup.response, model=body.model)
            return lookup.response
        record_cache_lookup(tier="exact", result="miss")
        response.headers["X-Pronaos-Cache"] = "miss"
    else:
        record_cache_lookup(tier="exact", result="skip")
        response.headers["X-Pronaos-Cache"] = "skip"

    # Phase 39: structured output validation + auto-retry.
    # Extract the schema from response_format (OpenAI shape) and decide
    # whether to forward natively or fall back to prompt-injection.
    # Validation + retry happens AFTER the upstream call returns; this
    # is the prep work.
    so_schema = extract_schema(body.response_format)
    so_max_retries = 0
    so_provider_native = False
    if so_schema is not None:
        so_max_retries = principal.structured_output_max_retries
        so_provider_native = principal.structured_output_provider_native
        # When falling back to prompt-injection (team setting OR provider
        # doesn't support native structured outputs), inject a system
        # message that instructs the model to respect the schema. Done
        # ONCE at the front of the messages list — survives retries
        # (the corrective messages append after it).
        if not so_provider_native:
            schema_sys = build_schema_system_message(so_schema)
            body.messages = [ChatMessage(**schema_sys), *body.messages]

    prov_req = ProviderRequest(
        model=body.model,
        messages=[_dump_message(m) for m in body.messages],
        stream=body.stream,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        tools=body.tools,
        tool_choice=body.tool_choice,
        # Forward response_format only when team has provider_native ON.
        # When OFF, the system-prompt fallback covers correctness and we
        # deliberately don't send response_format (some providers reject
        # the call if they don't understand the shape).
        response_format=body.response_format if so_provider_native else None,
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
        prov.name: circuit_registry.get(prov.name).trip_count for prov in plan.chain()
    }

    # Time from BEFORE failover starts so the histogram includes any retry
    # cost from the failover layer — SREs and operators want to see the
    # whole upstream latency story, not just the winning provider's wire time.
    provider_call_start = time.monotonic()
    try:
        provider, stream = await execute_with_failover(
            plan,
            prov_req,
            circuit_registry=circuit_registry,
            hedge_delay_ms=principal.hedge_delay_ms,
            hedge_max_count=principal.hedge_max_count
            if principal.hedge_max_count is not None
            else 1,
        )
    finally:
        # Publish circuit-trip webhooks regardless of whether failover
        # succeeded or raised — a trip during a request that ultimately
        # 502'd is still operationally interesting.
        _publish_circuit_trips(request, principal, circuit_registry, trip_snapshot)

    # Read the hedge outcome the failover layer stashed in the contextvar
    # so we can stamp response headers below. Default (no hedge fired)
    # produces no header — clients only see hedging headers when hedging
    # actually happened.
    from pronaos.core.failover import HedgeOutcome

    hedge_outcome = hedge_outcome_var.get(HedgeOutcome())
    if hedge_outcome.triggered:
        response.headers["X-Pronaos-Hedged"] = "true"
        # ``winner_role`` is "primary" if the original beat the hedge,
        # "hedge" if the speculative call won. Both decisions are
        # interesting to clients tuning ``hedge_delay_ms`` — a primary
        # win means the delay is too low (we hedged but didn't need to);
        # a hedge win means the delay was set right (we saved tail
        # latency).
        if hedge_outcome.winner_role is not None:
            response.headers["X-Pronaos-Hedge-Winner"] = hedge_outcome.winner_role
        if hedge_outcome.hedge_provider is not None:
            response.headers["X-Pronaos-Hedge-Provider"] = hedge_outcome.hedge_provider

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
        # Phase 28: pass cache context so the streaming completion path
        # can persist captured chunk timing alongside the assembled
        # response. None if the request is cache-ineligible (temperature,
        # tool turn) — the streaming handler then skips the cache write.
        stream_cache_payload = _canonical_cache_payload(body) if cache_eligible else None
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
            cache=cache if cache_eligible else None,
            cache_key_payload=stream_cache_payload,
            ab_arm=ab_arm,
            agent_turn_id=agent_turn_id,
            agent_turn_tracker=agent_turn_tracker,
            pii_token_store=pii_token_store if tokenization_active else None,
        )
    response_body = await _handle_non_streaming(
        stream,
        provider,
        body.model,
        principal,
        quota,
        session,
        provider_call_start,
        ab_arm=ab_arm,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        prompt_cache_observer=getattr(request.app.state, "prompt_cache_observer", None),
        reasoning_observer=getattr(request.app.state, "reasoning_observer", None),
    )

    # ---- Phase 39: structured-output validation + auto-retry ------------
    # When the client supplied a JSON Schema, validate the assistant's
    # content against it. On failure, append (failed_assistant +
    # correction) to messages and re-fire the upstream call up to
    # ``so_max_retries`` times. Cache hits skip this — cached
    # responses were validated when first written.
    so_retry_count = 0
    so_final_outcome = "skip"  # skip = no schema; passed/retried/failed otherwise
    if so_schema is not None:
        retry_messages = list(body.messages)
        for attempt in range(so_max_retries + 1):
            # Pull the assistant content out of the response_body.
            content = _extract_assistant_content(response_body)
            outcome = validate_response_content(content, so_schema)
            if outcome.passed:
                so_final_outcome = "passed" if attempt == 0 else "retried"
                break
            # Failed validation. If we still have retries left, build a
            # corrective prompt and re-fire the upstream call.
            if attempt < so_max_retries:
                so_retry_count += 1
                record_schema_retry(model=body.model)
                correction = build_correction_messages(
                    failed_response_content=content or "",
                    errors=outcome.errors or [],
                    schema=so_schema,
                )
                # Convert the dict messages to ChatMessage so the
                # downstream serializer treats them like first-class
                # turns.
                retry_messages = retry_messages + [ChatMessage(**m) for m in correction]
                retry_req = ProviderRequest(
                    model=body.model,
                    messages=[_dump_message(m) for m in retry_messages],
                    stream=body.stream,
                    max_tokens=body.max_tokens,
                    temperature=body.temperature,
                    tools=body.tools,
                    tool_choice=body.tool_choice,
                    response_format=body.response_format if so_provider_native else None,
                )
                # Re-fire through the same failover plan. Each retry
                # is a separate upstream call billed to usage_records
                # (record_call inside _handle_non_streaming).
                retry_start = time.monotonic()
                retry_provider, retry_stream = await execute_with_failover(
                    plan,
                    retry_req,
                    circuit_registry=circuit_registry,
                    hedge_delay_ms=principal.hedge_delay_ms,
                    hedge_max_count=principal.hedge_max_count
                    if principal.hedge_max_count is not None
                    else 1,
                )
                response_body = await _handle_non_streaming(
                    retry_stream,
                    retry_provider,
                    body.model,
                    principal,
                    quota,
                    session,
                    retry_start,
                    ab_arm=ab_arm,
                    max_tokens=body.max_tokens,
                    temperature=body.temperature,
                    prompt_cache_observer=getattr(request.app.state, "prompt_cache_observer", None),
                    reasoning_observer=getattr(request.app.state, "reasoning_observer", None),
                )
                continue
            # Out of retries — return the last response with the
            # failed marker.
            so_final_outcome = "failed"
        # Stamp the headers regardless of outcome.
        response.headers["X-Pronaos-Schema-Validation"] = so_final_outcome
        if so_retry_count > 0:
            response.headers["X-Pronaos-Schema-Retry-Count"] = str(so_retry_count)
        record_schema_validation(result=so_final_outcome, model=body.model)

    # ---- Phase 34: stamp prompt-cache headers ---------------------------
    # Surface Anthropic prompt-caching savings on the response so
    # clients can audit cost without round-tripping to admin/usage.
    # Headers are present only when the provider reported non-zero
    # cache stats — keeps the response clean for non-Anthropic calls.
    _stamp_prompt_cache_headers(response, response_body)

    # ---- Phase 56: stamp reasoning-token header --------------------------
    # Anthropic extended thinking, OpenAI o1/o3 reasoning, DeepSeek R1
    # thinking, Gemini thoughtsTokenCount — all surface via the same
    # ``X-Pronaos-Reasoning-Tokens`` header. No-op when reasoning is 0
    # (the common case for non-reasoning models).
    _stamp_reasoning_headers(response, response_body)

    # ---- Phase 41: stamp image-token headers ---------------------------
    # Estimate the token cost of each image part for THIS model.
    # We sum and surface the total so clients can audit cost without
    # decoding the response usage block. Per-image cost varies wildly
    # by resolution (OpenAI gpt-4o ranges 85 → 1530+ tokens per image)
    # so this is genuinely useful to surface.
    if image_inventory.parts:
        total_image_tokens = sum(
            estimate_image_tokens(p, model=body.model) for p in image_inventory.parts
        )
        response.headers["X-Pronaos-Image-Tokens"] = str(total_image_tokens)
        response.headers["X-Pronaos-Image-Count"] = str(len(image_inventory.parts))
        # Tick the input metrics — one per image part, labelled by
        # provider/model so dashboards can split vision usage from
        # text-only traffic.
        provider_name = body.model.split("/", 1)[0]
        record_image_input(
            provider=provider_name,
            model=body.model,
            count=len(image_inventory.parts),
            bytes_total=image_inventory.total_base64_bytes,
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
    # Phase 37: read tool_names off the response so the audit row records
    # the same tool list as usage_records. Extract from the assistant
    # message rather than carrying through a separate variable — the
    # response is the source of truth at this point in the pipeline,
    # and post-egress-redaction tool_calls are what we want to attest.
    audit_tool_names = _extract_response_tool_names(response_body)
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
        tool_names=audit_tool_names,
    )

    # ---- Agent-turn budget record (Phase 30) ----------------------------
    # Bump the per-turn running totals so the next call's pre-flight
    # check sees the cumulative spend. Fail-open: errors logged inside
    # the tracker, never raised here.
    if agent_turn_id and agent_turn_tracker is not None:
        usage = response_body.get("usage") or {}
        pronaos_meta = response_body.get("pronaos") or {}
        await agent_turn_tracker.record(
            team_id=principal.team_id,
            turn_id=agent_turn_id,
            tokens=int(usage.get("total_tokens") or 0),
            cost_hcents=int(pronaos_meta.get("cost_hcents") or 0),
            ttl_seconds=principal.agent_turn_ttl_seconds or 3600,
        )

    # ---- Phase 38: detokenize the client-facing response ----------------
    # This runs LAST, AFTER cache write + audit append + agent-turn record.
    # The cache stores the tokenized response (so a future cache hit also
    # serves tokenized output — the egress detokenizer runs again on
    # replay and reverses against THAT request's tenant's Redis store);
    # the audit chain hashes the tokenized response (so audit records
    # remain PII-free + verifier doesn't need Redis); the agent-turn
    # tracker accumulates token/cost counts which don't care about
    # content. Only the client-facing wire response carries originals.
    if tokenization_active and pii_token_store is not None:
        await _detokenize_response_body(
            response_body,
            store=pii_token_store,
            tenant_id=principal.tenant_id,
            response=response,
        )

    # ---- Phase 40: quality sampling (background, fire-and-forget) ------
    # Coin-flip per response at rate ``team.quality_sampling_rate``.
    # When sampled, schedule a background task that calls the judge
    # model, persists a quality_samples row, and runs the degradation
    # check. Failure anywhere downstream of this schedule = log line +
    # metric; the client response has already left the building.
    if principal.quality_sampling_rate > 0.0:
        _maybe_schedule_quality_sample(
            request=request,
            principal=principal,
            body=body,
            response_body=response_body,
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
    dispatcher: WebhookDispatcher | None = getattr(request.app.state, "webhook_dispatcher", None)
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
    *,
    ab_arm: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    prompt_cache_observer: Any = None,
    reasoning_observer: Any = None,
) -> dict[str, Any]:
    # Phase 6.3 + Phase 43: explicit span for the provider call so trace
    # exploration can pivot on provider/model/tokens without filtering
    # through FastAPI's auto-generated parent span. Span name + attributes
    # follow the OTel GenAI semantic conventions
    # (https://opentelemetry.io/docs/specs/semconv/gen-ai/) so Datadog /
    # Honeycomb / Splunk / Grafana Tempo GenAI dashboards work without
    # custom field mapping. The pronaos.* attributes stay alongside the
    # gen_ai.* ones for backward compatibility with existing dashboards.
    #
    # Streaming path is intentionally not yet span-wrapped — wrapping an
    # async generator across yields gets fiddly and the metrics already
    # cover the streaming latency story.
    tracer = get_tracer("pronaos.provider")
    with tracer.start_as_current_span(span_name_for(GEN_AI_OPERATION_CHAT, model)) as span:
        # Spec-compliant request attributes (Phase 43).
        apply_gen_ai_request_attrs(
            span,
            operation=GEN_AI_OPERATION_CHAT,
            system=gen_ai_system_for(provider.name),
            request_model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        # Pronaos-custom attributes (back-compat for existing dashboards).
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
        # Phase 34: Anthropic prompt-cache token counts. Default 0 for
        # adapters that don't surface them (OpenAI-compat) — cost_cents
        # uses the kwargs to apply the weighted pricing.
        cache_creation_tokens = chunk.cache_creation_tokens or 0
        cache_read_tokens = chunk.cache_read_tokens or 0
        # Phase 56: reasoning-token surface. Default 0 / None for adapters
        # that don't surface them. Reasoning tokens are ALREADY in
        # completion_tokens for every provider that exposes them (Anthropic
        # counts thinking toward output_tokens; OpenAI/DeepSeek count
        # reasoning toward completion_tokens; Vertex Gemini's adapter
        # adds thoughtsTokenCount to completion_tokens before yielding
        # the chunk so the cost-math invariant holds uniformly here).
        reasoning_tokens = chunk.reasoning_tokens or 0
        reasoning_content = chunk.reasoning_content
        cost_hcents = provider.cost_cents(
            prompt_tokens,
            completion_tokens,
            model,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        duration = time.monotonic() - provider_call_start

        # Hot span attributes — these are what an SRE asks of a trace.
        span.set_attribute("pronaos.prompt_tokens", prompt_tokens)
        span.set_attribute("pronaos.completion_tokens", completion_tokens)
        span.set_attribute("pronaos.cost_hcents", cost_hcents)
        span.set_attribute("pronaos.duration_seconds", duration)
        if cache_creation_tokens or cache_read_tokens:
            span.set_attribute("pronaos.cache_creation_tokens", cache_creation_tokens)
            span.set_attribute("pronaos.cache_read_tokens", cache_read_tokens)

        # Spec-compliant response attributes (Phase 43). finish_reasons is
        # an array per the spec — Pronaos has single-choice today so we
        # pass a one-element tuple.
        response_id = (chunk.raw or {}).get("id") if chunk.raw else None
        response_model = (chunk.raw or {}).get("model") if chunk.raw else None
        finish_reasons = (chunk.finish_reason,) if chunk.finish_reason else None
        apply_gen_ai_response_attrs(
            span,
            response_model=response_model,
            response_id=response_id,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            finish_reasons=finish_reasons,
        )

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

    # Phase 34: record prompt-cache token counters (no-op when both zero).
    record_prompt_cache_tokens(
        provider=provider.name,
        model=model,
        read_tokens=cache_read_tokens,
        write_tokens=cache_creation_tokens,
    )

    # Phase 56: record reasoning-token counter. ``source`` distinguishes
    # the provider-reported count (OpenAI o1/o3, DeepSeek R1, Gemini
    # thoughtsTokenCount — exact, from the upstream's usage block)
    # from Pronaos's char-length estimate for Anthropic thinking
    # blocks (which Anthropic does NOT separately count in usage).
    # Anthropic thinking can land via three provider names: direct
    # ``anthropic``, ``bedrock`` (Anthropic-on-Bedrock), or ``vertex``
    # (Anthropic-on-Vertex). We treat the source as "estimated"
    # whenever the chunk carries thinking-block text (reasoning_content
    # present): that's the unambiguous signal that the count came from
    # our char-length heuristic, not the upstream usage block. OpenAI's
    # o-series doesn't expose CoT text (reasoning_content None) → source
    # stays "upstream". DeepSeek R1 ships reasoning_content but its
    # reasoning_tokens comes from completion_tokens_details — still
    # "upstream" semantically, so we discriminate on provider name as
    # well: only the three Anthropic-hosting providers count as
    # "estimated."
    _anthropic_hosts = ("anthropic", "bedrock", "vertex")
    if provider.name in _anthropic_hosts and reasoning_content is not None:
        reasoning_source = "estimated"
    else:
        reasoning_source = "upstream"
    record_reasoning_tokens(
        provider=provider.name,
        model=model,
        tokens=reasoning_tokens,
        source=reasoning_source,
    )

    # Phase 47: feed the per-team prompt-cache observer with this call's
    # token counts. The observer aggregates per-(team_id, fqmn) rolling
    # totals in Redis; the prompt-cache-aware-cheapest router consults
    # the resulting snapshot at routing time. record() is fail-open —
    # a Redis outage degrades to "no observation," and the scorer
    # already handles an empty snapshot by falling back to plain cost.
    if prompt_cache_observer is not None:
        # Compute the per-call saving for the informational saved_hcents
        # totals (the routing math uses hit_rate, not saved_hcents).
        if cache_read_tokens or cache_creation_tokens:
            no_cache_cost = provider.cost_cents(
                prompt_tokens + cache_read_tokens + cache_creation_tokens,
                completion_tokens,
                model,
            )
            saved_for_record = max(0, no_cache_cost - cost_hcents)
        else:
            saved_for_record = 0
        try:
            await prompt_cache_observer.record(
                team_id=principal.team_id,
                fqmn=f"{provider.name}/{model}",
                prompt_tokens=prompt_tokens,
                cached_tokens=cache_read_tokens,
                saved_hcents=saved_for_record,
            )
        except Exception as e:  # observer is fail-open
            log.warning("prompt_cache_observer.record_failed", error=str(e))

    # Phase 57: feed the per-team reasoning-ratio observer. We record
    # every call (including reasoning_tokens=0) so the rolling ratio
    # accurately reflects the team's workload — a model used 90% for
    # plain chat and 10% for thinking mode should NOT look like a
    # reasoning-heavy model in the snapshot. record() is fail-open;
    # the strategy degrades to plain cheapest on an empty snapshot.
    if reasoning_observer is not None and completion_tokens > 0:
        try:
            await reasoning_observer.record(
                team_id=principal.team_id,
                fqmn=f"{provider.name}/{model}",
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        except Exception as e:  # observer is fail-open
            log.warning("reasoning_observer.record_failed", error=str(e))

    # Phase 37: extract the tool names the LLM emitted in this call.
    # Empty tuple (not None) when the response carried no tool_calls —
    # makes the downstream metric/budget code branchless. Each name is
    # passed both into CompletedCall (for usage_records + tool_budgets
    # increment) and into the metric emitter below.
    emitted_tools_list = tool_names_from_calls(chunk.tool_calls)
    emitted_tools: tuple[str, ...] | None = tuple(emitted_tools_list) or None
    for name in emitted_tools_list:
        record_tool_call_emitted(tool_name=name)

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
            ab_arm=ab_arm,
            tool_names=emitted_tools,
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

    # Phase 34: build the pronaos metadata block. Cache stats and savings
    # are included only when the provider returned non-zero values —
    # avoids noise on every chat call when the client didn't use caching.
    pronaos_meta: dict[str, Any] = {
        "provider": provider.name,
        "cost_hcents": cost_hcents,
    }
    if cache_creation_tokens or cache_read_tokens:
        pronaos_meta["cache_creation_tokens"] = cache_creation_tokens
        pronaos_meta["cache_read_tokens"] = cache_read_tokens
        # Saved cost = what cache_read_tokens WOULD have cost as regular
        # input, minus what we actually paid (0.10x). Reports the
        # FinOps win in hundredths of a cent. Cache writes are NOT a
        # saving — they're a one-time investment.
        no_cache_cost = provider.cost_cents(
            prompt_tokens + cache_read_tokens + cache_creation_tokens,
            completion_tokens,
            model,
        )
        pronaos_meta["cache_saved_hcents"] = max(0, no_cache_cost - cost_hcents)
    # Phase 56: reasoning metadata. Only attach when non-zero so chats
    # against non-reasoning models stay metadata-light.
    if reasoning_tokens:
        pronaos_meta["reasoning_tokens"] = reasoning_tokens
    if reasoning_content:
        pronaos_meta["reasoning_content"] = reasoning_content

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
        "pronaos": pronaos_meta,
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
    cache: Cache | None = None,
    cache_key_payload: dict[str, Any] | None = None,
    ab_arm: str | None = None,
    agent_turn_id: str | None = None,
    agent_turn_tracker: Any = None,
    pii_token_store: TokenStore | None = None,
) -> StreamingResponse:
    # The stream has already been resolved by the failover executor — any
    # construction-time error was surfaced there and converted to a JSON
    # response by the global error handler. From here on, errors are
    # mid-stream and can only be logged.
    #
    # Phase 28: when ``cache`` + ``cache_key_payload`` are both supplied
    # the streaming generator captures (text, inter_chunk_delay_ms)
    # pairs and writes the assembled response + chunk metadata into the
    # cache on clean completion. None on either argument disables the
    # write — used for cache-ineligible streams (temperature > 0, tool
    # turn).
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
        cache=cache,
        cache_key_payload=cache_key_payload,
        ab_arm=ab_arm,
        agent_turn_id=agent_turn_id,
        agent_turn_tracker=agent_turn_tracker,
        pii_token_store=pii_token_store,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_cached_response(stored: dict[str, Any], *, model: str) -> StreamingResponse:
    """Replay a cached non-streaming response as SSE (Phase 28).

    Two shapes the cached entry might have:

    1. **Captured during a prior streaming call** — carries
       ``pronaos.stream_chunks`` (a list of ``{text, delay_ms}`` entries).
       Replay walks the list, sleeps ``delay_ms`` before each chunk, and
       emits an SSE event per entry. The client experiences the original
       cadence, including the original time-to-first-token.

    2. **Captured during a prior non-streaming call** — has no
       ``stream_chunks``. Replay falls back to emitting the assembled
       ``content`` as a single content delta. Client still receives a
       valid SSE response, just without the captured cadence.

    Either way the wire shape matches what ``_sse_openai_chunks`` would
    emit for a fresh stream — same role-marker opener, same content
    deltas, same closing chunk with usage + finish_reason.
    """

    async def _generate() -> AsyncIterator[str]:
        request_id = stored.get("id") or _chat_id()
        created = int(time.time())
        choices = stored.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        full_content = message.get("content") or ""
        finish_reason = choice.get("finish_reason") or "stop"
        pronaos_meta = stored.get("pronaos") or {}
        stream_chunks = pronaos_meta.get("stream_chunks")

        # Opening role-marker chunk — matches the fresh-stream opener.
        yield _sse(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )

        if isinstance(stream_chunks, list) and stream_chunks:
            # Replay at the original inter-chunk cadence. The FIRST
            # chunk's stored ``delay_ms`` is the original time-to-first-
            # token (time from request issue to first provider chunk).
            # On cache replay we *don't* reproduce that wait — the whole
            # point of the hit is the user gets the first token without
            # paying the upstream's TTFT again. Subsequent chunks
            # preserve their stored inter-chunk gaps so the streaming
            # UX feels natural rather than a single dump.
            for i, entry in enumerate(stream_chunks):
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text") or ""
                delay_ms_raw = entry.get("delay_ms")
                delay_s = (
                    float(delay_ms_raw) / 1000.0 if isinstance(delay_ms_raw, int | float) else 0.0
                )
                if i > 0 and delay_s > 0:
                    await asyncio.sleep(delay_s)
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        elif full_content:
            # No captured cadence — emit content as a single chunk so the
            # client still gets a valid SSE response.
            yield _sse(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": full_content},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        # Closing chunk with finish_reason + usage (matches the fresh
        # generator's tail). Cached usage carries the original token
        # counts and cost so dashboards remain accurate on hit.
        usage = stored.get("usage") or _usage(0, 0)
        cached_pronaos: dict[str, Any] = {
            "provider": pronaos_meta.get("provider", ""),
            "cost_hcents": pronaos_meta.get("cost_hcents", 0),
            "cache": "hit",
        }
        yield _sse(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                "usage": usage,
                "pronaos": cached_pronaos,
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Pronaos-Cache": "hit:replay",
        },
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
    cache: Cache | None = None,
    cache_key_payload: dict[str, Any] | None = None,
    ab_arm: str | None = None,
    agent_turn_id: str | None = None,
    agent_turn_tracker: Any = None,
    pii_token_store: TokenStore | None = None,
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
    # Phase 28: track inter-chunk timing so a future cache hit can replay
    # the response at the original cadence. Each entry is
    # ``{"text": ..., "delay_ms": ...}`` where ``delay_ms`` is the time
    # from the previous chunk (the FIRST chunk uses time-since-provider-
    # call-start so the cached replay also reproduces the original
    # time-to-first-token). Tool-only chunks (no content) are NOT
    # captured — replay reproduces visible deltas, not the protocol
    # mechanics.
    chunk_timing: list[dict[str, Any]] = []
    last_chunk_at = provider_call_start
    # Phase 12: streaming tools. Providers accumulate per-chunk tool_call
    # fragments internally and emit the assembled list on a single
    # "tail" ChatCompletionChunk. We capture it here and re-shape into
    # OpenAI streaming-tools format on the wire (one ``delta.tool_calls``
    # event per tool — index, id, name, full arguments string).
    accumulated_tool_calls: list[dict[str, Any]] | None = None

    # Phase 38: per-stream detokenizer that reverses ``[TYPE_HASH]``
    # tokens emitted by the upstream LLM into their original PII
    # values. Holds a small (≤31 char) buffer at the chunk boundary
    # to handle tokens that span two chunks. ``None`` when the team
    # hasn't enabled tokenization OR Redis isn't available.
    detok: StreamingDetokenizer | None = (
        StreamingDetokenizer(pii_token_store, tenant_id=principal.tenant_id)
        if pii_token_store is not None
        else None
    )

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
                now = time.monotonic()
                chunk_timing.append(
                    {
                        "text": chunk.content_delta,
                        "delay_ms": round((now - last_chunk_at) * 1000.0, 3),
                    }
                )
                last_chunk_at = now
                content_buffer.append(chunk.content_delta)
                # Phase 38: when tokenization is active, reverse tokens
                # in this chunk before yielding to the client. The
                # StreamingDetokenizer holds back a small tail buffer
                # (worst-case partial token) so tokens spanning the
                # chunk boundary aren't corrupted; the buffer flushes
                # at stream end below.
                emit_text = chunk.content_delta
                if detok is not None:
                    emit_text = await detok.feed(chunk.content_delta)
                    if not emit_text:
                        # Whole chunk got held in the partial-token
                        # buffer (rare, only for tiny chunks with an
                        # unclosed ``[``). Skip the SSE emit; the
                        # text rejoins the next chunk.
                        continue
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": emit_text},
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

        # Phase 38: flush any text still held in the detokenizer's tail
        # buffer (e.g. a final ``"...email is [EMAIL_a3f7c2e1b890]"``
        # whose token landed in the last chunk). The buffer's content
        # passes through ``reverse_text`` so trailing tokens still
        # resolve.
        if detok is not None:
            tail = await detok.flush()
            if tail:
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": tail},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            for rule_suffix, count in detok.reversed_by_type.items():
                record_pii_token_reversed(rule=rule_suffix, count=count)
            for rule_suffix, count in detok.orphaned_by_type.items():
                record_pii_token_orphaned(rule=rule_suffix, count=count)
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
            ab_arm=ab_arm,
            agent_turn_id=agent_turn_id,
            agent_turn_tracker=agent_turn_tracker,
            agent_turn_ttl_seconds=principal.agent_turn_ttl_seconds,
            team_id=principal.team_id,
        )

        # Phase 28: persist the stream into the cache with chunk timing.
        # The cached entry mirrors the non-streaming response shape so a
        # future non-streaming request for the same key can also serve
        # from this entry (it just joins the chunks). Tool calls bypass
        # the cache write — replaying a cached tool_calls list would
        # short-circuit the agent loop a future request expects to
        # continue with fresh tool results.
        if (
            cache is not None
            and cache_key_payload is not None
            and accumulated_tool_calls is None
            and content_buffer
        ):
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(content_buffer),
            }
            cached_response: dict[str, Any] = {
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
                    # Stream-replay metadata. Each entry carries the
                    # text fragment and the wall-time gap since the
                    # previous chunk (or since provider_call_start for
                    # the first entry). On replay we sleep ``delay_ms``
                    # before emitting each chunk so the cached client
                    # experiences the same cadence — including the
                    # original time-to-first-token.
                    "stream_chunks": chunk_timing,
                },
            }
            with contextlib.suppress(Exception):
                await cache.put(
                    tenant_id=principal.tenant_id,
                    model=model,
                    key_payload=cache_key_payload,
                    response=cached_response,
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


async def _detokenize_response_body(
    response_body: dict[str, Any],
    *,
    store: TokenStore,
    tenant_id: str,
    response: Response,
) -> None:
    """Reverse PII tokens in the assistant's message content (Phase 38).

    Walks ``choices[].message.content`` and replaces ``[TYPE_HASH]``
    tokens with the original values from Redis. Mutates the response
    body in place — caller already cached + audited the tokenized
    version, so this is purely client-facing.

    Headers:
      - ``X-Pronaos-PII-Reversed``: count of tokens successfully
        reversed (omitted when 0).
      - ``X-Pronaos-PII-Orphaned``: count of tokens left in the
        response that didn't resolve (Redis miss / TTL elapsed /
        hallucinated token). Operationally a Redis health signal —
        clients can alert on persistent non-zero values.
    """
    choices = response_body.get("choices")
    if not isinstance(choices, list):
        return
    total_reversed = 0
    total_orphaned = 0
    reversed_by_type: dict[str, int] = {}
    orphaned_by_type: dict[str, int] = {}
    for choice in choices:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        outcome = await store.reverse_text(tenant_id=tenant_id, text=content)
        if outcome.text != content:
            msg["content"] = outcome.text
        total_reversed += outcome.reversed_count
        total_orphaned += outcome.orphaned_count
        if outcome.reversed_by_type:
            for k, v in outcome.reversed_by_type.items():
                reversed_by_type[k] = reversed_by_type.get(k, 0) + v
        if outcome.orphaned_by_type:
            for k, v in outcome.orphaned_by_type.items():
                orphaned_by_type[k] = orphaned_by_type.get(k, 0) + v
    for rule_suffix, count in reversed_by_type.items():
        record_pii_token_reversed(rule=rule_suffix, count=count)
    for rule_suffix, count in orphaned_by_type.items():
        record_pii_token_orphaned(rule=rule_suffix, count=count)
    if total_reversed > 0:
        response.headers["X-Pronaos-PII-Reversed"] = str(total_reversed)
    if total_orphaned > 0:
        response.headers["X-Pronaos-PII-Orphaned"] = str(total_orphaned)


def _maybe_schedule_quality_sample(
    *,
    request: Request,
    principal: Principal,
    body: ChatCompletionBody,
    response_body: dict[str, Any],
) -> None:
    """Coin-flip sampling for the quality monitor (Phase 40).

    Fire-and-forget: schedules a background task that runs the judge
    call + persistence + degradation check. The chat response has
    already left the building by the time this returns — we never
    let sampling slow down the hot path.

    Sampling rate is read from the principal (loaded at auth time).
    Random source: ``secrets.randbelow`` for cryptographic-quality
    randomness — overkill statistically but the secrets module is
    already imported by the API key code and re-using it avoids
    pulling in numpy or random for this one call.
    """
    import secrets

    rate = principal.quality_sampling_rate
    if rate <= 0.0:
        return
    # secrets.randbelow(1_000_000) / 1_000_000 = uniform [0, 1).
    if secrets.randbelow(1_000_000) / 1_000_000 >= rate:
        return

    user_prompt = _extract_first_user_content(body)
    assistant_content = _extract_assistant_content(response_body)
    if not user_prompt or not assistant_content:
        # Nothing to judge — skip silently. Empty/tool-only responses
        # don't carry quality signal we can score.
        return

    sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
    if sessionmaker is None:
        return

    # Capture the values we need NOW so the background task doesn't
    # close over mutable request state.
    tenant_id = principal.tenant_id
    team_id = principal.team_id
    model = body.model
    request_id = _current_request_id()
    judge_model = principal.quality_judge_model or get_settings().quality_default_judge_model

    # Pin the task to app.state so the GC doesn't collect it mid-flight.
    # Same pattern asyncio docs recommend for fire-and-forget tasks —
    # without a strong reference the runtime can drop the task before
    # it completes.
    tasks: set[asyncio.Task[None]] = getattr(request.app.state, "_quality_sample_tasks", set())
    if not hasattr(request.app.state, "_quality_sample_tasks"):
        request.app.state._quality_sample_tasks = tasks
    task = asyncio.create_task(
        _run_quality_sample(
            sessionmaker=sessionmaker,
            gateway_base_url=str(request.base_url).rstrip("/"),
            api_key=_extract_bearer(request),
            judge_model=judge_model,
            tenant_id=tenant_id,
            team_id=team_id,
            model=model,
            request_id=request_id,
            user_prompt=user_prompt,
            assistant_content=assistant_content,
        )
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _run_quality_sample(
    *,
    sessionmaker: Any,
    gateway_base_url: str,
    api_key: str | None,
    judge_model: str,
    tenant_id: str,
    team_id: str,
    model: str,
    request_id: str | None,
    user_prompt: str,
    assistant_content: str,
) -> None:
    """Background task body — judge, persist, check degradation.

    Wrapped in a broad try so a single bad sample never crashes the
    asyncio task pool. Failures bump the ``failed`` metric so a
    persistent judge outage is visible without scraping logs.
    """
    try:
        if not api_key:
            # No bearer to call ourselves with — sampling needs a key
            # because the judge call goes back through the gateway.
            record_quality_sample(model=model, result="failed")
            return
        score = await judge_response(
            base_url=gateway_base_url,
            api_key=api_key,
            judge_model=judge_model,
            prompt=user_prompt,
            response=assistant_content,
        )
        if score is None:
            record_quality_sample(model=model, result="failed")
            return
        record_quality_sample(model=model, result="ok")
        async with sessionmaker() as session:
            await record_sample(
                session,
                tenant_id=tenant_id,
                team_id=team_id,
                model=model,
                score=score,
                judge_model=judge_model,
                request_id=request_id,
            )
            check = await check_degradation(session, team_id=team_id, model=model)
            await session.commit()
        if check is not None and check.transition.value != "no_change":
            record_quality_degradation(model=model, action=check.transition.value)
            log.info(
                "quality_monitor.transition",
                team=team_id,
                model=model,
                action=check.transition.value,
                recent_mean=check.recent_mean,
                baseline_mean=check.baseline_mean,
                n_recent=check.n_recent,
                p_value=check.p_value,
            )
    except Exception as e:
        record_quality_sample(model=model, result="failed")
        log.warning(
            "quality_monitor.sample_task_failed",
            team=team_id,
            model=model,
            error=str(e),
        )


def _extract_first_user_content(body: ChatCompletionBody) -> str | None:
    """Pull the most recent user message's text content out of ``body``.

    Used by the quality sampler to give the judge the right context.
    Returns ``None`` when no user message has text content (tool-only
    chains, empty conversations). Handles Phase-41 multi-modal content
    by flattening text parts.
    """
    for msg in reversed(body.messages):
        if msg.role != "user":
            continue
        flat = _flatten_text_content(msg.content)
        if flat:
            return flat
    return None


def _flatten_text_content(content: str | list[dict[str, Any]] | None) -> str:
    """Pull all text parts out of multi-modal content as one string.

    Phase 41: ``ChatMessage.content`` may be a list of OpenAI-shape
    parts (``[{"type":"text","text":"..."}, {"type":"image_url",...}]``).
    For guardrail scanning + cache-key derivation + quality sampling
    we need the concatenated text. Image parts contribute nothing
    to the regex/judge scan.

    Plain string content returns unchanged. ``None`` returns empty.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                out_parts.append(text)
    return "\n".join(out_parts)


def _replace_text_in_content(
    original: str | list[dict[str, Any]] | None, new_text: str
) -> str | list[dict[str, Any]]:
    """Rebuild ``original`` content with all text parts replaced by ``new_text``.

    Phase 41 helper for the guardrail post-scan rewrite. When original
    content is a plain string, the result is the new string. When it's
    multi-modal, the image parts are preserved and a single text part
    holds the redacted/tokenized text — preferred over splitting
    because guardrail rules can match across part boundaries and
    rebuilding the original split would require re-scanning each
    part individually.
    """
    if original is None or isinstance(original, str):
        return new_text
    new_parts: list[dict[str, Any]] = [{"type": "text", "text": new_text}]
    for part in original:
        if isinstance(part, dict) and part.get("type") != "text":
            new_parts.append(part)
    return new_parts


def _extract_bearer(request: Request) -> str | None:
    """Pull the Bearer token from the request's Authorization header.

    The quality sampler calls back into the gateway to invoke the judge
    model — needs the caller's own API key so the judge call goes
    through auth + quota tracking like any other completion. None
    when the header is missing/malformed (sampling skipped).
    """
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


def _extract_assistant_content(response_body: dict[str, Any]) -> str | None:
    """Pull the assistant's text content out of an OpenAI-shape response.

    Returns ``None`` when no content is present (tool-only response or
    empty completion). Used by the Phase 39 schema-validation loop to
    feed the content into ``validate_response_content``.
    """
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return None


def _extract_response_tool_names(response_body: dict[str, Any]) -> tuple[str, ...] | None:
    """Pull tool names out of an OpenAI-shape assistant response (Phase 37).

    Walks ``choices[0].message.tool_calls`` and returns the function names
    in emission order. Returns ``None`` when no tool_calls present so the
    audit logger writes NULL into the column (matches the "no tools used"
    semantic). Duplicates are preserved — same tool called twice in one
    response is two budget hits and two audit-visible invocations.
    """
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    if not isinstance(msg, dict):
        return None
    tcs = msg.get("tool_calls")
    names = tool_names_from_calls(tcs)
    return tuple(names) or None


def _stamp_prompt_cache_headers(response: Response, response_body: dict[str, Any]) -> None:
    """Phase 34: surface Anthropic prompt-cache stats on response headers.

    Pulls cache_creation_tokens / cache_read_tokens / cache_saved_hcents
    from the ``pronaos`` metadata block (only present when the upstream
    returned non-zero cache stats). Stamps three X-Pronaos headers so
    clients can audit cost savings without parsing the body.

    No-op when the provider didn't report cache stats (the common case
    for OpenAI-compat upstreams).
    """
    meta = response_body.get("pronaos")
    if not isinstance(meta, dict):
        return
    read = meta.get("cache_read_tokens")
    write = meta.get("cache_creation_tokens")
    saved = meta.get("cache_saved_hcents")
    if read is None and write is None:
        return
    if read is not None:
        response.headers["X-Pronaos-Prompt-Cache-Read-Tokens"] = str(int(read))
    if write is not None:
        response.headers["X-Pronaos-Prompt-Cache-Write-Tokens"] = str(int(write))
    if saved is not None:
        response.headers["X-Pronaos-Prompt-Cache-Saved-Hcents"] = str(int(saved))


def _stamp_reasoning_headers(response: Response, response_body: dict[str, Any]) -> None:
    """Phase 56: surface reasoning / extended-thinking token count on
    response headers.

    Symmetric with ``_stamp_prompt_cache_headers``: pulls
    ``reasoning_tokens`` from the ``pronaos`` metadata block (set only
    when the model emitted non-zero reasoning), stamps a single
    ``X-Pronaos-Reasoning-Tokens`` header so clients can audit
    extended-thinking spend without parsing the body. CoT content is
    intentionally NOT exposed via headers (header values can be logged
    by intermediaries — keeping reasoning content body-only matches
    Anthropic's own posture on thinking-block visibility).

    No-op when the model didn't emit reasoning tokens (the common case
    for non-reasoning models).
    """
    meta = response_body.get("pronaos")
    if not isinstance(meta, dict):
        return
    reasoning_tokens = meta.get("reasoning_tokens")
    if reasoning_tokens is None or not int(reasoning_tokens):
        return
    response.headers["X-Pronaos-Reasoning-Tokens"] = str(int(reasoning_tokens))


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
    ab_arm: str | None = None,
    agent_turn_id: str | None = None,
    agent_turn_tracker: Any = None,
    agent_turn_ttl_seconds: int | None = None,
    team_id: str | None = None,
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
            action_label = "redact" if egress_verdict.text != full_content else "log_only"
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

    # Phase 37: extract tool names emitted on this streaming response so
    # both the audit row and usage_record carry the same information as
    # the non-streaming path. ``accumulated_tool_calls`` was assembled
    # from per-chunk tool_call fragments earlier in the SSE generator.
    emitted_tools_list = tool_names_from_calls(accumulated_tool_calls)
    emitted_tools: tuple[str, ...] | None = tuple(emitted_tools_list) or None
    for name in emitted_tools_list:
        record_tool_call_emitted(tool_name=name)

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
        tool_names=emitted_tools,
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
            ab_arm=ab_arm,
            tool_names=emitted_tools,
        ),
    )

    # Phase 30: agent-turn budget record. Bump the per-turn running
    # totals so the next call's pre-flight check sees the cumulative
    # spend. Fail-open: errors logged inside the tracker.
    if agent_turn_id and agent_turn_tracker is not None and team_id is not None:
        cost_now = provider.cost_cents(prompt_tokens, completion_tokens, model)
        await agent_turn_tracker.record(
            team_id=team_id,
            turn_id=agent_turn_id,
            tokens=prompt_tokens + completion_tokens,
            cost_hcents=cost_now,
            ttl_seconds=agent_turn_ttl_seconds or 3600,
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


# --------------------------------------------------------------------------- #
# Phase 54 — MCP client federation                                            #
# --------------------------------------------------------------------------- #


_MCP_FEDERATION_MAX_ITERATIONS_DEFAULT = 5
_MCP_FEDERATION_MAX_ITERATIONS_CAP = 10


async def _run_mcp_federation_loop(
    *,
    request: Request,
    body: ChatCompletionBody,
    response: Response,
    principal: Principal,
) -> dict[str, Any]:
    """Multi-turn loop for MCP client federation.

    Opens connections to every spec'd MCP server, augments the
    request's ``tools`` array with the federated tool schemas (each
    namespaced as ``{server-name}.{tool-name}``), loopback-POSTs to
    ``/v1/chat/completions`` with the federation field stripped, and
    keeps looping while the upstream returns ``tool_calls`` that point
    at federated tools. Per-iteration:

    1. Build the augmented body (tools = client_tools + federated_tools).
    2. Loopback POST to the same endpoint with the augmented body.
    3. Inspect ``response.choices[0].message.tool_calls``:
       - If any matches a federated prefix → dispatch through the
         right session, append a synthetic ``tool`` role message, loop.
       - If none match → return the response.

    Iteration cap defaults to 5; clients can override via the
    ``X-Pronaos-MCP-Max-Iterations`` header (clamped to 10).
    """
    # Validate + open federation. Malformed spec → 422.
    raw_specs = body.pronaos_mcp_servers or []
    max_iters_header = request.headers.get("x-pronaos-mcp-max-iterations")
    try:
        max_iterations = (
            int(max_iters_header) if max_iters_header else _MCP_FEDERATION_MAX_ITERATIONS_DEFAULT
        )
    except ValueError:
        max_iterations = _MCP_FEDERATION_MAX_ITERATIONS_DEFAULT
    max_iterations = max(1, min(max_iterations, _MCP_FEDERATION_MAX_ITERATIONS_CAP))

    # Extract bearer for the loopback calls — same token the original
    # request authenticated with. Preserves auth + quota accounting.
    bearer = request.headers.get("authorization", "")

    # Loopback target is the same gateway process. base_url already
    # carries scheme + host:port + the /v1 prefix the route is mounted
    # under, so we construct the full URL from request.url_for-style
    # resolution.
    chat_url = str(request.url_for("chat_completions"))

    # Strip the federation field so the loopback call doesn't re-enter
    # this branch. Everything else passes through.
    inner_body_template = body.model_dump(exclude_none=True)
    inner_body_template.pop("pronaos_mcp_servers", None)
    # Always non-streaming on the inner call (v1 limit).
    inner_body_template["stream"] = False

    try:
        async with open_federation(raw_specs) as federation:
            federated_tool_schemas = federation.federated_tool_schemas()
            # Merge with client-supplied tools. Client tools come first;
            # federated tools appended. Names can't collide because the
            # federation prefixes its tools with the server name (the
            # spec validator rejects names that would collide with
            # client tools at runtime if a client happened to name one
            # of theirs ``foo.bar``).
            base_tools = list(inner_body_template.get("tools") or [])
            augmented_tools = base_tools + federated_tool_schemas
            inner_body_template["tools"] = augmented_tools

            messages = list(inner_body_template.get("messages") or [])
            last_payload: dict[str, Any] = {}
            iterations_used = 0

            async with httpx.AsyncClient(timeout=120.0) as client:
                for iteration in range(1, max_iterations + 1):
                    iterations_used = iteration
                    inner_body = {**inner_body_template, "messages": messages}
                    resp = await client.post(
                        chat_url,
                        headers={"Authorization": bearer},
                        json=inner_body,
                    )
                    # Propagate non-2xx as-is — the inner handler
                    # already 4xx / 5xx'd, surface to the caller.
                    if resp.status_code >= 400:
                        record_mcp_federation_session(result="ok")
                        # Mirror the inner status + body
                        response.status_code = resp.status_code
                        return resp.json() if resp.content else {}
                    last_payload = resp.json()

                    # Walk the response for federated tool_calls.
                    choices = last_payload.get("choices") or []
                    if not choices:
                        record_mcp_federation_session(result="ok")
                        _stamp_federation_headers(
                            response,
                            opened=federation.opened_server_names,
                            failed=list(federation.failed_server_names.keys()),
                            iterations=iterations_used,
                        )
                        return last_payload
                    msg = choices[0].get("message") or {}
                    tool_calls = msg.get("tool_calls") or []
                    federated_calls = [
                        tc
                        for tc in tool_calls
                        if isinstance(tc, dict)
                        and federation.is_federated_tool_name(
                            (tc.get("function") or {}).get("name", "")
                        )
                    ]
                    if not federated_calls:
                        # Final response (or response with non-federated
                        # tool_calls the client is expected to handle).
                        record_mcp_federation_session(result="ok")
                        _stamp_federation_headers(
                            response,
                            opened=federation.opened_server_names,
                            failed=list(federation.failed_server_names.keys()),
                            iterations=iterations_used,
                        )
                        return last_payload

                    # Append the assistant message (with the tool_calls)
                    # so the upstream has the call/result pair on the
                    # next turn.
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.get("content") or "",
                            "tool_calls": tool_calls,
                        }
                    )
                    # Dispatch each federated tool_call.
                    for tc in federated_calls:
                        prefixed = (tc.get("function") or {}).get("name", "")
                        arg_str = (tc.get("function") or {}).get("arguments") or "{}"
                        try:
                            args = (
                                json.loads(arg_str) if isinstance(arg_str, str) else dict(arg_str)
                            )
                        except json.JSONDecodeError:
                            args = {}
                        server, _, original = prefixed.partition(".")
                        result = await federation.call_tool(prefixed, args)
                        label = "upstream_error" if result["is_error"] else "ok"
                        if server not in federation.opened_server_names:
                            label = "federation_error"
                        record_mcp_federated_tool_call(server=server, tool=original, result=label)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": result["content"],
                            }
                        )
                    # Loop: re-fire chat with the updated messages.

                # Loop terminated by max-iterations cap.
                record_mcp_federation_session(result="max_iterations")
                _stamp_federation_headers(
                    response,
                    opened=federation.opened_server_names,
                    failed=list(federation.failed_server_names.keys()),
                    iterations=iterations_used,
                )
                response.headers["X-Pronaos-MCP-Max-Iterations-Reached"] = "1"
                return last_payload
    except ValueError as e:
        record_mcp_federation_session(result="invalid_spec")
        raise HTTPException(
            status_code=422,
            detail={
                "type": "mcp_invalid_spec",
                "message": str(e),
            },
        ) from e


def _stamp_federation_headers(
    response: Response,
    *,
    opened: list[str],
    failed: list[str],
    iterations: int,
) -> None:
    """Stamp federation telemetry on the response headers."""
    if opened:
        response.headers["X-Pronaos-MCP-Federated-Servers"] = ",".join(opened)
    if failed:
        response.headers["X-Pronaos-MCP-Failed-Servers"] = ",".join(failed)
    response.headers["X-Pronaos-MCP-Iterations"] = str(iterations)


async def _run_mcp_streaming_federation(
    *,
    request: Request,
    body: ChatCompletionBody,
    principal: Principal,
) -> StreamingResponse:
    """Phase 58: streaming MCP client federation.

    Closes the Phase 54 documented honest-limit (``stream=true`` +
    ``pronaos_mcp_servers`` returned 422). v1 design: run the existing
    non-streaming federation loop to completion, then synthesize an
    OpenAI-shape SSE stream from its final response. Federation
    headers are stamped on the StreamingResponse so IDE clients see
    the same telemetry the non-streaming path exposes.

    Honest-limit (documented in CLAIMS.md Claim #45): intermediate
    iterations do NOT stream to the client — their tool-calling text
    would confuse the SSE consumer. The final assistant message is
    delivered as SSE for protocol compatibility, but TTFT equals the
    time it takes to run the full federation loop (no first-token
    benefit). True per-iteration token streaming with mid-stream
    tool_call routing is a future phase — it requires accumulating
    tool_call fragments from the stream, which adds non-trivial
    code on the OpenAI-compat path and is unsafe to ship without a
    larger refactor of the streaming adapter.

    What this DOES give clients:
    - The request shape `{"stream": true, "pronaos_mcp_servers": [...]}`
      is now accepted (no more 422) — IDE-class clients that always
      stream can use MCP federation without flipping their request
      shape per call.
    - Federation headers (``X-Pronaos-MCP-Federated-Servers``,
      ``X-Pronaos-MCP-Iterations``) flow through.
    - The audit/quota/guardrail middleware chain runs identically to
      non-streaming (each loopback chat completion is independently
      audited).
    """
    # Re-use the non-streaming federation loop by flipping the
    # stream flag off on the inner body. The loop already handles
    # all the per-iteration tool-routing logic; we just need its
    # final response to feed into the SSE synthesizer.
    inner_body = body.model_copy(update={"stream": False})

    # Capture loop's response.headers in a throwaway Response so we
    # can carry them onto the streaming response below.
    captured = Response()
    try:
        final_payload = await _run_mcp_federation_loop(
            request=request,
            body=inner_body,
            response=captured,
            principal=principal,
        )
    except HTTPException as e:
        # Mirror the loop's failure taxonomy on the streaming counter,
        # then re-raise so the outer error handling sees an HTTPException
        # (FastAPI renders it correctly for the client).
        detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
        result = "invalid_spec" if detail.get("type") == "mcp_invalid_spec" else "error"
        record_mcp_streaming_federation_session(result=result)
        raise
    # Mirror the inner non-streaming counter's terminal result on the
    # streaming counter. The non-streaming loop has already incremented
    # ``mcp_federation_sessions_total`` for the same session — that
    # double-counts ``ok`` sessions in the union of both counters,
    # which is documented behaviour: operators read each counter in
    # isolation. Sum of both = (non-streaming + 2*streaming), which
    # the metric docstring spells out.
    streaming_result = (
        "max_iterations" if captured.headers.get("X-Pronaos-MCP-Max-Iterations-Reached") else "ok"
    )
    record_mcp_streaming_federation_session(result=streaming_result)

    request_id = (
        final_payload.get("id") if isinstance(final_payload, dict) else None
    ) or _chat_id()
    created = int(time.time())
    model = body.model
    final_msg: dict[str, Any] = {}
    finish_reason: str | None = "stop"
    if isinstance(final_payload, dict):
        choices = final_payload.get("choices") or []
        if choices:
            choice0 = choices[0]
            final_msg = choice0.get("message") or {}
            finish_reason = choice0.get("finish_reason") or "stop"
    content = final_msg.get("content") or ""
    tool_calls = final_msg.get("tool_calls")

    async def _emit() -> AsyncIterator[str]:
        # First chunk carries the role per OpenAI's wire shape so
        # clients (LangChain, SDK clients) that key off the role
        # header don't break on the first content delta.
        yield _sse(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
        )
        # Chunk the buffered content into reasonable-size pieces so
        # SSE consumers see multiple deltas (better than one mega-
        # delta). 64 chars per chunk is the same size grouping the
        # cached-stream-replay path uses (Phase 28).
        if content:
            step = 64
            for offset in range(0, len(content), step):
                yield _sse(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content[offset : offset + step]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        # Tool calls (if any client-supplied non-federated tools came
        # through) ride on the terminal chunk.
        terminal_delta: dict[str, Any] = {}
        if tool_calls:
            terminal_delta["tool_calls"] = tool_calls
        yield _sse(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": terminal_delta,
                        "finish_reason": finish_reason,
                    }
                ],
            }
        )
        yield "data: [DONE]\n\n"

    headers = dict(captured.headers)
    # Mark the response so dashboards / log scrapers can tell this
    # was a federated streaming response (not regular chat streaming).
    headers["X-Pronaos-MCP-Streamed"] = "1"
    return StreamingResponse(
        _emit(),
        media_type="text/event-stream",
        headers=headers,
    )
