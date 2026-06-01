"""FastAPI auth dependencies.

Centralises Bearer-token parsing, DB session access, and the actual
``verify_key`` call so handlers can declare ``principal: Annotated[Principal,
Depends(require_principal)]`` and be done with it.

Layering (Phase 4)
------------------
The full per-request pipeline is::

    auth → scope → quota gates → handler → (record_usage)

``require_principal``  parses the Bearer token and returns the Principal.
``require_scope("X")`` ensures the principal has scope X.
``enforce_quotas``     checks (a) per-key RPS limit, (b) per-team token budget.

Logging side-effect
-------------------
On successful auth we bind ``tenant_id``, ``team_id``, and ``key_id`` to the
structlog context. Every subsequent log line carries them automatically — no
manual plumbing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal, resolve_oidc_subject, verify_key
from pronaos.auth.oidc import OidcAuthError, OidcVerifier
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import RateLimiter
from pronaos.core.webhooks import (
    WebhookConfig,
    WebhookDispatcher,
    quota_exhausted_event,
)
from pronaos.observability.metrics import record_quota_denial

_bearer = HTTPBearer(auto_error=False)


def _looks_like_jwt(token: str) -> bool:
    """Cheap shape check. JWTs are three dot-separated base64 segments;
    Pronaos API keys are underscore-separated. Never collide, so we can
    dispatch on the structural difference without trying to decode."""
    return token.count(".") == 2 and " " not in token and token[0:1] != "_"


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
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorised()

    token = credentials.credentials

    # Phase 26: dual-auth dispatch. JWTs (three dot-separated segments)
    # go to the OIDC path; everything else is an API key. The two formats
    # never collide on the wire (API keys are underscore-separated).
    if _looks_like_jwt(token):
        verifier: OidcVerifier | None = getattr(request.app.state, "oidc_verifier", None)
        if verifier is None:
            # JWT-shaped token but OIDC isn't configured on this
            # gateway. Surface 401 with a non-leaking message — we
            # don't want to expose to the caller whether OIDC is on
            # or off as a probe vector.
            raise _unauthorised()
        try:
            claims = verifier.verify(token)
        except OidcAuthError:
            raise _unauthorised() from None
        principal = await resolve_oidc_subject(session, claims.subject)
        if principal is None:
            # JWT was valid but no tenant claims this subject. From
            # the caller's perspective this is also 401 — the gateway
            # doesn't reveal which subjects are configured.
            raise _unauthorised()
    else:
        principal = await verify_key(session, token)
        if principal is None:
            raise _unauthorised()

    # Bind for the rest of the request. This propagates to every structlog
    # call regardless of where in the code it happens.
    structlog.contextvars.bind_contextvars(
        tenant_id=principal.tenant_id,
        team_id=principal.team_id,
        key_id=principal.key_id,
        auth_method=principal.auth_method,
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


# --------------------------------------------------------------------------- #
# Quota gate                                                                  #
# --------------------------------------------------------------------------- #


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise RuntimeError("rate_limiter not initialised on app.state")
    return limiter


def get_quota_tracker(request: Request) -> QuotaTracker:
    tracker: QuotaTracker | None = getattr(request.app.state, "quota_tracker", None)
    if tracker is None:
        raise RuntimeError("quota_tracker not initialised on app.state")
    return tracker


def enforce_quotas(
    required_scope: str,
) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: auth → scope → RPS check → budget check.

    Use as the single gate on protected endpoints, replacing ``require_scope``::

        @router.post("/v1/chat/completions")
        async def chat(
            ...,
            principal: Annotated[Principal, Depends(enforce_quotas("chat:write"))],
        ): ...

    A denied request returns ``429`` with a ``Retry-After`` header. The error
    body is OpenAI-shape (matches ``forge_gateway.errors.install_error_handlers``).
    """

    # Use default-arg form of Depends here (not Annotated[...]) because the
    # ``required_scope`` closure variable can't be resolved by
    # ``typing.get_type_hints()`` under ``from __future__ import annotations`` —
    # FastAPI would silently fall back to treating ``principal`` as a query
    # param, returning 422 instead of 401 on missing auth.
    async def _gate(
        request: Request,
        principal: Principal = Depends(require_scope(required_scope)),  # noqa: B008
        limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
        tracker: QuotaTracker = Depends(get_quota_tracker),  # noqa: B008
        session: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> Principal:
        # ---- Layer 1: per-key RPS ---------------------------------------
        if principal.rps_limit is not None:
            rl_result = await limiter.check_and_consume(
                scope_key=principal.key_id,
                burst=principal.rps_limit,
                refill_per_second=float(principal.rps_limit),
            )
            if not rl_result.allowed:
                record_quota_denial("rate_limit")
                _publish_quota_denial(
                    request,
                    principal,
                    reason="rate_limit",
                    retry_after_seconds=int(rl_result.retry_after_seconds or 0) or None,
                )
                raise _rate_limited(
                    retry_after_seconds=rl_result.retry_after_seconds,
                    reason="rate_limit",
                )

        # ---- Layer 2: per-team token + cost budgets ---------------------
        budget_result = await tracker.check_budget(session, principal.team_id)
        if not budget_result.allowed:
            reason = budget_result.reason or "budget_exhausted"
            record_quota_denial(reason)
            _publish_quota_denial(
                request,
                principal,
                reason=reason,
                retry_after_seconds=int(budget_result.retry_after_seconds or 0) or None,
            )
            raise _rate_limited(
                retry_after_seconds=budget_result.retry_after_seconds,
                reason=reason,
            )

        return principal

    return _gate


def _publish_quota_denial(
    request: Request,
    principal: Principal,
    *,
    reason: str,
    retry_after_seconds: int | None,
) -> None:
    """Fire a ``quota.exhausted`` webhook event for the principal's tenant.

    Fire-and-forget — no-op if the tenant has no webhook configured.
    Called immediately before raising the 429 so the operator's
    receiver learns about the denial in near-real-time, not on the
    next dashboard scrape.
    """
    dispatcher: WebhookDispatcher | None = getattr(request.app.state, "webhook_dispatcher", None)
    if dispatcher is None:
        return
    dispatcher.publish(
        WebhookConfig(
            url=principal.webhook_url,
            secret=principal.webhook_secret,
        ),
        quota_exhausted_event(
            tenant_id=principal.tenant_id,
            team_id=principal.team_id,
            team_name=principal.team_name,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
        ),
    )


def _rate_limited(retry_after_seconds: float, reason: str) -> HTTPException:
    # ``Retry-After`` must be an integer second count per RFC 7231.
    retry_after = max(1, int(retry_after_seconds + 0.999))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "type": reason,
            "message": "request denied by quota policy",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def _unauthorised() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
