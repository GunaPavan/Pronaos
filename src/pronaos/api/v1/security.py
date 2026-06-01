"""Composed security config + audit log endpoints (Phase 67).

Two endpoint groups under one router:

1. ``GET/PUT /v1/admin/security/{team_id}`` — composed per-team
   security config (guardrail policy + PII tokenization flags). The
   UI binds this directly into its policy editor; PATCH semantics
   match the routing console (Phase 66).

2. ``GET /v1/admin/audit/{tenant_id}`` (list, paginated) and
   ``POST /v1/admin/audit/{tenant_id}/verify`` (run the hash-chain
   verifier and return the verdict). The audit log is the only
   tenant-scoped (not team-scoped) admin surface — chains are
   per-tenant so a verify pass walks the whole tenant in one shot.

Scope model
-----------
GETs (security + audit list + audit verify) all use ``admin:usage``.
PUT on security uses ``admin:identity`` — the policy controls block /
redact / tokenize behavior on every chat call, which is sensitive
enough to gate behind the write scope (same posture as the routing
console).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.audit.verifier import AuditVerifier
from pronaos.auth.api_keys import Principal
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import AuditRecord, Team, Tenant
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-security"])


# --------------------------------------------------------------------------- #
# Known rule + action vocabulary                                              #
# --------------------------------------------------------------------------- #

# Rule IDs the engine knows. The UI uses this list to render the
# editor; the backend validates the policy body against it. Keeping
# the list HERE rather than importing from each detector module keeps
# the endpoint's surface stable when a new optional detector lands —
# operators can still write the rule into policy and the engine just
# ignores it until the detector is wired up.
_KNOWN_RULE_IDS = {
    "pii.email",
    "pii.phone",
    "pii.ssn",
    "pii.ipv4",
    "injection",
    "presidio",
    "llama_guard",
}

_VALID_ACTIONS = {"block", "redact", "tokenize", "log_only"}


# --------------------------------------------------------------------------- #
# Security config                                                             #
# --------------------------------------------------------------------------- #


class SecurityConfigResponse(BaseModel):
    """Composed per-team security config.

    All fields are nullable / defaulted — NULL guardrail_policy means
    "use gateway defaults"; pii_tokenization_enabled defaults to
    False (master switch off); pii_token_ttl_seconds NULL means
    "use the engine default (typically 7d)".
    """

    team_id: str
    guardrail_policy: dict[str, Any] | None
    pii_tokenization_enabled: bool
    pii_token_ttl_seconds: int | None

    # Static, but echoed back so the UI doesn't hard-code the
    # vocabulary. Operators adding a new detector update _KNOWN_RULE_IDS
    # here and the UI picks it up on the next page load.
    known_rule_ids: list[str]
    valid_actions: list[str]


def _validate_guardrail_policy(
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate the guardrail_policy JSON before it touches the DB.

    Shape (per src/pronaos/db/models.py Team.guardrail_policy):
      {
        "disabled_rules": ["pii.ipv4", ...],
        "rule_actions": {"pii.email": "redact", ...},
        # Optional sub-blocks for ML detectors (Phase 22, 44).
        "presidio": {"enabled": true, ...},
        "llama_guard": {"enabled": true, ...},
      }
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("guardrail_policy must be an object")

    if "disabled_rules" in value:
        disabled = value["disabled_rules"]
        if not isinstance(disabled, list) or not all(
            isinstance(r, str) for r in disabled
        ):
            raise ValueError("disabled_rules must be a list of strings")

    if "rule_actions" in value:
        actions = value["rule_actions"]
        if not isinstance(actions, dict):
            raise ValueError("rule_actions must be an object")
        for rule_id, action in actions.items():
            if not isinstance(rule_id, str):
                raise ValueError(
                    f"rule_actions key must be a string, got {type(rule_id).__name__}"
                )
            if not isinstance(action, str) or action not in _VALID_ACTIONS:
                raise ValueError(
                    f"rule_actions[{rule_id!r}] must be one of {sorted(_VALID_ACTIONS)}; "
                    f"got {action!r}"
                )

    # Presidio + Llama Guard sub-blocks are opaque to this endpoint —
    # the detector modules own their own shape. Pass through verbatim.
    return value


class SecurityConfigUpdate(BaseModel):
    """PATCH-style update. Omitted fields are unchanged; explicit
    ``null`` clears (matches routing / budgets conventions)."""

    guardrail_policy: dict[str, Any] | None = None
    pii_tokenization_enabled: bool | None = None
    pii_token_ttl_seconds: int | None = Field(default=None, ge=0)

    @field_validator("guardrail_policy")
    @classmethod
    def _check_policy(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_guardrail_policy(v)


def _team_to_security_response(team: Team) -> SecurityConfigResponse:
    return SecurityConfigResponse(
        team_id=team.id,
        guardrail_policy=team.guardrail_policy,
        pii_tokenization_enabled=team.pii_tokenization_enabled,
        pii_token_ttl_seconds=team.pii_token_ttl_seconds,
        known_rule_ids=sorted(_KNOWN_RULE_IDS),
        valid_actions=sorted(_VALID_ACTIONS),
    )


async def _load_team(session: AsyncSession, team_id: str) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "team_id": team_id},
        )
    return team


@router.get(
    "/security/{team_id}",
    response_model=SecurityConfigResponse,
)
async def get_security(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SecurityConfigResponse:
    """Return the composed security config for a team.

    Includes the live config + the static vocabulary the UI needs
    (known_rule_ids, valid_actions) to render a useful editor.
    """
    team = await _load_team(session, team_id)
    return _team_to_security_response(team)


@router.put(
    "/security/{team_id}",
    response_model=SecurityConfigResponse,
)
async def put_security(
    team_id: str,
    body: SecurityConfigUpdate,
    principal: Annotated[Principal, Depends(require_scope("admin:identity"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SecurityConfigResponse:
    """Replace any subset of the team's security config.

    PATCH semantics — see SecurityConfigUpdate docstring. Policy
    shape validated server-side; invalid input → 422 before the
    write hits the DB.
    """
    team = await _load_team(session, team_id)
    touched = body.model_fields_set
    changes: dict[str, Any] = {}

    if "guardrail_policy" in touched:
        team.guardrail_policy = body.guardrail_policy
        changes["guardrail_policy_keys"] = (
            list(body.guardrail_policy.keys()) if body.guardrail_policy else None
        )
    if "pii_tokenization_enabled" in touched:
        # bool is non-nullable on the model, so we coerce None → False.
        # Operators wanting "default off" should pass False explicitly;
        # null is treated identically.
        team.pii_tokenization_enabled = bool(body.pii_tokenization_enabled)
        changes["pii_tokenization_enabled"] = team.pii_tokenization_enabled
    if "pii_token_ttl_seconds" in touched:
        team.pii_token_ttl_seconds = body.pii_token_ttl_seconds
        changes["pii_token_ttl_seconds"] = body.pii_token_ttl_seconds

    await session.commit()
    await session.refresh(team)

    log.info("admin.security.updated", team_id=team_id, changes=changes)
    return _team_to_security_response(team)


# --------------------------------------------------------------------------- #
# Audit log                                                                   #
# --------------------------------------------------------------------------- #


class AuditRecordItem(BaseModel):
    """One audit-record row for the list endpoint.

    The full audit_records row carries 11+ fields; we surface every
    one of the chain-relevant fields so the UI can render the chain
    + drill-down to a specific record. We do NOT surface request_hash
    / response_hash bodies — they're just hashes (SHA-256 hex), small,
    and useful for tamper-evidence display.
    """

    id: str
    ts: str  # ISO 8601
    tenant_id: str
    team_id: str
    key_id: str
    provider: str
    model: str
    request_hash: str
    response_hash: str
    prev_hash: str
    this_hash: str
    request_id: str | None


class AuditListResponse(BaseModel):
    items: list[AuditRecordItem]
    total: int
    limit: int
    offset: int


class ChainBreakItem(BaseModel):
    """One chain-break record from the verifier.

    Mirrors ``audit.verifier.ChainBreak`` but as a Pydantic shape so
    it serialises cleanly through FastAPI.
    """

    record_id: str
    ts_iso: str
    reason: str  # "hash_mismatch" | "prev_hash_mismatch" | "missing_predecessor"
    expected_hash: str
    actual_hash: str


class AuditVerifyResponse(BaseModel):
    tenant_id: str
    is_intact: bool
    total_records: int
    verified_records: int
    breaks: list[ChainBreakItem]


async def _ensure_tenant_exists(session: AsyncSession, tenant_id: str) -> None:
    """404 early on unknown tenant. The verifier walks audit_records
    directly (which has soft FKs); without this guard a typo'd
    tenant_id would silently return an empty list."""
    exists = await session.get(Tenant, tenant_id)
    if exists is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "tenant_not_found", "tenant_id": tenant_id},
        )


@router.get(
    "/audit/{tenant_id}",
    response_model=AuditListResponse,
)
async def list_audit_records(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    team_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    """Paginated audit-record list for a tenant.

    Ordered oldest-first so the chain reads top-to-bottom in the UI
    (prev_hash on row N matches this_hash on row N-1). Optional
    team_id filter for cases where the operator only wants one team's
    activity within the tenant.
    """
    await _ensure_tenant_exists(session, tenant_id)

    base = select(AuditRecord).where(AuditRecord.tenant_id == tenant_id)
    if team_id:
        base = base.where(AuditRecord.team_id == team_id)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (
        (
            await session.execute(
                base.order_by(AuditRecord.ts.asc(), AuditRecord.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    items = [
        AuditRecordItem(
            id=r.id,
            ts=r.ts.isoformat(),
            tenant_id=r.tenant_id,
            team_id=r.team_id,
            key_id=r.key_id,
            provider=r.provider,
            model=r.model,
            request_hash=r.request_hash,
            response_hash=r.response_hash,
            prev_hash=r.prev_hash,
            this_hash=r.this_hash,
            request_id=r.request_id,
        )
        for r in rows
    ]
    return AuditListResponse(items=items, total=total or 0, limit=limit, offset=offset)


@router.post(
    "/audit/{tenant_id}/verify",
    response_model=AuditVerifyResponse,
)
async def verify_audit_chain(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope("admin:usage"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuditVerifyResponse:
    """Walk the tenant's audit chain and report tamper points.

    Wraps :class:`audit.verifier.AuditVerifier`. The hash-chain walk
    is O(N) — sub-second on a million-record chain. ``admin:usage``
    scope (read-only verification; the verifier doesn't mutate the DB).
    """
    await _ensure_tenant_exists(session, tenant_id)
    verifier = AuditVerifier()
    result = await verifier.verify(session, tenant_id)
    log.info(
        "admin.audit.verified",
        tenant_id=tenant_id,
        total=result.total_records,
        verified=result.verified_records,
        breaks=len(result.breaks),
        is_intact=result.is_intact,
    )
    return AuditVerifyResponse(
        tenant_id=result.tenant_id,
        is_intact=result.is_intact,
        total_records=result.total_records,
        verified_records=result.verified_records,
        breaks=[
            ChainBreakItem(
                record_id=b.record_id,
                ts_iso=b.ts_iso,
                reason=b.reason,
                expected_hash=b.expected_hash,
                actual_hash=b.actual_hash,
            )
            for b in result.breaks
        ],
    )
