"""FastAPI auth dependencies.

Centralises Bearer-token parsing, DB session access, and the actual
``verify_key`` call so handlers can declare ``principal: Annotated[Principal,
Depends(require_principal)]`` and be done with it.

Logging side-effect
-------------------
On success we bind ``tenant_id``, ``team_id``, and ``key_id`` to the structlog
context. Every subsequent log line in this request carries them automatically —
no manual plumbing into handlers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal, verify_key

_bearer = HTTPBearer(auto_error=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped async session.

    Commit happens at the end only if the handler returned cleanly — which
    gives us transaction boundaries matching HTTP request boundaries.
    """
    sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
    if sessionmaker is None:
        raise RuntimeError("db sessionmaker not initialised on app.state")
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorised()

    principal = await verify_key(session, credentials.credentials)
    if principal is None:
        raise _unauthorised()

    # Bind for the rest of the request. This propagates to every structlog
    # call regardless of where in the code it happens.
    structlog.contextvars.bind_contextvars(
        tenant_id=principal.tenant_id,
        team_id=principal.team_id,
        key_id=principal.key_id,
    )
    return principal


def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory that enforces a scope token on an already-authed request."""

    async def _check(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required scope: {scope}",
            )
        return principal

    return _check


def _unauthorised() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
