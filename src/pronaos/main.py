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
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from pronaos import __version__
from pronaos.api.v1 import router as v1_router
from pronaos.config import get_settings
from pronaos.core.quota import QuotaTracker
from pronaos.core.ratelimit import make_rate_limiter
from pronaos.core.router import Router
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.errors import install_error_handlers
from pronaos.logging import configure_logging, get_logger
from pronaos.middleware import RequestContextMiddleware
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

    # Phase 4 quota infrastructure. The rate limiter is in-memory by default
    # and switches to Redis when ``PRONAOS_REDIS_URL`` is set. The quota
    # tracker is stateless — one instance per process.
    rate_limiter = make_rate_limiter(redis_url=settings.redis_url or None)
    app.state.rate_limiter = rate_limiter
    app.state.quota_tracker = QuotaTracker()

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
    app.add_middleware(RequestContextMiddleware)

    install_error_handlers(app)
    app.include_router(v1_router)

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
