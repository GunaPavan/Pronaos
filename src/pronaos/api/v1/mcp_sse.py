"""FastAPI routes mounting the MCP SSE transport — Phase 48.

Two endpoints:

- ``GET /v1/mcp/sse`` — long-lived SSE connection. The MCP client
  opens this stream; the server sends MCP protocol messages on it.
- ``POST /v1/mcp/messages`` — short-lived message-post endpoint.
  The MCP client POSTs messages here; the server routes them onto
  the corresponding SSE connection's read stream.

Auth at the SSE handshake
-------------------------
The MCP protocol itself has no auth spec. We validate the bearer
token (a Pronaos API key) BEFORE entering the SSE transport. The
validated token is stashed into a per-task ContextVar
(``mcp.server.current_bearer_token``); every ``tools/call``
handler reads it and forwards via loopback HTTP.

The POST /v1/mcp/messages endpoint is NOT auth-gated separately —
it's identified by the ``session_id`` query parameter the SSE
transport hands the client. An attacker without a valid SSE
session can't construct a working session_id without already
having connected (and thus passed the bearer-token check at SSE
time).

When MCP is disabled
--------------------
The routes return 404 even when MCP is disabled rather than not
registering at all. That gives operators a stable URL surface
for setting up MCP clients while flipping the feature on/off
via ``PRONAOS_MCP_ENABLED``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from pronaos.auth.api_keys import Principal, verify_key
from pronaos.auth.deps import get_db
from pronaos.logging import get_logger
from pronaos.mcp.server import reset_bearer_token, set_bearer_token

log = get_logger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


async def _bearer_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> tuple[Principal, str]:
    """Authenticate the SSE handshake via the standard Pronaos API key.

    Returns ``(principal, bearer_token)``. The bearer token is
    threaded into the MCP server's ContextVar for the duration of
    the SSE connection; the principal isn't passed into the
    forwarded REST calls because each call re-resolves its own
    principal from the same bearer token — same path REST clients
    take. We still resolve it here to fail fast on bad tokens.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "missing_bearer_token"},
            headers={"WWW-Authenticate": 'Bearer realm="pronaos-mcp"'},
        )
    token = auth[len("Bearer ") :].strip()
    principal = await verify_key(session, token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "invalid_api_key"},
            headers={"WWW-Authenticate": 'Bearer realm="pronaos-mcp"'},
        )
    if "chat:write" not in principal.scopes:
        # MCP exposes chat/embed/rerank — all under the chat:write
        # scope today. A key with only admin:usage shouldn't be able
        # to call them via MCP either.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"type": "missing_scope", "required": "chat:write"},
        )
    return principal, token


@router.get("/sse")
async def mcp_sse(
    request: Request,
    auth: Annotated[tuple[Principal, str], Depends(_bearer_principal)],
) -> Response:
    """Open the MCP SSE channel.

    Returns 503 when MCP is disabled at the operator level. Otherwise
    blocks for the lifetime of the connection (FastAPI/Starlette
    streams the response).
    """
    mcp_server = getattr(request.app.state, "mcp_server", None)
    mcp_transport = getattr(request.app.state, "mcp_transport", None)
    if mcp_server is None or mcp_transport is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "mcp_disabled",
                "hint": (
                    "set PRONAOS_MCP_ENABLED=true and restart the gateway "
                    "to enable the MCP server adapter"
                ),
            },
        )
    principal, bearer = auth
    log.info(
        "mcp.sse.connect",
        tenant=principal.tenant_name,
        team=principal.team_name,
        key_id=principal.key_id,
    )
    # Hand the connection off to the MCP SDK's SSE transport. The
    # transport yields the read/write streams the MCP Server instance
    # then drives. We set the bearer token on a ContextVar so the
    # tool-call handlers (running inside the same asyncio task) can
    # forward it on the loopback HTTP calls.
    token_reset = set_bearer_token(bearer)
    try:
        async with mcp_transport.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            init_options = mcp_server.mcp.create_initialization_options()
            await mcp_server.mcp.run(read_stream, write_stream, init_options)
    finally:
        reset_bearer_token(token_reset)
    # Connection ended cleanly. Returning a Response here is mostly
    # ceremonial — Starlette has already closed the response by the
    # time we get here. FastAPI requires a return value so the type
    # contract is satisfied.
    return Response(status_code=200)


@router.post("/messages")
async def mcp_post_message(request: Request) -> Response:
    """Receive an MCP message from a client and route it to the
    matching SSE session.

    Auth isn't re-checked here (see module docstring): only a holder
    of the ``session_id`` issued during a successful SSE handshake
    can construct a working POST. The SDK's transport handles
    routing internally.
    """
    mcp_transport = getattr(request.app.state, "mcp_transport", None)
    if mcp_transport is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"type": "mcp_disabled"},
        )
    await mcp_transport.handle_post_message(request.scope, request.receive, request._send)
    return Response(status_code=202)
