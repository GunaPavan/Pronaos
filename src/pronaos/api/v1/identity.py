"""Identity REST surface — tenant / team / API key CRUD (Phase 63).

The CLI has shipped tenant + team + key management since Phase 4
(``pronaos-cli tenant create``, ``team create``, ``key issue``,
``key revoke``). Phase 63 surfaces the same operations over REST so
the admin UI (and any other automation that prefers HTTP to a
subprocess) can manage identity without shelling out to the CLI.

Scope model
-----------
Identity is more sensitive than usage queries — creating a key, in
particular, is functionally equivalent to printing money for the
team's quota. So we add a NEW scope ``admin:identity`` and require it
on every endpoint in this module. The existing ``admin:usage`` scope
is unchanged — keys with only ``admin:usage`` keep working for the
``/v1/admin/usage`` + per-team config endpoints.

Conventions
-----------
- IDs are server-generated (uuid7-ish via ``_new_id``); never accept
  client-supplied IDs.
- Soft delete for keys (``revoked_at``); hard delete for tenants +
  teams. Tenant + team rows are operator-managed; key revocation
  needs to preserve the audit trail.
- Generated API key bodies are returned in the POST response exactly
  ONCE. Subsequent GETs return only the prefix + label + scopes +
  status. This matches the CLI's ``key issue`` invariant + every
  industry-standard secret-issuance UX.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import (
    Principal,
    generate_api_key,
    hash_key,
)
from pronaos.auth.deps import get_db, require_scope
from pronaos.db.models import ApiKey, Team, Tenant
from pronaos.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["admin-identity"])

# Scope every identity write requires. Reads accept either this OR
# the older admin:usage scope so existing dashboards keep working.
SCOPE_IDENTITY = "admin:identity"


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


# --------------------------------------------------------------------------- #
# Pydantic schemas                                                            #
# --------------------------------------------------------------------------- #


class TenantBody(BaseModel):
    """POST /tenants body — create a tenant."""

    name: str = Field(..., min_length=1, max_length=255)


class TenantUpdateBody(BaseModel):
    """PATCH /tenants/{id} body.

    All fields are optional — omitted means "leave unchanged".
    ``oidc_subject=None`` explicitly clears the OIDC binding for
    the tenant (after Phase 71, operators can manage it from the
    admin UI without the CLI).
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    # Phase 71 — expose oidc_subject via PATCH so the /settings UI
    # can set/clear it without shell access. An empty string is
    # treated the same as None (clear).
    oidc_subject: str | None = Field(
        default=None,
        description=(
            "OIDC subject claim for SSO auth (Phase 26). "
            "null clears the OIDC binding; omitted leaves it unchanged."
        ),
    )


class TenantResponse(BaseModel):
    id: str
    name: str
    created_at: int
    webhook_url: str | None
    oidc_subject: str | None


class TeamBody(BaseModel):
    """POST /teams body."""

    tenant_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)


class TeamUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class TeamResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    created_at: int


class KeyGenerateBody(BaseModel):
    """POST /keys body — generate a new key for a team."""

    team_id: str = Field(..., min_length=1, max_length=64)
    label: str = Field(default="", max_length=255)
    scopes: list[str] = Field(default_factory=lambda: ["chat:write"])
    env: Literal["live", "test"] = "live"


class KeyResponse(BaseModel):
    """Returned on GET — secret is never included."""

    id: str
    team_id: str
    prefix: str
    label: str
    scopes: list[str]
    status: Literal["active", "revoked"]
    created_at: int
    revoked_at: int | None
    last_used_at: int | None


class KeyGenerateResponse(BaseModel):
    """Returned on POST /keys — includes the FULL key exactly once.

    Subsequent reads return ``KeyResponse`` without the secret. The
    caller MUST persist ``api_key`` immediately; the gateway has no
    way to recover it after this response."""

    id: str
    team_id: str
    prefix: str
    label: str
    scopes: list[str]
    status: Literal["active"]
    created_at: int
    api_key: str


# --------------------------------------------------------------------------- #
# Tenants                                                                     #
# --------------------------------------------------------------------------- #


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(description="Substring filter on name")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TenantResponse]:
    _ = principal  # required by Depends but not used directly
    stmt = select(Tenant).order_by(Tenant.created_at.desc()).limit(limit)
    if q:
        stmt = stmt.where(Tenant.name.ilike(f"%{q}%"))
    rows = (await session.execute(stmt)).scalars().all()
    return [_tenant_to_response(t) for t in rows]


@router.post("/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: TenantBody,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    _ = principal
    tenant = Tenant(name=body.name)
    session.add(tenant)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"type": "tenant_conflict", "hint": str(e.orig)},
        ) from e
    await session.commit()
    await session.refresh(tenant)
    log.info("identity.tenant.created", id=tenant.id, name=tenant.name)
    return _tenant_to_response(tenant)


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    _ = principal
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "tenant_not_found", "hint": tenant_id},
        )
    return _tenant_to_response(tenant)


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdateBody,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    _ = principal
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "tenant_not_found", "hint": tenant_id},
        )
    touched = body.model_fields_set
    if "name" in touched and body.name is not None:
        tenant.name = body.name
    if "oidc_subject" in touched:
        # Empty string treated as clear (same as None).
        tenant.oidc_subject = body.oidc_subject or None
    await session.commit()
    await session.refresh(tenant)
    log.info("identity.tenant.updated", id=tenant.id)
    return _tenant_to_response(tenant)


