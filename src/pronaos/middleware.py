"""Request-scoped logging context middleware.

Binds a ``request_id`` to structlog's contextvars for every incoming request
so every log line emitted during that request is correlatable. Clients can
supply ``X-Request-ID`` to inherit their own trace id; otherwise we mint one.

The auth dependency layers ``tenant_id``/``team_id``/``key_id`` onto the same
context once a principal is resolved, so structured logs end up with the full
``(request_id, tenant_id, team_id, key_id)`` tuple on every line with zero
handler-side plumbing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_HEADER = "x-request-id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(_HEADER) or uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers[_HEADER] = request_id
        return response
