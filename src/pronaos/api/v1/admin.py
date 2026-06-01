"""Admin endpoints — FinOps and operational visibility.

Phase 5.3 scope: ``GET /v1/admin/usage`` — paginated query over the
``usage_records`` table with filters and an aggregate totals block in
the same response.

Tenant isolation
----------------
Every query is silently scoped to ``principal.tenant_id`` regardless of
whether the caller supplied a ``team_id`` filter. A future ``admin:global``
scope can lift this restriction for self-hosted-multi-tenant operators; for
now an admin key can only inspect its own tenant's usage. That's the
correct default — a leaked admin key shouldn't expose every customer.

Scope
-----
``admin:usage`` (introduced in Phase 5.3). Distinct from a generic
``admin`` scope so least-privilege keys can be issued — a FinOps dashboard
key needs read access to usage but not the right to rotate keys or change
budgets.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import Team, UsageRecord
from pronaos.guardrails.policy import validate_policy
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# Response models                                                             #
# --------------------------------------------------------------------------- #


class UsageItem(BaseModel):
    """One row of ``usage_records``, projected for the HTTP wire."""

    id: str
    ts: datetime
    tenant_id: str
    team_id: str
    key_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_hcents: int
    request_id: str | None
    status: str


class UsageTotals(BaseModel):
    """Aggregate over the same filter set as ``items`` — NOT just the
    current page. Lets dashboards display a single 'spend this period'
    figure without making a second query."""

    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_hcents: int


class UsageResponse(BaseModel):
    items: list[UsageItem]
    totals: UsageTotals
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    start_ts: Annotated[datetime | None, Query(description="Inclusive lower bound on ts")] = None,
    end_ts: Annotated[datetime | None, Query(description="Exclusive upper bound on ts")] = None,
    team_id: Annotated[
        str | None, Query(description="Filter to one team within the tenant")
    ] = None,
    provider: Annotated[
        str | None, Query(description="Filter to one provider (e.g. 'anthropic')")
    ] = None,
    model: Annotated[str | None, Query(description="Filter to one model id")] = None,
    status: Annotated[str | None, Query(description="success | error | …")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UsageResponse:
    """Paginated query over the tenant's usage with totals.

    The response carries ``items`` (the current page) and ``totals``
    (aggregates over the *full* filter, not the page). That shape lets a
    dashboard fetch "spend this month, plus first 100 line items" in one
    request.
    """
    # Build the WHERE clause once so it applies identically to both the
    # SELECT and the aggregate query — drift between them would silently
    # corrupt the totals.
    conditions: list[Any] = [UsageRecord.tenant_id == principal.tenant_id]
    if start_ts is not None:
        conditions.append(UsageRecord.ts >= start_ts)
    if end_ts is not None:
        conditions.append(UsageRecord.ts < end_ts)
    if team_id is not None:
        conditions.append(UsageRecord.team_id == team_id)
    if provider is not None:
        conditions.append(UsageRecord.provider == provider)
    if model is not None:
        conditions.append(UsageRecord.model == model)
    if status is not None:
        conditions.append(UsageRecord.status == status)

    # Page query — newest first is the dashboard-natural default.
    items_stmt = (
        select(UsageRecord)
        .where(*conditions)
        .order_by(UsageRecord.ts.desc())
        .limit(limit)
        .offset(offset)
    )
    items = (await session.execute(items_stmt)).scalars().all()

    # Aggregate query — runs over the SAME filter. COUNT(*) gives the total
    # number of matching rows (so the client can paginate); the SUMs give
    # the FinOps numbers.
    agg_stmt = select(
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_hcents), 0),
    ).where(*conditions)
    row = (await session.execute(agg_stmt)).one()
    requests, prompt_sum, completion_sum, cost_sum = row

    totals = UsageTotals(
        requests=int(requests),
        prompt_tokens=int(prompt_sum),
        completion_tokens=int(completion_sum),
        total_tokens=int(prompt_sum) + int(completion_sum),
        cost_hcents=int(cost_sum),
    )

    return UsageResponse(
        items=[_to_item(r) for r in items],
        totals=totals,
        limit=limit,
        offset=offset,
    )


def _to_item(r: UsageRecord) -> UsageItem:
    return UsageItem(
        id=r.id,
        ts=r.ts,
        tenant_id=r.tenant_id,
        team_id=r.team_id,
        key_id=r.key_id,
        provider=r.provider,
        model=r.model,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cost_hcents=r.cost_hcents,
        request_id=r.request_id,
        status=r.status,
    )


# --------------------------------------------------------------------------- #
# Guardrail policy (Phase 8.2 / Phase 11 admin endpoint)                       #
# --------------------------------------------------------------------------- #


class GuardrailPolicyBody(BaseModel):
    """Per-team guardrail policy override.

    Same JSON shape as the column on ``teams.guardrail_policy`` and the
    CLI ``--disable`` / ``--set-action`` flags. ``disabled_rules`` skips
    a rule entirely (no scan); ``rule_actions`` overrides the action a
    rule produces when it fires. ``presidio`` toggles the Phase 22 ML
    detector (and reserves shape for future entity-level controls).
    All fields are optional; an empty body is a valid "clear policy"
    request — equivalent to ``--reset`` on the CLI.
    """

    disabled_rules: list[str] = Field(default_factory=list)
    rule_actions: dict[str, str] = Field(default_factory=dict)
    # Phase 22 — Presidio toggle. ``None`` = leave whatever's there; an
    # explicit object replaces it. ``Any`` value type so the validator
    # below (or ``validate_policy``) can range-check entries without
    # the Pydantic layer over-constraining the schema.
    presidio: dict[str, Any] | None = None
    # Phase 44 — Llama Guard ML jailbreak / unsafe-content toggle.
    # Same shape as ``presidio``. ``None`` = leave whatever's there;
    # explicit object replaces it. ``validate_policy`` range-checks
    # ``enabled``, ``model``, ``default_action``.
    llama_guard: dict[str, Any] | None = None

    def to_storage(self) -> dict[str, Any] | None:
        """Return the JSON to persist (None = clear / use engine defaults)."""
        out: dict[str, Any] = {}
        if self.disabled_rules:
            out["disabled_rules"] = sorted(set(self.disabled_rules))
        if self.rule_actions:
            out["rule_actions"] = {k: v.lower() for k, v in self.rule_actions.items()}
        if self.presidio is not None:
            out["presidio"] = self.presidio
        if self.llama_guard is not None:
            out["llama_guard"] = self.llama_guard
        return out or None


class GuardrailPolicyResponse(BaseModel):
    """Current per-team policy or ``null`` when defaults are in effect."""

    team_id: str
    policy: dict[str, Any] | None


@router.get(
    "/team/{team_id}/guardrail-policy",
    response_model=GuardrailPolicyResponse,
)
async def get_guardrail_policy(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GuardrailPolicyResponse:
    """Read the current policy for a team.

    Returns ``policy: null`` when no override is configured (engine
    defaults are in effect). Tenant-isolated: a 404 is returned both
    when the team doesn't exist AND when it belongs to a different
    tenant — admins can't probe foreign tenants by id.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return GuardrailPolicyResponse(team_id=team.id, policy=team.guardrail_policy)


