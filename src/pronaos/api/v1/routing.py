"""Composed routing config endpoint (Phase 66).

The Phase 21-57 admin surface added separate endpoints for each
routing-related per-team field — ``/routing-strategy``,
``/tool-use-scores``, ``/prompt-cache-config``, ``/reasoning-config``,
``/quality-monitor``, allowlist, etc. That made sense incrementally
(each phase shipped one piece) but it left the Phase 66 UI with
seven different GETs to load the full picture and seven different
PUTs to save edits.

This module composes them. ``GET /v1/admin/routing/{team_id}``
returns every routing-related column on the Team row in one shape;
``PUT`` accepts a partial body (PATCH-style: ``null`` clears,
omitted is unchanged) and validates the strategy enum + the score-
dict structure before write.

Scope model
-----------
GET uses ``admin:usage`` — same as the rest of the dashboard reads.
PUT uses ``admin:identity`` — routing changes are operationally
sensitive (a wrong strategy can route traffic to the wrong tier).
The legacy per-config endpoints in admin.py still use ``admin:usage``
for writes — those are kept for back-compat; new clients (the
Phase 66 UI included) should use this composed endpoint.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.core.scorer import RoutingStrategy
from pronaos.db.models import Team
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-routing"])


# --------------------------------------------------------------------------- #
# Response shape                                                              #
# --------------------------------------------------------------------------- #


class RoutingConfigResponse(BaseModel):
    """Every per-team routing-related column on the Team row.

    Mirrors the columns in ``db.models.Team`` named in Phases 21, 24,
    46, 47, and 57. Every field is nullable — NULL means "not set;
    fall back to gateway default."
    """

    team_id: str
    routing_strategy: str | None
    allowed_models: list[str] | None

    # Phase 24 — quality-aware routing
    quality_threshold: float | None
    quality_scores: dict[str, Any] | None

    # Phase 46 — tool-use-aware routing
    tool_use_threshold: float | None
    tool_use_scores: dict[str, Any] | None

    # Phase 47 — prompt-cache-aware routing
    prompt_cache_min_samples: int | None
    prompt_cache_min_hit_rate: float | None

    # Phase 57 — reasoning-aware routing
    reasoning_aware_min_samples: int | None
    reasoning_aware_max_ratio: float | None


# --------------------------------------------------------------------------- #
# Update body                                                                 #
# --------------------------------------------------------------------------- #


def _validate_strategy(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return RoutingStrategy(value.strip().lower()).value
    except ValueError as e:
        valid = ", ".join(s.value for s in RoutingStrategy)
        raise ValueError(
            f"invalid routing_strategy {value!r}; must be one of: {valid}"
        ) from e


def _validate_scores_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Score dicts must be ``{fqmn: {score: float, ...}}``.

    Outer keys are arbitrary fqmns (we don't validate against the catalog
    — operators may seed scores ahead of catalog updates). Inner values
    must be dicts carrying at minimum a numeric ``score`` field; extra
    keys (n_samples, source_eval_id, ts) are preserved verbatim.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("scores must be a dict keyed by fqmn")
    cleaned: dict[str, Any] = {}
    for fqmn, payload in value.items():
        if not isinstance(fqmn, str) or not fqmn:
            raise ValueError(f"score key must be a non-empty fqmn string, got {fqmn!r}")
        if not isinstance(payload, dict):
            raise ValueError(
                f"score value for {fqmn!r} must be an object, got {type(payload).__name__}"
            )
        if "score" not in payload:
            raise ValueError(f"score for {fqmn!r} missing required 'score' field")
        score = payload["score"]
        if not isinstance(score, (int, float)):
            raise ValueError(f"score for {fqmn!r} must be numeric, got {type(score).__name__}")
        cleaned[fqmn] = payload
    return cleaned


class RoutingConfigUpdate(BaseModel):
    """PATCH-style update. Every field is optional.

    Pydantic semantics: a field omitted from the JSON body is NOT in
    ``model_fields_set`` — the handler treats that as "leave the
    column unchanged." A field explicitly set to ``null`` IS in
    ``model_fields_set`` — the handler treats that as "clear the
    column" (back to NULL = gateway default).
    """

    routing_strategy: str | None = None
    allowed_models: list[str] | None = None
    quality_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    quality_scores: dict[str, Any] | None = None
    tool_use_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    tool_use_scores: dict[str, Any] | None = None
    prompt_cache_min_samples: int | None = Field(default=None, ge=0)
    prompt_cache_min_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning_aware_min_samples: int | None = Field(default=None, ge=0)
    reasoning_aware_max_ratio: float | None = Field(default=None, ge=0.0)

    @field_validator("routing_strategy")
    @classmethod
    def _check_strategy(cls, v: str | None) -> str | None:
        return _validate_strategy(v)

    @field_validator("quality_scores", "tool_use_scores")
    @classmethod
    def _check_scores(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_scores_dict(v)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _team_to_response(team: Team) -> RoutingConfigResponse:
    return RoutingConfigResponse(
        team_id=team.id,
        routing_strategy=team.routing_strategy,
        allowed_models=team.allowed_models,
        quality_threshold=team.quality_threshold,
        quality_scores=team.quality_scores,
        tool_use_threshold=team.tool_use_threshold,
        tool_use_scores=team.tool_use_scores,
        prompt_cache_min_samples=team.prompt_cache_min_samples,
        prompt_cache_min_hit_rate=team.prompt_cache_min_hit_rate,
        reasoning_aware_min_samples=team.reasoning_aware_min_samples,
        reasoning_aware_max_ratio=team.reasoning_aware_max_ratio,
    )


async def _load_team(session: AsyncSession, team_id: str) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "team_id": team_id},
        )
    return team


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.get(
    "/routing/{team_id}",
    response_model=RoutingConfigResponse,
)
async def get_routing(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingConfigResponse:
    """Return the composed routing config for a team.

    Every nullable column on the Team row appears in the response —
    NULL means "use the gateway default for this strategy." The
    Phase 66 UI binds the response shape directly into its controls.
    """
    team = await _load_team(session, team_id)
    return _team_to_response(team)


@router.put(
    "/routing/{team_id}",
    response_model=RoutingConfigResponse,
)
async def put_routing(
    team_id: str,
    body: RoutingConfigUpdate,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RoutingConfigResponse:
    """Replace any subset of the team's routing config.

    PATCH semantics: only fields present in the JSON body are written.
    Passing ``null`` explicitly clears the column; omitting it leaves
    the current value intact. Validation:
      - ``routing_strategy`` must be a value of ``RoutingStrategy``
        (validated in the body's field_validator before this handler
        sees it — invalid input → 422)
      - score dicts must be ``{fqmn: {score, ...}}`` with a numeric
        ``score``; the rest of the per-fqmn payload is preserved
      - thresholds bounded 0..1; sample counts non-negative
    """
    team = await _load_team(session, team_id)
    touched = body.model_fields_set
    changes: dict[str, Any] = {}

    if "routing_strategy" in touched:
        team.routing_strategy = body.routing_strategy
        changes["routing_strategy"] = body.routing_strategy
    if "allowed_models" in touched:
        team.allowed_models = body.allowed_models
        changes["allowed_models"] = body.allowed_models
    if "quality_threshold" in touched:
        team.quality_threshold = body.quality_threshold
        changes["quality_threshold"] = body.quality_threshold
    if "quality_scores" in touched:
        team.quality_scores = body.quality_scores
        changes["quality_scores_keys"] = (
            list(body.quality_scores.keys()) if body.quality_scores else None
        )
    if "tool_use_threshold" in touched:
        team.tool_use_threshold = body.tool_use_threshold
        changes["tool_use_threshold"] = body.tool_use_threshold
    if "tool_use_scores" in touched:
        team.tool_use_scores = body.tool_use_scores
        changes["tool_use_scores_keys"] = (
            list(body.tool_use_scores.keys()) if body.tool_use_scores else None
        )
    if "prompt_cache_min_samples" in touched:
        team.prompt_cache_min_samples = body.prompt_cache_min_samples
        changes["prompt_cache_min_samples"] = body.prompt_cache_min_samples
    if "prompt_cache_min_hit_rate" in touched:
        team.prompt_cache_min_hit_rate = body.prompt_cache_min_hit_rate
        changes["prompt_cache_min_hit_rate"] = body.prompt_cache_min_hit_rate
    if "reasoning_aware_min_samples" in touched:
        team.reasoning_aware_min_samples = body.reasoning_aware_min_samples
        changes["reasoning_aware_min_samples"] = body.reasoning_aware_min_samples
    if "reasoning_aware_max_ratio" in touched:
        team.reasoning_aware_max_ratio = body.reasoning_aware_max_ratio
        changes["reasoning_aware_max_ratio"] = body.reasoning_aware_max_ratio

    await session.commit()
    await session.refresh(team)

    log.info(
        "admin.routing.updated",
        team_id=team_id,
        changes=changes,
    )
    return _team_to_response(team)