@router.delete("/tenants/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    _ = principal
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "tenant_not_found", "hint": tenant_id},
        )
    await session.delete(tenant)
    await session.commit()
    log.info("identity.tenant.deleted", id=tenant_id)
    return Response(status_code=204)


def _tenant_to_response(t: Tenant) -> TenantResponse:
    return TenantResponse(
        id=t.id,
        name=t.name,
        created_at=_epoch(t.created_at) or 0,
        webhook_url=t.webhook_url,
        oidc_subject=t.oidc_subject,
    )


# --------------------------------------------------------------------------- #
# Teams                                                                       #
# --------------------------------------------------------------------------- #


@router.get("/teams", response_model=list[TeamResponse])
async def list_teams(
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[str | None, Query(description="Filter to this tenant")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TeamResponse]:
    _ = principal
    stmt = select(Team).order_by(Team.created_at.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(Team.tenant_id == tenant_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_team_to_response(t) for t in rows]


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    body: TeamBody,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TeamResponse:
    _ = principal
    # FK check up front so we return 422 with a helpful message rather
    # than the SQLAlchemy IntegrityError surfacing as 500.
    tenant = await session.get(Tenant, body.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "tenant_not_found",
                "hint": f"tenant_id={body.tenant_id!r} does not exist",
            },
        )
    team = Team(tenant_id=body.tenant_id, name=body.name)
    session.add(team)
    await session.commit()
    await session.refresh(team)
    log.info("identity.team.created", id=team.id, tenant_id=team.tenant_id)
    return _team_to_response(team)


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TeamResponse:
    _ = principal
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "hint": team_id},
        )
    return _team_to_response(team)


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    body: TeamUpdateBody,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TeamResponse:
    _ = principal
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "hint": team_id},
        )
    if body.name is not None:
        team.name = body.name
    await session.commit()
    await session.refresh(team)
    log.info("identity.team.updated", id=team.id)
    return _team_to_response(team)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    _ = principal
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "team_not_found", "hint": team_id},
        )
    await session.delete(team)
    await session.commit()
    log.info("identity.team.deleted", id=team_id)
    return Response(status_code=204)


def _team_to_response(t: Team) -> TeamResponse:
    return TeamResponse(
        id=t.id,
        tenant_id=t.tenant_id,
        name=t.name,
        created_at=_epoch(t.created_at) or 0,
    )


# --------------------------------------------------------------------------- #
# API keys                                                                    #
# --------------------------------------------------------------------------- #


@router.get("/keys", response_model=list[KeyResponse])
async def list_keys(
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
    team_id: Annotated[str | None, Query(description="Filter to this team")] = None,
    include_revoked: Annotated[
        bool,
        Query(description="When false (default), revoked keys are hidden"),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[KeyResponse]:
    _ = principal
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).limit(limit)
    if team_id:
        stmt = stmt.where(ApiKey.team_id == team_id)
    if not include_revoked:
        stmt = stmt.where(ApiKey.revoked_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    return [_key_to_response(k) for k in rows]


@router.post("/keys", response_model=KeyGenerateResponse, status_code=201)
async def generate_key(
    body: KeyGenerateBody,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> KeyGenerateResponse:
    """Generate a new API key. The full key is returned in this
    response ONCE — the caller must persist it immediately.

    Subsequent reads via GET /keys/{id} return only metadata (prefix,
    label, scopes, status). The gateway stores an argon2 hash of the
    key; the raw secret is unrecoverable after this response."""
    _ = principal
    team = await session.get(Team, body.team_id)
    if team is None:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "team_not_found",
                "hint": f"team_id={body.team_id!r} does not exist",
            },
        )
    # The DB stores scopes as a space-separated string; the wire form
    # is a list for ergonomics.
    scopes_str = " ".join(body.scopes)
    full_key, prefix = generate_api_key(env=body.env)
    api_key = ApiKey(
        team_id=body.team_id,
        prefix=prefix,
        key_hash=hash_key(full_key),
        scopes=scopes_str,
        label=body.label,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    log.info(
        "identity.key.generated",
        id=api_key.id,
        team_id=api_key.team_id,
        prefix=api_key.prefix,
        scopes=scopes_str,
    )
    return KeyGenerateResponse(
        id=api_key.id,
        team_id=api_key.team_id,
        prefix=api_key.prefix,
        label=api_key.label or "",
        scopes=list(body.scopes),
        status="active",
        created_at=_epoch(api_key.created_at) or 0,
        api_key=full_key,
    )


@router.get("/keys/{key_id}", response_model=KeyResponse)
async def get_key(
    key_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> KeyResponse:
    _ = principal
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "key_not_found", "hint": key_id},
        )
    return _key_to_response(api_key)


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    principal: Annotated[Principal, Depends(require_scope(SCOPE_IDENTITY))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Soft-revoke: sets revoked_at so the audit trail is preserved.
    Subsequent calls with this key return 401."""
    _ = principal
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(
            status_code=404,
            detail={"type": "key_not_found", "hint": key_id},
        )
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        await session.commit()
        log.info("identity.key.revoked", id=key_id)
    return Response(status_code=204)


def _key_to_response(k: ApiKey) -> KeyResponse:
    return KeyResponse(
        id=k.id,
        team_id=k.team_id,
        prefix=k.prefix,
        label=k.label or "",
        scopes=k.scope_list(),
        status="revoked" if k.revoked_at else "active",
        created_at=_epoch(k.created_at) or 0,
        revoked_at=_epoch(k.revoked_at),
        last_used_at=_epoch(k.last_used_at),
    )