@router.put(
    "/team/{team_id}/guardrail-policy",
    response_model=GuardrailPolicyResponse,
)
async def put_guardrail_policy(
    team_id: str,
    body: GuardrailPolicyBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GuardrailPolicyResponse:
    """Replace the team's policy.

    The body is validated against the same rules the CLI applies
    (unknown action names, unknown top-level keys → 422). An empty body
    clears the policy (engine defaults apply). PUT semantics: each call
    replaces the policy wholesale — there's no PATCH for incremental
    edits. Operators wanting fine-grained updates should GET, mutate
    the dict locally, then PUT back. (Phase 12 may add PATCH if the
    UX warrants.)"""
    raw_policy = body.to_storage()
    errors = validate_policy(raw_policy)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"type": "invalid_policy", "errors": errors},
        )

    team = await _load_team_for_caller(session, team_id, principal)
    team.guardrail_policy = raw_policy
    # The get_db dependency commits on clean exit — no explicit commit needed.
    return GuardrailPolicyResponse(team_id=team.id, policy=raw_policy)


# --------------------------------------------------------------------------- #
# Model allowlist (Phase 17)                                                  #
# --------------------------------------------------------------------------- #
#
# Same shape as guardrail-policy: GET + PUT on a per-team resource, tenant-
# isolated by ``_load_team_for_caller``. The JSON body is a single ``patterns``
# field — explicit ``None`` clears the allowlist (team becomes unrestricted),
# an empty list explicitly denies everything.


class AllowedModelsBody(BaseModel):
    """Per-team model allowlist.

    ``patterns`` semantics:

    - ``None`` (or field omitted): clear the allowlist; team becomes
      unrestricted.
    - ``[]``: deny everything — useful for pausing a team without
      revoking its keys.
    - ``["groq/*", "anthropic/claude-opus-*"]``: allow only models
      matching any of the fnmatch-style patterns.
    """

    patterns: list[str] | None = None


class AllowedModelsResponse(BaseModel):
    """Current per-team allowlist; ``null`` ≡ unrestricted."""

    team_id: str
    allowed_models: list[str] | None


@router.get(
    "/team/{team_id}/allowed-models",
    response_model=AllowedModelsResponse,
)
async def get_allowed_models(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedModelsResponse:
    """Read a team's current model allowlist.

    Returns ``allowed_models: null`` when no policy is set (team can
    invoke any model in the catalog). Same tenant-isolation behaviour
    as ``get_guardrail_policy``.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return AllowedModelsResponse(team_id=team.id, allowed_models=team.allowed_models)


@router.put(
    "/team/{team_id}/allowed-models",
    response_model=AllowedModelsResponse,
)
async def put_allowed_models(
    team_id: str,
    body: AllowedModelsBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedModelsResponse:
    """Replace the team's model allowlist.

    Validated against the same rules as the CLI (every entry must be a
    non-empty string). Invalid input → 422 with a per-field reason
    so operators can fix the typo without trial-and-error. PUT
    semantics: each call replaces the policy wholesale."""
    from pronaos.core.model_access import validate_allowed_models

    raw = body.patterns
    if raw is not None:
        try:
            raw = validate_allowed_models(raw)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"type": "invalid_allowed_models", "error": str(e)},
            ) from None

    team = await _load_team_for_caller(session, team_id, principal)
    team.allowed_models = raw
    return AllowedModelsResponse(team_id=team.id, allowed_models=raw)


# --------------------------------------------------------------------------- #
# Routing strategy (Phase 21)                                                 #
# --------------------------------------------------------------------------- #
#
# Per-team strategy for resolving ``model="auto"`` requests. NULL = no
# preference; the gateway falls back to ``cheapest``. Validated against
# the RoutingStrategy enum on PUT so bad strings never reach the DB.


class RoutingStrategyBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/routing-strategy``.

    ``strategy: null`` clears the column (unset → defaults to ``cheapest``);
    a string is validated against the ``RoutingStrategy`` enum.
    """

    strategy: str | None = None


class RoutingStrategyResponse(BaseModel):
    """Current per-team routing strategy; ``null`` ≡ unset (defaults to cheapest)."""

    team_id: str
    routing_strategy: str | None


@router.get(
    "/team/{team_id}/routing-strategy",
    response_model=RoutingStrategyResponse,
)
async def get_routing_strategy(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingStrategyResponse:
    """Read a team's current routing strategy.

    Returns ``routing_strategy: null`` when no strategy is set; the
    gateway will treat that as ``cheapest`` for auto-routed requests.
    Same tenant-isolation behaviour as the allowlist endpoint.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return RoutingStrategyResponse(team_id=team.id, routing_strategy=team.routing_strategy)


@router.put(
    "/team/{team_id}/routing-strategy",
    response_model=RoutingStrategyResponse,
)
async def put_routing_strategy(
    team_id: str,
    body: RoutingStrategyBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingStrategyResponse:
    """Replace the team's routing strategy.

    Validated against the ``RoutingStrategy`` enum. Invalid input → 422.
    PUT semantics: each call replaces the value wholesale.
    """
    from pronaos.core.scorer import RoutingStrategy

    raw = body.strategy
    if raw is not None:
        try:
            raw = RoutingStrategy(raw.strip().lower()).value
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "invalid_routing_strategy",
                    "error": (
                        "strategy must be one of: " + ", ".join(s.value for s in RoutingStrategy)
                    ),
                },
            ) from None

    team = await _load_team_for_caller(session, team_id, principal)
    team.routing_strategy = raw
    return RoutingStrategyResponse(team_id=team.id, routing_strategy=raw)


# --------------------------------------------------------------------------- #
# Tool-use-aware routing scores (Phase 46)                                    #
# --------------------------------------------------------------------------- #
#
# Per-team per-model BFCL-style tool-use accuracy scores. The
# ``tool-use-aware-cheapest`` routing strategy reads these to filter
# eligible models when the request carries tools. Same JSON shape as
# ``quality_scores`` but a different signal axis. NULL ≡ no data;
# the strategy degrades to plain ``cheapest`` when scores are missing.


class ToolUseScoresBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/tool-use-scores``.

    Both fields are optional; an empty body clears the data
    (equivalent to ``--reset``).

    ``scores`` shape:
        {
          "groq/llama-3.3-70b-versatile": {
            "score": 1.0,
            "n_samples": 12,
            "source_eval_id": "tool_use_basic-2026-05-21",
            "ts": "2026-05-21T17:02:00Z"
          },
          ...
        }
    """

    scores: dict[str, dict[str, Any]] | None = None
    threshold: float | None = None


class ToolUseScoresResponse(BaseModel):
    """Current per-team tool-use-aware routing data."""

    team_id: str
    tool_use_scores: dict[str, dict[str, Any]] | None
    tool_use_threshold: float | None


@router.get(
    "/team/{team_id}/tool-use-scores",
    response_model=ToolUseScoresResponse,
)
async def get_tool_use_scores(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolUseScoresResponse:
    team = await _load_team_for_caller(session, team_id, principal)
    return ToolUseScoresResponse(
        team_id=team.id,
        tool_use_scores=team.tool_use_scores,
        tool_use_threshold=team.tool_use_threshold,
    )


@router.put(
    "/team/{team_id}/tool-use-scores",
    response_model=ToolUseScoresResponse,
)
async def put_tool_use_scores(
    team_id: str,
    body: ToolUseScoresBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolUseScoresResponse:
    """Replace the team's tool-use scores + threshold.

    Validation: ``threshold`` must be in [0, 1] when set; every score
    entry must be a mapping with a numeric ``score`` field. PUT
    semantics: each call replaces the values wholesale. To merge
    (e.g. add one model without touching others), GET first, mutate
    the dict locally, then PUT back.
    """
    if body.threshold is not None and not 0.0 <= body.threshold <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_tool_use_threshold",
                "error": f"threshold must be in [0, 1], got {body.threshold!r}",
            },
        )
    if body.scores is not None:
        for fqmn, entry in body.scores.items():
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "type": "invalid_tool_use_scores",
                        "error": f"entry for {fqmn!r} must be a JSON object",
                    },
                )
            score = entry.get("score")
            if not isinstance(score, int | float):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "type": "invalid_tool_use_scores",
                        "error": f"entry for {fqmn!r} missing numeric 'score' field",
                    },
                )

    team = await _load_team_for_caller(session, team_id, principal)
    team.tool_use_scores = body.scores
    team.tool_use_threshold = body.threshold
    return ToolUseScoresResponse(
        team_id=team.id,
        tool_use_scores=body.scores,
        tool_use_threshold=body.threshold,
    )


