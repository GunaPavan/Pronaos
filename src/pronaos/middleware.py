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

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from pronaos.observability.metrics import (
    http_request_duration_seconds,
    http_requests_total,
)

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


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record per-request Prometheus counters/histograms.

    The ``route`` label uses Starlette's matched route template (e.g.
    ``/v1/admin/usage``) rather than the raw URL path, which would explode
    cardinality on any endpoint that accepts path parameters. If no route
    matched (404), the label falls back to ``"unmatched"`` so the metric
    isn't lost.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        method = request.method
        start = time.monotonic()
        # Default to ``unmatched`` so 404s and uncaught exceptions are still
        # accounted for under a low-cardinality bucket.
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.monotonic() - start
            route = self._route_label(request)
            http_requests_total.labels(
                method=method, route=route, status_code=str(status_code)
            ).inc()
            http_request_duration_seconds.labels(method=method, route=route).observe(
                duration
            )

    @staticmethod
    def _route_label(request: Request) -> str:
        # Starlette populates ``request.scope["route"]`` after routing —
        # ``.path`` is the template like "/v1/chat/completions".
        route = request.scope.get("route")
        path: str | None = getattr(route, "path", None)
        return path or "unmatched"
