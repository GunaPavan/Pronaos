"""Model catalog endpoint (Phase 65).

The Phase 65 playground UI needs a list of models the operator's
team can target: which providers are configured, which fully-
qualified model names are in the team's allowlist, what each
model's pricing and capabilities are.

Until Phase 65, the only way to enumerate models was reading the
Python catalog directly. The playground would have had to
hard-code its model list and silently drift from the gateway's
actual capabilities. This endpoint closes the loop by surfacing
the same data the chat handler and router consult:

- The native ``anthropic`` provider's pricing dict.
- Every entry in ``providers.catalog.CATALOG`` that declares a
  chat ``pricing`` block (skipping embedding-only or rerank-only
  catalog entries).

For each model the response reports:

- ``provider_configured`` — does the gateway have the API key /
  service-account secret needed to actually call this provider?
  Mirrors :meth:`ProviderRegistry.available_keys`.
- ``allowed`` — is this fqmn in the team's ``allowed_models``
  whitelist? ``None``-whitelist means "all models allowed";
  reported as ``allowed=True`` for every row.

Scope: ``admin:usage``. Same posture as the FinOps dashboard
endpoints — a key that can see the dashboard can see what models
are routable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import Team
from pronaos.logging import get_logger
from pronaos.providers.anthropic import _PRICING as ANTHROPIC_PRICING
from pronaos.providers.catalog import CATALOG, ModelCapabilities
from pronaos.providers.registry import ProviderRegistry

log = get_logger(__name__)
router = APIRouter(tags=["admin-models"])


class ModelInfo(BaseModel):
    """Per-model row returned by ``GET /v1/admin/models``."""

    fqmn: str  # provider/model, e.g. "groq/llama-3.3-70b-versatile"
    provider: str
    input_hcents_per_mtok: int
    output_hcents_per_mtok: int
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool
    max_context_tokens: int
    provider_configured: bool
    allowed: bool


class ModelsResponse(BaseModel):
    """Catalog of routable chat models for the calling principal's team."""

    items: list[ModelInfo]


# Anthropic models support tools + streaming + vision uniformly today.
# The native adapter doesn't surface this as a separate matrix, so we
# pin the values here — keep in sync with anthropic.py if a future
# generation changes the surface.
_ANTHROPIC_CAPS = ModelCapabilities(
    supports_tools=True,
    supports_streaming=True,
    supports_vision=True,
    max_context_tokens=200_000,
)


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelsResponse:
    """Enumerate routable chat models, annotated with this team's
    allowlist membership + this gateway's provider-configured state.

    The list is deliberately broad — every model the gateway *could*
    route to, not just the ones currently allowed. The UI uses the
    ``allowed`` + ``provider_configured`` flags to dim or hide rows;
    surfacing the full set keeps the source of truth (the catalog)
    visible to operators without a separate /catalog endpoint.
    """
    # 1) Load the team to read its allowlist. None = "no allowlist set
    #    = every model is allowed".
    team = await db.get(Team, principal.team_id)
    allowed_set: set[str] | None = None
    if team and team.allowed_models:
        allowed_set = set(team.allowed_models)

    # 2) Configured-provider set comes from the registry's existing
    #    available_keys() check — same gate the chat handler uses to
    #    refuse calls when a key/secret isn't present.
    registry: ProviderRegistry = request.app.state.provider_registry
    configured = set(registry.available_keys())

    items: list[ModelInfo] = []

    # 3) Anthropic native — hard-coded pricing dict; no catalog entry.
    for model_name, anth_price in ANTHROPIC_PRICING.items():
        fqmn = f"anthropic/{model_name}"
        items.append(
            ModelInfo(
                fqmn=fqmn,
                provider="anthropic",
                input_hcents_per_mtok=anth_price.input_hcents_per_mtok,
                output_hcents_per_mtok=anth_price.output_hcents_per_mtok,
                supports_tools=_ANTHROPIC_CAPS.supports_tools,
                supports_streaming=_ANTHROPIC_CAPS.supports_streaming,
                supports_vision=_ANTHROPIC_CAPS.supports_vision,
                max_context_tokens=_ANTHROPIC_CAPS.max_context_tokens,
                provider_configured="anthropic" in configured,
                allowed=allowed_set is None or fqmn in allowed_set,
            )
        )

    # 4) Catalog providers. Iterate the chat ``pricing`` dict; skip
    #    entries that only carry embedding_pricing / rerank_pricing.
    default_caps = ModelCapabilities()
    for provider_key, entry in CATALOG.items():
        for model_name, cat_price in entry.pricing.items():
            fqmn = f"{provider_key}/{model_name}"
            caps = entry.capabilities.get(model_name, default_caps)
            items.append(
                ModelInfo(
                    fqmn=fqmn,
                    provider=provider_key,
                    input_hcents_per_mtok=cat_price.input_hcents_per_mtok,
                    output_hcents_per_mtok=cat_price.output_hcents_per_mtok,
                    supports_tools=caps.supports_tools,
                    supports_streaming=caps.supports_streaming,
                    supports_vision=caps.supports_vision,
                    max_context_tokens=caps.max_context_tokens,
                    provider_configured=provider_key in configured,
                    allowed=allowed_set is None or fqmn in allowed_set,
                )
            )

    # 5) Sort for stable UI rendering: configured-and-allowed first,
    #    then allowed-but-unconfigured, then disallowed. Inside each
    #    bucket, alphabetical by fqmn so the dropdown reads sensibly.
    def _rank(m: ModelInfo) -> tuple[int, str]:
        if m.allowed and m.provider_configured:
            tier = 0
        elif m.allowed:
            tier = 1
        else:
            tier = 2
        return (tier, m.fqmn)

    items.sort(key=_rank)

    log.info(
        "admin.models.list",
        team_id=principal.team_id,
        model_count=len(items),
        configured_providers=sorted(configured),
        has_allowlist=allowed_set is not None,
    )
    return ModelsResponse(items=items)