# --------------------------------------------------------------------------- #
# Per-team prompt-cache-aware routing (Phase 47)                              #
# --------------------------------------------------------------------------- #
#
# Two surfaces:
#   * GET  /team/{id}/prompt-cache-stats — read-only snapshot of the
#     PromptCacheObserver's per-fqmn rolling totals for this team.
#     Operators audit "what's my actual prompt-cache hit rate per
#     model" without trawling Prometheus. Includes a header-style
#     ``inferred_hit_rate`` per entry, computed the same way the
#     scorer does.
#   * PUT  /team/{id}/prompt-cache-config — set the two thresholds
#     the router consults. Replace-wholesale (NULL → use scorer
#     defaults). The observer's data itself is NOT settable here —
#     it accumulates from real traffic and resets via a separate
#     DELETE endpoint (provided here too, for operator testing).


class PromptCacheConfigBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/prompt-cache-config``.

    Both fields are optional; NULL on either reverts to the scorer's
    default (20 samples, 0.10 hit-rate floor). A body with both NULL
    is the "clear my custom thresholds" shape.
    """

    min_samples: int | None = None
    min_hit_rate: float | None = None


class PromptCacheStatEntry(BaseModel):
    """One model's observed prompt-cache stats for a team."""

    fqmn: str
    n_samples: int
    prompt_tokens: int
    cached_tokens: int
    saved_hcents: int
    hit_rate: float


class PromptCacheStatsResponse(BaseModel):
    """GET response — observer snapshot + the team's configured thresholds."""

    team_id: str
    min_samples: int | None
    min_hit_rate: float | None
    stats: list[PromptCacheStatEntry]


class PromptCacheConfigResponse(BaseModel):
    """PUT response — just the thresholds (mirror of the body)."""

    team_id: str
    min_samples: int | None
    min_hit_rate: float | None


@router.get(
    "/team/{team_id}/prompt-cache-stats",
    response_model=PromptCacheStatsResponse,
)
async def get_prompt_cache_stats(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PromptCacheStatsResponse:
    """Return the team's stored thresholds + a snapshot of the
    PromptCacheObserver's per-fqmn rolling totals.

    Empty ``stats`` is the common case for a team that hasn't yet
    produced any cache-bearing responses (or that runs without a
    Redis-configured gateway). The router's behaviour in that case
    is "fall through to plain cheapest" — see scorer.py."""
    team = await _load_team_for_caller(session, team_id, principal)
    observer = getattr(request.app.state, "prompt_cache_observer", None)
    snapshot: dict[str, Any] = {}
    if observer is not None:
        snapshot = await observer.snapshot(team_id)
    entries = [
        PromptCacheStatEntry(
            fqmn=stat.fqmn,
            n_samples=stat.n_samples,
            prompt_tokens=stat.prompt_tokens,
            cached_tokens=stat.cached_tokens,
            saved_hcents=stat.saved_hcents,
            hit_rate=stat.hit_rate,
        )
        for stat in snapshot.values()
    ]
    entries.sort(key=lambda e: (-e.hit_rate, e.fqmn))
    return PromptCacheStatsResponse(
        team_id=team.id,
        min_samples=team.prompt_cache_min_samples,
        min_hit_rate=team.prompt_cache_min_hit_rate,
        stats=entries,
    )


@router.put(
    "/team/{team_id}/prompt-cache-config",
    response_model=PromptCacheConfigResponse,
)
async def put_prompt_cache_config(
    team_id: str,
    body: PromptCacheConfigBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PromptCacheConfigResponse:
    """Replace the team's prompt-cache routing thresholds.

    Validation: ``min_samples`` must be ≥ 0; ``min_hit_rate`` must be
    in [0, 1]. NULL on either reverts to the scorer's default
    (20 samples, 0.10 hit rate). Replace-wholesale semantics — same
    shape as Phase 46's tool-use-scores endpoint.
    """
    if body.min_samples is not None and body.min_samples < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_prompt_cache_min_samples",
                "error": f"min_samples must be >= 0, got {body.min_samples!r}",
            },
        )
    if body.min_hit_rate is not None and not 0.0 <= body.min_hit_rate <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_prompt_cache_min_hit_rate",
                "error": (f"min_hit_rate must be in [0, 1], got {body.min_hit_rate!r}"),
            },
        )
    team = await _load_team_for_caller(session, team_id, principal)
    team.prompt_cache_min_samples = body.min_samples
    team.prompt_cache_min_hit_rate = body.min_hit_rate
    return PromptCacheConfigResponse(
        team_id=team.id,
        min_samples=body.min_samples,
        min_hit_rate=body.min_hit_rate,
    )


