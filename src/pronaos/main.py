"""FastAPI application entrypoint.

Responsible for:
- configuring logging and OpenTelemetry before any request is served
- building the FastAPI app with middleware, routers, and exception handlers
- managing app-scoped resources (provider registry) via the lifespan
- exposing a ``cli`` entrypoint for the ``pronaos`` console script
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from pronaos import __version__
from pronaos.api.v1 import router as v1_router
from pronaos.audit.logger import AuditLogger
from pronaos.cache.factory import make_cache
from pronaos.config import get_settings
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import make_rate_limiter
from pronaos.core.router import Router
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.errors import install_error_handlers
from pronaos.guardrails.factory import make_guardrail_engine
from pronaos.logging import configure_logging, get_logger
from pronaos.middleware import MetricsMiddleware, RequestContextMiddleware
from pronaos.observability.metrics import REGISTRY as METRICS_REGISTRY
from pronaos.observability.otel import configure_tracing
from pronaos.providers.registry import ProviderRegistry

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hooks."""
    configure_logging()
    configure_tracing()
    settings = get_settings()

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    app.state.db_engine = engine
    app.state.db_sessionmaker = sessionmaker

    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry

    # Router: default provider is picked lazily. In Phase 2 we set no default
    # (bare model names error out loudly) since every catalog entry is
    # prefix-accessible. A later phase may add per-tenant defaults.
    app.state.router = Router(registry, default_provider=None)

    # Phase 15: per-provider circuit breakers. One process-local registry,
    # lazily populated as providers are first invoked. The failover layer
    # reads it on every request to skip OPEN providers up front.
    from pronaos.core.circuit import CircuitBreakerRegistry

    app.state.circuit_registry = CircuitBreakerRegistry()

    # Phase 19: outbound webhook dispatcher. One process-local instance,
    # shared across requests. Holds the httpx client so we don't pay
    # TLS handshake cost on every event. Tenants opt in by setting
    # webhook_url + webhook_secret; the dispatcher is a no-op for
    # unconfigured tenants.
    from pronaos.core.webhooks import WebhookDispatcher

    app.state.webhook_dispatcher = WebhookDispatcher()

    # Phase 4 quota infrastructure. The rate limiter is in-memory by default
    # and switches to Redis when ``PRONAOS_REDIS_URL`` is set. The quota
    # tracker is stateless — one instance per process.
    rate_limiter = make_rate_limiter(redis_url=settings.redis_url or None)
    app.state.rate_limiter = rate_limiter
    app.state.quota_tracker = QuotaTracker()

    # Phase 7 cache. NullCache when Redis isn't configured; layered
    # (exact + semantic) when both Redis and semantic_cache_enabled=True;
    # exact-only in between.
    cache = await make_cache(settings)
    app.state.cache = cache

    # Phase 8 guardrails. NullEngine when disabled; default rule set
    # (PII regexes + prompt-injection patterns) when enabled.
    app.state.guardrails = make_guardrail_engine(settings)

    # Phase 10 audit log. Stateless service — one instance per process.
    app.state.audit_logger = AuditLogger()

    log.info(
        "pronaos.startup",
        version=__version__,
        env=settings.env.value,
        providers=registry.available_keys(),
        database=_safe_db_name(settings.database_url),
        rate_limiter=type(rate_limiter).__name__,
    )
    try:
        yield
    finally:
        await cache.aclose()
        await rate_limiter.aclose()
        await registry.aclose()
        await engine.dispose()
        log.info("pronaos.shutdown")


def _safe_db_name(url: str) -> str:
    """Return a redacted label for startup logs.

    Never log the full URL — it may contain credentials.
    """
    if url.startswith("sqlite"):
        return "sqlite"
    scheme, _, _ = url.partition("://")
    return scheme


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Pronaos",
        version=__version__,
        description="Enterprise LLM gateway with observability, cost control, and multi-tenancy.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Order matters: the request-context middleware runs INSIDE metrics, so
    # the latency histogram covers the structlog setup overhead too —
    # which keeps the metric honest about end-to-end gateway cost.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(MetricsMiddleware)

    install_error_handlers(app)
    app.include_router(v1_router)

    # ---- Prometheus exposition ----
    # Plain-text Prometheus format on /metrics. Unauthenticated by design —
    # Prometheus servers don't speak Bearer auth, and the surface area is
    # already firewalled to the internal observability stack in any real
    # deployment. The data exposed is aggregate counters, not per-tenant
    # rows, so there's no PII or cost-breakdown leak.
    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:  # pragma: no cover — trivial passthrough
        # Refresh circuit-breaker gauges from the live registry on every
        # scrape. The breaker is a state machine whose OPEN→HALF_OPEN
        # transition fires on time, not on a request — without this
        # pre-scrape sync, the dashboard would still show OPEN for the
        # 30-second window even after the breaker had silently moved
        # to HALF_OPEN. Cheap (one dict iteration per scrape).
        from pronaos.observability.metrics import record_circuit_state

        circuit_registry = getattr(app.state, "circuit_registry", None)
        if circuit_registry is not None:
            for provider_name, state in circuit_registry.snapshot().items():
                record_circuit_state(provider_name, state.value)

        return Response(
            content=generate_latest(METRICS_REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    # ---- Root index ----
    # A bare ``curl http://localhost:8080`` or browser hit lands here.
    # Returns a small JSON map of the public surface — proof of life
    # plus a self-describing pointer to docs, health, and metrics so
    # someone discovering the gateway doesn't have to grep source.
    @app.get("/", include_in_schema=False)
    def index() -> dict[str, object]:  # pragma: no cover — static index
        return {
            "service": "pronaos",
            "version": __version__,
            "endpoints": {
                "openapi_docs": "/docs",
                "openapi_redoc": "/redoc",
                "openapi_schema": "/openapi.json",
                "health": "/v1/healthz",
                "chat_completions": "/v1/chat/completions",
                "admin_usage": "/v1/admin/usage",
                "prometheus_metrics": "/metrics",
            },
        }

    # Instrument after routes are registered so all endpoints produce spans.
    if settings.otel_enabled:
        FastAPIInstrumentor.instrument_app(app)

    return app


app = create_app()


def cli() -> None:
    """Console-script entrypoint: ``pronaos``."""
    settings = get_settings()
    uvicorn.run(
        "pronaos.main:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
        log_config=None,  # defer to structlog
    )


if __name__ == "__main__":
    cli()
