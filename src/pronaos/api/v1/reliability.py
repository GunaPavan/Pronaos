"""Reliability console + doctor endpoints (Phase 68).

Two endpoint groups under one router:

1. ``GET /v1/admin/providers`` — composes the catalog (Anthropic
   native + every CATALOG entry) with the live
   ``CircuitBreakerRegistry`` state. Operators see at a glance
   which providers are configured + which currently have an open
   breaker.

   ``POST /v1/admin/providers/{name}/reset-breaker`` force-resets
   one provider's breaker back to CLOSED. Sensitive because it
   can re-enable a still-broken provider — gated on
   ``admin:identity``.

2. ``GET /v1/admin/doctor`` runs the 14-gate doctor health check
   (Phase 61) and returns its existing
   ``DoctorReport.to_dict()`` shape. Same scope as the rest of
   the dashboard reads (``admin:usage``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import require_scope
from pronaos.config import get_settings
from pronaos.core.circuit import CircuitBreakerRegistry, CircuitState
from pronaos.core.doctor import run_doctor
from pronaos.logging import get_logger
from pronaos.providers.anthropic import _PRICING as ANTHROPIC_PRICING
from pronaos.providers.catalog import CATALOG
from pronaos.providers.registry import ProviderRegistry

log = get_logger(__name__)
router = APIRouter(tags=["admin-reliability"])


# --------------------------------------------------------------------------- #
# Providers                                                                   #
# --------------------------------------------------------------------------- #


class ProviderInfo(BaseModel):
    """One provider row for the reliability console."""

    name: str
    configured: bool
    model_count: int
    typical_p50_ms: int | None
    # ``circuit_state`` values mirror ``CircuitState``: "closed" /
    # "open" / "half_open". A provider that has never been called has
    # no breaker entry — we report "closed" since the failover layer
    # treats absence as "ready" too. (The breaker is created lazily on
    # first call.)
    circuit_state: str
    notes: str


class ProvidersResponse(BaseModel):
    items: list[ProviderInfo]


def _circuit_state_for(name: str, snapshot: dict[str, CircuitState]) -> str:
    """Return the wire-format circuit state.

    Missing-from-snapshot → "closed" because that's how the failover
    layer treats it (the breaker is created lazily on first call).
    """
    state = snapshot.get(name)
    if state is None:
        return "closed"
    return state.value


@router.get(
    "/providers",
    response_model=ProvidersResponse,
)
async def list_providers(
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
) -> ProvidersResponse:
    """Catalog + live circuit-breaker state, one row per provider.

    Anthropic (native, no catalog entry) is composed at request time.
    The CATALOG entries are filtered to *chat* providers — entries
    that only declare ``embedding_pricing`` (or only ``rerank_pricing``)
    aren't usable through the chat surface and don't get a circuit
    breaker either, so listing them here would be misleading.
    """
    registry: ProviderRegistry = request.app.state.provider_registry
    configured = set(registry.available_keys())

    circuit_registry: CircuitBreakerRegistry | None = getattr(
        request.app.state, "circuit_registry", None
    )
    snapshot: dict[str, CircuitState] = (
        circuit_registry.snapshot() if circuit_registry is not None else {}
    )

    items: list[ProviderInfo] = []

    # Anthropic native — no catalog entry, configured via anthropic_api_key.
    items.append(
        ProviderInfo(
            name="anthropic",
            configured="anthropic" in configured,
            model_count=len(ANTHROPIC_PRICING),
            typical_p50_ms=None,
            circuit_state=_circuit_state_for("anthropic", snapshot),
            notes="Native Anthropic adapter (Claude family).",
        )
    )

    # CATALOG entries — filter to providers that declare chat pricing.
    for key, entry in CATALOG.items():
        if not entry.pricing:
            # Embedding-only or rerank-only catalog entry; skip from
            # the chat-reliability view. The /v1/admin/models endpoint
            # already lists those models under their provider when
            # relevant.
            continue
        items.append(
            ProviderInfo(
                name=key,
                configured=key in configured,
                model_count=len(entry.pricing),
                typical_p50_ms=entry.typical_p50_ms,
                circuit_state=_circuit_state_for(key, snapshot),
                notes=entry.notes,
            )
        )

    # Sort: configured first (operators care most about live ones),
    # then alphabetical inside each bucket.
    items.sort(key=lambda p: (0 if p.configured else 1, p.name))

    return ProvidersResponse(items=items)


class ResetBreakerResponse(BaseModel):
    name: str
    circuit_state: str


@router.post(
    "/providers/{name}/reset-breaker",
    response_model=ResetBreakerResponse,
)
async def reset_breaker(
    name: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
) -> ResetBreakerResponse:
    """Force a provider's circuit breaker back to CLOSED.

    The breaker auto-recovers via HALF_OPEN probing (Phase 25); this
    endpoint exists for the rare case where the operator KNOWS the
    upstream is healthy again and doesn't want to wait for the
    recovery timer. Calling this while the upstream is still broken
    just means the next call trips it again — no permanent damage.

    Validates that ``name`` is a recognised provider so a typo doesn't
    silently spawn a new empty breaker.
    """
    if name != "anthropic" and name not in CATALOG:
        raise HTTPException(
            status_code=404,
            detail={"type": "provider_not_found", "name": name},
        )
    circuit_registry: CircuitBreakerRegistry | None = getattr(
        request.app.state, "circuit_registry", None
    )
    if circuit_registry is None:
        raise HTTPException(
            status_code=503,
            detail={"type": "circuit_registry_unavailable"},
        )
    breaker = circuit_registry.get(name)
    breaker.record_success()  # the existing way the system resets to CLOSED
    log.info("admin.reliability.breaker_reset", provider=name)
    return ResetBreakerResponse(name=name, circuit_state=breaker.state.value)


# --------------------------------------------------------------------------- #
# Doctor                                                                      #
# --------------------------------------------------------------------------- #


class DoctorGate(BaseModel):
    name: str
    verdict: str
    detail: str


class DoctorSummary(BaseModel):
    total: int
    passed: int
    failed: int
    warn: int
    skip: int


class DoctorResponse(BaseModel):
    gates: list[DoctorGate]
    summary: DoctorSummary
    has_fail: bool
    has_warn: bool


@router.get(
    "/doctor",
    response_model=DoctorResponse,
)
async def get_doctor_report(
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
) -> DoctorResponse:
    """Run the 14-gate doctor health check and return the report.

    Mirrors the ``pronaos-cli doctor`` CLI command's output (Phase
    61) but wired into the admin REST surface so the UI can render
    grouped gate results. ``probe_providers=False`` by default —
    the slower per-provider HTTP probe is operator-opt-in to avoid
    making this endpoint long-running.
    """
    settings = get_settings()
    report = await run_doctor(settings, probe_providers=False)

    gates = [
        DoctorGate(name=g.name, verdict=g.verdict.value, detail=g.detail) for g in report.gates
    ]
    # The DoctorReport's existing .to_dict() summary uses "pass" which
    # is a Python keyword in some toolchains; we rename to "passed"
    # on the wire to stay Pydantic-friendly for the response model.
    summary = DoctorSummary(
        total=len(report.gates),
        passed=sum(1 for g in gates if g.verdict == "PASS"),
        failed=sum(1 for g in gates if g.verdict == "FAIL"),
        warn=sum(1 for g in gates if g.verdict == "WARN"),
        skip=sum(1 for g in gates if g.verdict == "SKIP"),
    )
    log.info(
        "admin.reliability.doctor",
        total=summary.total,
        passed=summary.passed,
        failed=summary.failed,
        warn=summary.warn,
        has_fail=report.has_fail(),
    )
    return DoctorResponse(
        gates=gates,
        summary=summary,
        has_fail=report.has_fail(),
        has_warn=report.has_warn(),
    )