@router.delete(
    "/team/{team_id}/prompt-cache-stats",
    status_code=204,
)
async def delete_prompt_cache_stats(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Wipe the PromptCacheObserver's accumulated stats for this team.

    Operator-facing: useful for testing the live-verify script (reset
    state between runs) or when an operator wants to discard stale
    observations after a known workload change. Returns 204 — there's
    nothing left to surface."""
    await _load_team_for_caller(session, team_id, principal)
    observer = getattr(request.app.state, "prompt_cache_observer", None)
    if observer is not None:
        await observer.reset(team_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Per-team reasoning-aware routing (Phase 57)                                 #
# --------------------------------------------------------------------------- #
#
# Mirrors the prompt-cache-aware admin shape (Phase 47):
#   * GET  /team/{id}/reasoning-stats — read-only snapshot of the
#     ReasoningObserver's per-fqmn rolling completion + reasoning
#     totals + computed ratio.
#   * PUT  /team/{id}/reasoning-config — set the two thresholds
#     (min_samples, max_ratio).
#   * DELETE /team/{id}/reasoning-stats — wipe observations.


class ReasoningConfigBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/reasoning-config``.

    Both fields are optional. ``min_samples`` NULL → 20.
    ``max_ratio`` NULL → no exclusion cap (the router ranks purely
    by effective cost). Replace-wholesale.
    """

    min_samples: int | None = None
    max_ratio: float | None = None


class ReasoningStatEntry(BaseModel):
    """One model's observed reasoning stats for a team."""

    fqmn: str
    n_samples: int
    completion_tokens: int
    reasoning_tokens: int
    ratio: float


class ReasoningStatsResponse(BaseModel):
    """GET response — observer snapshot + the team's configured thresholds."""

    team_id: str
    min_samples: int | None
    max_ratio: float | None
    stats: list[ReasoningStatEntry]


class ReasoningConfigResponse(BaseModel):
    """PUT response — just the thresholds (mirror of the body)."""

    team_id: str
    min_samples: int | None
    max_ratio: float | None


@router.get(
    "/team/{team_id}/reasoning-stats",
    response_model=ReasoningStatsResponse,
)
async def get_reasoning_stats(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ReasoningStatsResponse:
    """Return the team's stored thresholds + a snapshot of the
    ReasoningObserver's per-fqmn rolling totals.

    Empty ``stats`` is the common case for a team that hasn't yet
    produced any reasoning-bearing responses (or runs without a
    Redis-configured gateway). The router's behaviour in that case
    is "fall through to plain cheapest" — see scorer.py."""
    team = await _load_team_for_caller(session, team_id, principal)
    observer = getattr(request.app.state, "reasoning_observer", None)
    snapshot: dict[str, Any] = {}
    if observer is not None:
        snapshot = await observer.snapshot(team_id)
    entries = [
        ReasoningStatEntry(
            fqmn=stat.fqmn,
            n_samples=stat.n_samples,
            completion_tokens=stat.completion_tokens,
            reasoning_tokens=stat.reasoning_tokens,
            ratio=stat.ratio,
        )
        for stat in snapshot.values()
    ]
    # Highest ratio first (the reasoning-heavy models the operator
    # most wants to see).
    entries.sort(key=lambda e: (-e.ratio, e.fqmn))
    return ReasoningStatsResponse(
        team_id=team.id,
        min_samples=team.reasoning_aware_min_samples,
        max_ratio=team.reasoning_aware_max_ratio,
        stats=entries,
    )


@router.put(
    "/team/{team_id}/reasoning-config",
    response_model=ReasoningConfigResponse,
)
async def put_reasoning_config(
    team_id: str,
    body: ReasoningConfigBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ReasoningConfigResponse:
    """Replace the team's reasoning-aware routing thresholds.

    Validation: ``min_samples`` must be ≥ 0; ``max_ratio`` must be
    ≥ 0 (no upper limit — a 5x reasoning-to-output ratio is rare but
    theoretically valid on extreme math problems).
    """
    if body.min_samples is not None and body.min_samples < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_reasoning_min_samples",
                "error": f"min_samples must be >= 0, got {body.min_samples!r}",
            },
        )
    if body.max_ratio is not None and body.max_ratio < 0.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_reasoning_max_ratio",
                "error": (f"max_ratio must be >= 0, got {body.max_ratio!r}"),
            },
        )
    team = await _load_team_for_caller(session, team_id, principal)
    team.reasoning_aware_min_samples = body.min_samples
    team.reasoning_aware_max_ratio = body.max_ratio
    return ReasoningConfigResponse(
        team_id=team.id,
        min_samples=body.min_samples,
        max_ratio=body.max_ratio,
    )


@router.delete(
    "/team/{team_id}/reasoning-stats",
    status_code=204,
)
async def delete_reasoning_stats(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Wipe the ReasoningObserver's accumulated stats for this team."""
    await _load_team_for_caller(session, team_id, principal)
    observer = getattr(request.app.state, "reasoning_observer", None)
    if observer is not None:
        await observer.reset(team_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Per-team tool-call result cache (Phase 49)                                  #
# --------------------------------------------------------------------------- #
#
# Three surfaces:
#   * GET  /team/{id}/tool-result-cache — read-only snapshot of the
#     team's cached (tool_name, args) → result entries. Operators
#     audit which tools their workload memoizes most.
#   * PUT  /team/{id}/tool-result-cache-config — set the enable flag
#     and TTL.
#   * DELETE /team/{id}/tool-result-cache — wipe the team's cache
#     entirely. Useful when a tool's underlying data changes or for
#     live-verify cleanup.


class ToolResultCacheConfigBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/tool-result-cache-config``.

    ``enabled`` is required; both clearing and enabling the feature
    must be explicit. ``ttl_seconds`` is optional — NULL reverts to
    the scorer's default (3600 = 1 hour).
    """

    enabled: bool
    ttl_seconds: int | None = None


class ToolResultCacheEntryResponse(BaseModel):
    """One cached tool-result entry, surfaced via the admin GET."""

    tool_name: str
    args_hash: str
    result: str
    n_hits: int


class ToolResultCacheStatsResponse(BaseModel):
    """GET response — team config + per-tool entries from the cache."""

    team_id: str
    enabled: bool
    ttl_seconds: int | None
    entries: list[ToolResultCacheEntryResponse]


class ToolResultCacheConfigResponse(BaseModel):
    """PUT response — mirror of the body for ack."""

    team_id: str
    enabled: bool
    ttl_seconds: int | None


@router.get(
    "/team/{team_id}/tool-result-cache",
    response_model=ToolResultCacheStatsResponse,
)
async def get_tool_result_cache(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolResultCacheStatsResponse:
    """Return the team's tool-result cache config + a snapshot of the
    currently-cached entries.

    Empty ``entries`` is the common case for a team that hasn't yet
    produced any cache-bearing chat traffic (or that runs without
    Redis configured — the cache silently no-ops in that case)."""
    team = await _load_team_for_caller(session, team_id, principal)
    trc = getattr(request.app.state, "tool_result_cache", None)
    entries: list[Any] = []
    if trc is not None:
        entries = await trc.snapshot(team_id)
    return ToolResultCacheStatsResponse(
        team_id=team.id,
        enabled=team.tool_result_cache_enabled,
        ttl_seconds=team.tool_result_cache_ttl_seconds,
        entries=[
            ToolResultCacheEntryResponse(
                tool_name=e.tool_name,
                args_hash=e.args_hash,
                result=e.result,
                n_hits=e.n_hits,
            )
            for e in entries
        ],
    )


@router.put(
    "/team/{team_id}/tool-result-cache-config",
    response_model=ToolResultCacheConfigResponse,
)
async def put_tool_result_cache_config(
    team_id: str,
    body: ToolResultCacheConfigBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolResultCacheConfigResponse:
    """Replace the team's tool-result cache config.

    Validation: ``ttl_seconds`` must be > 0 when set. Replace-wholesale
    semantics — the ``enabled`` flag is required so flipping off is
    explicit.
    """
    if body.ttl_seconds is not None and body.ttl_seconds <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_tool_result_cache_ttl",
                "error": f"ttl_seconds must be > 0, got {body.ttl_seconds!r}",
            },
        )
    team = await _load_team_for_caller(session, team_id, principal)
    team.tool_result_cache_enabled = body.enabled
    team.tool_result_cache_ttl_seconds = body.ttl_seconds
    return ToolResultCacheConfigResponse(
        team_id=team.id,
        enabled=body.enabled,
        ttl_seconds=body.ttl_seconds,
    )


@router.delete(
    "/team/{team_id}/tool-result-cache",
    status_code=204,
)
async def delete_tool_result_cache(
    request: Request,
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Wipe the team's tool-result cache.

    Returns 204 — nothing left to surface. Useful when an underlying
    tool's data changes (e.g. a price feed) and stale cached results
    would mislead the LLM, or for live-verify cleanup between runs."""
    await _load_team_for_caller(session, team_id, principal)
    trc = getattr(request.app.state, "tool_result_cache", None)
    if trc is not None:
        await trc.reset(team_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Per-team hedge policy (Phase 27)                                            #
# --------------------------------------------------------------------------- #
#
# Hedging is per-team because per-tenant deployments have different
# latency tolerances. GET reads the current values; PUT replaces them
# atomically. Both columns are nullable — NULL on ``hedge_delay_ms``
# means "no hedging" regardless of what ``hedge_max_count`` is set to.


class HedgePolicyBody(BaseModel):
    """PUT body for ``/v1/admin/team/{id}/hedge-policy``.

    ``hedge_delay_ms: null`` clears hedging entirely (back to sequential
    failover). A non-null value must be ≥ 0; ≤ 0 also disables hedging
    (recommend NULL for clarity). ``hedge_max_count`` defaults to 1 when
    unset (race the primary against one alternative); set to 0 to
    disable hedging while preserving the delay value, set to 2+ on long
    chains where racing the primary against two alternatives matters
    more than the per-request cost overhead.
    """

    hedge_delay_ms: float | None = None
    hedge_max_count: int | None = None


class HedgePolicyResponse(BaseModel):
    """Current per-team hedge policy. NULL ``hedge_delay_ms`` ≡ disabled."""

    team_id: str
    hedge_delay_ms: float | None
    hedge_max_count: int | None


@router.get(
    "/team/{team_id}/hedge-policy",
    response_model=HedgePolicyResponse,
)
async def get_hedge_policy(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> HedgePolicyResponse:
    """Read the team's current hedge policy.

    Returns ``hedge_delay_ms: null`` when hedging is disabled. Same
    tenant-isolation as the other team endpoints — ``admin:usage`` for
    your own tenant only.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    return HedgePolicyResponse(
        team_id=team.id,
        hedge_delay_ms=team.hedge_delay_ms,
        hedge_max_count=team.hedge_max_count,
    )


@router.put(
    "/team/{team_id}/hedge-policy",
    response_model=HedgePolicyResponse,
)
async def put_hedge_policy(
    team_id: str,
    body: HedgePolicyBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> HedgePolicyResponse:
    """Replace the team's hedge policy.

    Validates that ``hedge_delay_ms`` and ``hedge_max_count`` are
    non-negative when set; invalid input → 422. PUT semantics: each
    call replaces both columns wholesale.
    """
    if body.hedge_delay_ms is not None and body.hedge_delay_ms < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_hedge_delay",
                "error": "hedge_delay_ms must be >= 0 (or null to disable)",
            },
        )
    if body.hedge_max_count is not None and body.hedge_max_count < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_hedge_max_count",
                "error": "hedge_max_count must be >= 0 (or null for default 1)",
            },
        )

    team = await _load_team_for_caller(session, team_id, principal)
    team.hedge_delay_ms = body.hedge_delay_ms
    team.hedge_max_count = body.hedge_max_count
    return HedgePolicyResponse(
        team_id=team.id,
        hedge_delay_ms=team.hedge_delay_ms,
        hedge_max_count=team.hedge_max_count,
    )


# --------------------------------------------------------------------------- #
# Tenant webhook config (Phase 19)                                            #
# --------------------------------------------------------------------------- #
#
# Tenant-scoped, not team-scoped, because operational events typically route
# to ONE incident channel per organisation. GET shows current state with the
# secret REDACTED (just "(set)" / "(missing)"), PUT sets both fields.


class WebhookBody(BaseModel):
    """Per-tenant webhook config.

    Either both ``url`` and ``secret`` are non-empty, or both are
    ``None`` (omitted) to clear. Mixed states (URL but no secret, or
    vice versa) → 422; sending an unsigned payload would be a security
    smell."""

    url: str | None = None
    secret: str | None = None


class WebhookResponse(BaseModel):
    """Current webhook config; secret is REDACTED on GET (the gateway
    treats it as a write-only field once stored — operators rotate by
    overwriting, never by reading)."""

    tenant_id: str
    url: str | None
    secret_set: bool


@router.get(
    "/tenant/{tenant_id}/webhook",
    response_model=WebhookResponse,
)
async def get_webhook(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookResponse:
    """Read a tenant's current webhook config.

    Tenant-isolated: a caller can only read their own tenant's config.
    Cross-tenant requests return 403. Secret is redacted in the
    response — see the WebhookResponse docstring for rationale."""
    from sqlalchemy import select

    from pronaos.db.models import Tenant

    # Tenant-isolation: the caller's tenant_id must match the URL's
    # tenant_id. We don't surface 404 for "wrong tenant" to avoid
    # leaking tenant existence to outsiders.
    if principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"type": "forbidden", "message": "cross-tenant access denied"},
        )

    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "tenant_not_found"},
        )
    return WebhookResponse(
        tenant_id=tenant_row.id,
        url=tenant_row.webhook_url,
        secret_set=bool(tenant_row.webhook_secret),
    )


@router.put(
    "/tenant/{tenant_id}/webhook",
    response_model=WebhookResponse,
)
async def put_webhook(
    tenant_id: str,
    body: WebhookBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookResponse:
    """Set or clear a tenant's webhook config.

    Body shape: ``{"url": "...", "secret": "..."}``. Both must be
    present + non-empty to enable webhooks; both omitted (or null)
    clears the config. Mixed states → 422 (a half-configured webhook
    would either fail the no-op check or send unsigned payloads —
    neither is what the operator meant)."""
    from sqlalchemy import select

    from pronaos.db.models import Tenant

    if principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"type": "forbidden", "message": "cross-tenant access denied"},
        )

    # Validate the "both or neither" invariant up front so the storage
    # layer never holds a half-configured webhook.
    url_set = bool(body.url and body.url.strip())
    secret_set = bool(body.secret and body.secret.strip())
    if url_set != secret_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_webhook_config",
                "error": (
                    "url and secret must both be set, or both omitted; "
                    f"got url_set={url_set}, secret_set={secret_set}"
                ),
            },
        )
    if url_set:
        assert body.url is not None  # narrowing for mypy
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "invalid_webhook_url",
                    "error": "webhook URL must start with http:// or https://",
                },
            )

    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "tenant_not_found"},
        )
    tenant_row.webhook_url = body.url if url_set else None
    tenant_row.webhook_secret = body.secret if secret_set else None
    return WebhookResponse(
        tenant_id=tenant_row.id,
        url=tenant_row.webhook_url,
        secret_set=bool(tenant_row.webhook_secret),
    )


# --------------------------------------------------------------------------- #
# Per-tool budgets (Phase 37)                                                 #
# --------------------------------------------------------------------------- #
#
# Operators set per-tool call caps with PUT and inspect running counters
# with GET. The chat handler reads ``teams.tool_budgets`` at every
# request and strips over-budget tools from the upstream payload.


class ToolBudgetEntry(BaseModel):
    """One tool's budget config + current state.

    Shape mirrors the column JSON exactly so admin UIs can round-trip
    without renaming fields. ``current_calls`` is read-only via PUT —
    operators reset it through the dedicated ``reset=true`` flag or
    via the CLI's ``--reset`` (PUT-with-current_calls would create
    a race window where two admins step on each other's resets)."""

    limit_calls: int = Field(..., ge=0)
    current_calls: int = Field(default=0, ge=0)


class ToolBudgetsBody(BaseModel):
    """PUT payload for ``/v1/admin/team/{id}/tool-budgets``.

    ``budgets`` shape: ``{"tool_name": {"limit_calls": N, "current_calls": M}, ...}``.
    ``reset_counters`` (optional) — when True, every tool's
    ``current_calls`` is set to 0 in the persisted dict regardless
    of what the body supplied. Useful for mid-period resets after
    an incident.

    Passing ``budgets={}`` clears all tool caps for the team (back to
    uncapped). PUT semantics: the supplied dict REPLACES the column
    wholesale — partial updates aren't supported here; use the CLI's
    ``--tool ... --limit ...`` for that.
    """

    budgets: dict[str, ToolBudgetEntry]
    reset_counters: bool = False


class ToolBudgetsResponse(BaseModel):
    team_id: str
    budgets: dict[str, ToolBudgetEntry]


@router.get(
    "/team/{team_id}/tool-budgets",
    response_model=ToolBudgetsResponse,
)
async def get_tool_budgets(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolBudgetsResponse:
    """Read the team's current per-tool budgets.

    Returns ``budgets: {}`` when nothing is configured (uncapped).
    Same tenant-isolation as the other team endpoints.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    raw = team.tool_budgets or {}
    out: dict[str, ToolBudgetEntry] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        out[name] = ToolBudgetEntry(
            limit_calls=int(entry.get("limit_calls") or 0),
            current_calls=int(entry.get("current_calls") or 0),
        )
    return ToolBudgetsResponse(team_id=team.id, budgets=out)


@router.put(
    "/team/{team_id}/tool-budgets",
    response_model=ToolBudgetsResponse,
)
async def put_tool_budgets(
    team_id: str,
    body: ToolBudgetsBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ToolBudgetsResponse:
    """Replace the team's per-tool budgets wholesale.

    PUT semantics: the entire ``tool_budgets`` column is rewritten to
    match ``body.budgets``. Tools previously configured but absent
    from this body become uncapped. ``body.budgets = {}`` clears all
    caps. ``body.reset_counters = true`` forces every ``current_calls``
    to 0 in the persisted state (regardless of the body's values).

    422 on any negative limit/current. The JSON shape is intentionally
    flat — operators authoring policies by hand can read and write the
    same wire format the chat handler consumes.
    """
    team = await _load_team_for_caller(session, team_id, principal)

    if not body.budgets:
        team.tool_budgets = None
        return ToolBudgetsResponse(team_id=team.id, budgets={})

    new_budgets: dict[str, dict[str, int]] = {}
    for name, entry in body.budgets.items():
        # Pydantic already enforces ge=0 via Field; this is belt-and-
        # braces in case a future model edit relaxes that constraint.
        if entry.limit_calls < 0 or entry.current_calls < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "invalid_tool_budget",
                    "tool": name,
                    "error": "limit_calls and current_calls must both be >= 0",
                },
            )
        new_budgets[name] = {
            "limit_calls": entry.limit_calls,
            "current_calls": 0 if body.reset_counters else entry.current_calls,
        }
    team.tool_budgets = new_budgets

    out: dict[str, ToolBudgetEntry] = {
        name: ToolBudgetEntry(limit_calls=v["limit_calls"], current_calls=v["current_calls"])
        for name, v in new_budgets.items()
    }
    return ToolBudgetsResponse(team_id=team.id, budgets=out)


# --------------------------------------------------------------------------- #
# Reversible PII tokenization (Phase 38)                                      #
# --------------------------------------------------------------------------- #


class PIITokenizationBody(BaseModel):
    """PUT payload for ``/v1/admin/team/{id}/pii-tokenization``.

    ``enabled`` flips the master switch. ``ttl_seconds`` overrides the
    per-mapping Redis TTL; NULL falls back to the gateway default
    (3600s). Negative TTLs reject with 422."""

    enabled: bool
    ttl_seconds: int | None = None


class PIITokenizationResponse(BaseModel):
    team_id: str
    enabled: bool
    ttl_seconds: int | None


@router.get(
    "/team/{team_id}/pii-tokenization",
    response_model=PIITokenizationResponse,
)
async def get_pii_tokenization(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PIITokenizationResponse:
    """Read the team's PII tokenization config (Phase 38)."""
    team = await _load_team_for_caller(session, team_id, principal)
    return PIITokenizationResponse(
        team_id=team.id,
        enabled=team.pii_tokenization_enabled,
        ttl_seconds=team.pii_token_ttl_seconds,
    )


@router.put(
    "/team/{team_id}/pii-tokenization",
    response_model=PIITokenizationResponse,
)
async def put_pii_tokenization(
    team_id: str,
    body: PIITokenizationBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PIITokenizationResponse:
    """Set the team's PII tokenization config (Phase 38).

    422 on negative TTL. Tokenization requires BOTH this flag AND a
    rule-level ``"tokenize"`` action in the team's
    ``guardrail_policy.rule_actions`` — either one missing falls back
    to REDACT.
    """
    if body.ttl_seconds is not None and body.ttl_seconds < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "invalid_ttl",
                "error": "ttl_seconds must be >= 1 (or null to use the default)",
            },
        )

    team = await _load_team_for_caller(session, team_id, principal)
    team.pii_tokenization_enabled = body.enabled
    team.pii_token_ttl_seconds = body.ttl_seconds
    return PIITokenizationResponse(
        team_id=team.id,
        enabled=team.pii_tokenization_enabled,
        ttl_seconds=team.pii_token_ttl_seconds,
    )


# --------------------------------------------------------------------------- #
# Structured output validation + auto-retry (Phase 39)                        #
# --------------------------------------------------------------------------- #


class StructuredOutputBody(BaseModel):
    """PUT payload for ``/v1/admin/team/{id}/structured-output``.

    ``max_retries`` and ``provider_native`` map 1:1 to the team
    columns. Both are required on PUT (operators get atomic config;
    no surprising None-vs-missing semantics)."""

    max_retries: int = Field(..., ge=0)
    provider_native: bool


class StructuredOutputResponse(BaseModel):
    team_id: str
    max_retries: int
    provider_native: bool


@router.get(
    "/team/{team_id}/structured-output",
    response_model=StructuredOutputResponse,
)
async def get_structured_output(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> StructuredOutputResponse:
    """Read the team's structured-output config (Phase 39)."""
    team = await _load_team_for_caller(session, team_id, principal)
    return StructuredOutputResponse(
        team_id=team.id,
        max_retries=team.structured_output_max_retries,
        provider_native=team.structured_output_provider_native,
    )


@router.put(
    "/team/{team_id}/structured-output",
    response_model=StructuredOutputResponse,
)
async def put_structured_output(
    team_id: str,
    body: StructuredOutputBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> StructuredOutputResponse:
    """Set the team's structured-output config (Phase 39).

    422 on negative ``max_retries``. PUT semantics: both fields are
    replaced wholesale on each call.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    team.structured_output_max_retries = body.max_retries
    team.structured_output_provider_native = body.provider_native
    return StructuredOutputResponse(
        team_id=team.id,
        max_retries=team.structured_output_max_retries,
        provider_native=team.structured_output_provider_native,
    )


# --------------------------------------------------------------------------- #
# Quality regression monitoring (Phase 40)                                    #
# --------------------------------------------------------------------------- #


class QualityMonitorBody(BaseModel):
    """PUT payload for ``/v1/admin/team/{id}/quality-monitor``.

    ``sampling_rate`` is bounded [0.0, 1.0]. ``judge_model`` overrides
    the gateway-wide default — use the fqmn shape
    (e.g. ``openai/gpt-4o-mini``). Both are optional on the body so
    operators can update one without touching the other; PATCH-style
    behaviour because typical workflows toggle sampling on/off
    independently from the judge model.
    """

    sampling_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_model: str | None = None


class QualityMonitorResponse(BaseModel):
    team_id: str
    sampling_rate: float
    judge_model: str | None
    degradation_state: dict[str, Any] | None


@router.get(
    "/team/{team_id}/quality-monitor",
    response_model=QualityMonitorResponse,
)
async def get_quality_monitor(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> QualityMonitorResponse:
    """Read quality-monitor config + current degradation state (Phase 40)."""
    team = await _load_team_for_caller(session, team_id, principal)
    return QualityMonitorResponse(
        team_id=team.id,
        sampling_rate=team.quality_sampling_rate,
        judge_model=team.quality_judge_model,
        degradation_state=team.model_degradation_state,
    )


@router.put(
    "/team/{team_id}/quality-monitor",
    response_model=QualityMonitorResponse,
)
async def put_quality_monitor(
    team_id: str,
    body: QualityMonitorBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> QualityMonitorResponse:
    """Update quality-monitor config (Phase 40).

    PATCH-style: NULL fields on the body leave the existing value
    alone. Operators flip sampling on/off without overwriting the
    judge model.
    """
    team = await _load_team_for_caller(session, team_id, principal)
    if body.sampling_rate is not None:
        team.quality_sampling_rate = body.sampling_rate
    if body.judge_model is not None:
        team.quality_judge_model = body.judge_model or None
    return QualityMonitorResponse(
        team_id=team.id,
        sampling_rate=team.quality_sampling_rate,
        judge_model=team.quality_judge_model,
        degradation_state=team.model_degradation_state,
    )


# --------------------------------------------------------------------------- #
# Multi-modal image cap (Phase 41)                                            #
# --------------------------------------------------------------------------- #


class ImageCapBody(BaseModel):
    """PUT payload for ``/v1/admin/team/{id}/image-cap``.

    ``max_bytes`` NULL = clear the cap (back to unlimited). Negative
    values → 422 at validation time."""

    max_bytes: int | None = Field(default=None, ge=0)


class ImageCapResponse(BaseModel):
    team_id: str
    max_bytes: int | None


@router.get(
    "/team/{team_id}/image-cap",
    response_model=ImageCapResponse,
)
async def get_image_cap(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ImageCapResponse:
    """Read the team's max image-bytes cap (Phase 41)."""
    team = await _load_team_for_caller(session, team_id, principal)
    return ImageCapResponse(team_id=team.id, max_bytes=team.max_image_bytes)


@router.put(
    "/team/{team_id}/image-cap",
    response_model=ImageCapResponse,
)
async def put_image_cap(
    team_id: str,
    body: ImageCapBody,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ImageCapResponse:
    """Set / clear the team's image-bytes cap (Phase 41)."""
    team = await _load_team_for_caller(session, team_id, principal)
    team.max_image_bytes = body.max_bytes
    return ImageCapResponse(team_id=team.id, max_bytes=team.max_image_bytes)


# --------------------------------------------------------------------------- #
# A/B test stats — Phase 66 gap fill                                          #
# --------------------------------------------------------------------------- #
#
# Exposes the A/B-test statistical summary that was previously only available
# via the pronaos-cli abtest report command.  This endpoint computes the same
# Welch's t-test + arm aggregates in the HTTP layer so the Phase 66 UI can
# surface the results without requiring a CLI session.
#
# GET /team/{id}/ab-test   — test config + per-arm stats + t-test result


class ABTestArmStatsItem(BaseModel):
    """Per-arm aggregate from usage_records."""

    arm: str
    n: int
    mean_cost_hcents: float
    mean_total_tokens: float
    median_total_tokens: float


class ABTestTTestResult(BaseModel):
    """Welch's t-test outcome (cost-per-call: arm a vs arm b)."""

    t_statistic: float
    p_value: float
    df: float
    cohens_d: float
    ci_low: float
    ci_high: float
    significant_at_05: bool


class ABTestResponse(BaseModel):
    """Full picture of the team's current A/B test and its observed stats."""

    team_id: str
    test_id: str | None
    test_name: str | None
    started_at: str | None
    arm_a_model: str | None
    arm_b_model: str | None
    arm_a_stats: ABTestArmStatsItem | None
    arm_b_stats: ABTestArmStatsItem | None
    t_test: ABTestTTestResult | None


@router.get(
    "/team/{team_id}/ab-test",
    response_model=ABTestResponse,
)
async def get_ab_test(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ABTestResponse:
    """Return the team's current A/B-test config and per-arm statistics.

    Aggregates cost and token data from ``usage_records`` for rows where
    ``ab_arm`` is "a" or "b", then runs Welch's t-test on the mean
    cost-per-call.  The t-test result is ``null`` when either arm has
    fewer than two samples (the test is statistically undefined).

    Returns 200 with ``test_id: null`` when the team has no active test.
    """
    from pronaos.core.abtest_stats import summarise_arm, welchs_t_test

    team = await _load_team_for_caller(session, team_id, principal)
    ab_raw: dict[str, Any] | None = team.ab_test if isinstance(team.ab_test, dict) else None

    # Bail out early with a "no test" response rather than 404 — a
    # missing test is an expected state (test stopped, never started).
    if not ab_raw:
        return ABTestResponse(
            team_id=team.id,
            test_id=None,
            test_name=None,
            started_at=None,
            arm_a_model=None,
            arm_b_model=None,
            arm_a_stats=None,
            arm_b_stats=None,
            t_test=None,
        )

    test_id: str | None = ab_raw.get("id")
    test_name: str | None = ab_raw.get("name")
    started_at_raw: str | None = ab_raw.get("started_at")
    arm_a_model: str | None = (ab_raw.get("arm_a") or {}).get("model")
    arm_b_model: str | None = (ab_raw.get("arm_b") or {}).get("model")

    stmt = select(UsageRecord).where(
        UsageRecord.team_id == team_id,
        UsageRecord.ab_arm.in_(["a", "b"]),
    )
    if started_at_raw:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(UsageRecord.ts >= datetime.fromisoformat(started_at_raw))
    rows = (await session.execute(stmt)).scalars().all()

    a_costs: list[int] = [r.cost_hcents for r in rows if r.ab_arm == "a"]
    b_costs: list[int] = [r.cost_hcents for r in rows if r.ab_arm == "b"]
    a_tokens: list[int] = [r.prompt_tokens + r.completion_tokens for r in rows if r.ab_arm == "a"]
    b_tokens: list[int] = [r.prompt_tokens + r.completion_tokens for r in rows if r.ab_arm == "b"]

    arm_a = summarise_arm("a", a_costs, a_tokens)
    arm_b = summarise_arm("b", b_costs, b_tokens)
    t_result = welchs_t_test(list(arm_a.sample_costs), list(arm_b.sample_costs))

    return ABTestResponse(
        team_id=team.id,
        test_id=test_id,
        test_name=test_name,
        started_at=started_at_raw,
        arm_a_model=arm_a_model,
        arm_b_model=arm_b_model,
        arm_a_stats=ABTestArmStatsItem(
            arm="a",
            n=arm_a.n,
            mean_cost_hcents=arm_a.mean_cost_hcents,
            mean_total_tokens=arm_a.mean_total_tokens,
            median_total_tokens=arm_a.median_total_tokens,
        )
        if arm_a.n > 0
        else None,
        arm_b_stats=ABTestArmStatsItem(
            arm="b",
            n=arm_b.n,
            mean_cost_hcents=arm_b.mean_cost_hcents,
            mean_total_tokens=arm_b.mean_total_tokens,
            median_total_tokens=arm_b.median_total_tokens,
        )
        if arm_b.n > 0
        else None,
        t_test=ABTestTTestResult(
            t_statistic=t_result.t_statistic,
            p_value=t_result.p_value,
            df=t_result.df,
            cohens_d=t_result.cohens_d,
            ci_low=t_result.ci_low,
            ci_high=t_result.ci_high,
            significant_at_05=t_result.significant_at_05,
        )
        if t_result
        else None,
    )


async def _load_team_for_caller(session: AsyncSession, team_id: str, principal: Principal) -> Team:
    """Fetch ``team_id`` if and only if it belongs to ``principal.tenant_id``.

    Otherwise returns 404 — the same response whether the team doesn't
    exist or belongs to a different tenant. The two cases are
    deliberately indistinguishable so an admin can't enumerate other
    tenants' team ids by probing this endpoint."""
    team = await session.get(Team, team_id)
    if team is None or team.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team not found: {team_id}",
        )
    return team


__all__ = ["UsageItem", "UsageResponse", "UsageTotals", "router"]
