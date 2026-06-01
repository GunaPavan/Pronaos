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
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
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
    #
    # Phase 25: when ``circuit_breaker_distributed`` is set AND a Redis
    # URL is configured, use the Redis-backed registry so trip state
    # converges across gateway replicas. Falls back to the in-memory
    # registry on any setup failure (logged + serve continues).
    from pronaos.core.circuit import CircuitBreakerRegistry

    settings_obj = get_settings()
    if settings_obj.circuit_breaker_distributed and settings_obj.redis_url:
        try:
            import redis

            from pronaos.core.circuit_redis import RedisCircuitBreakerRegistry

            sync_redis = redis.Redis.from_url(settings_obj.redis_url, decode_responses=False)
            # Sanity ping — if Redis is misconfigured we want to know
            # at startup, not on the first failing provider call. A
            # failed ping falls through to the in-memory registry so
            # the gateway still boots.
            sync_redis.ping()
            app.state.circuit_registry = RedisCircuitBreakerRegistry(redis_client=sync_redis)
            log.info(
                "circuit.registry.distributed",
                redis_url=settings_obj.redis_url,
            )
        except Exception as e:
            log.warning(
                "circuit.registry.distributed_unavailable",
                error=str(e),
                fallback="in_memory",
            )
            app.state.circuit_registry = CircuitBreakerRegistry()
    else:
        app.state.circuit_registry = CircuitBreakerRegistry()

    # Phase 26: OIDC verifier. Only installed when ``oidc_issuer`` is
    # configured — without it, the OIDC path is disabled and only
    # API-key auth works. Construction is cheap (JWKS fetches are
    # lazy via PyJWKClient and cached for 5 min).
    if settings_obj.oidc_issuer:
        try:
            from pronaos.auth.oidc import OidcVerifier

            app.state.oidc_verifier = OidcVerifier(
                issuer=settings_obj.oidc_issuer,
                audience=settings_obj.oidc_audience,
                jwks_url=settings_obj.oidc_jwks_url,
            )
            log.info(
                "oidc.verifier.enabled",
                issuer=settings_obj.oidc_issuer,
                audience=settings_obj.oidc_audience,
            )
        except Exception as e:
            log.warning("oidc.verifier.unavailable", error=str(e))
            app.state.oidc_verifier = None
    else:
        app.state.oidc_verifier = None

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

    # Phase 30 agent-turn budget tracker. Redis-backed when configured,
    # no-op (allow-all) otherwise. The tracker enforces per-execution
    # caps on tool-using agent loops via the X-Pronaos-Agent-Turn-ID
    # header; teams opt in by setting non-NULL agent_turn_budget_*
    # columns. Fail-open: Redis outage degrades to "no gate."
    from pronaos.core.agent_turn import AgentTurnTracker

    agent_turn_redis: Any = None
    if settings.redis_url:
        try:
            import redis.asyncio as redis_async

            agent_turn_redis = redis_async.from_url(settings.redis_url, decode_responses=False)
            log.info("agent_turn.tracker.redis", redis_url=settings.redis_url)
        except Exception as e:
            log.warning("agent_turn.tracker.redis_unavailable", error=str(e))
            agent_turn_redis = None
    app.state.agent_turn_tracker = AgentTurnTracker(agent_turn_redis)

    # Phase 38: reversible PII tokenization store. Same Redis instance
    # as agent_turn_tracker — the store is namespaced by tenant_id and
    # holds the ``token -> original`` mapping with a per-team TTL.
    # ``None`` when Redis isn't configured; the chat handler then
    # falls back to one-way redaction even for teams that have opted
    # in to tokenization (logged once at request time so operators
    # see the degraded mode).
    from pronaos.core.pii_tokens import TokenStore

    if agent_turn_redis is not None:
        app.state.pii_token_store = TokenStore(agent_turn_redis)
        log.info("pii_tokens.store.redis")
    else:
        app.state.pii_token_store = None

    # Phase 47: per-team per-fqmn prompt-cache hit-rate observer. Same
    # Redis instance as agent_turn_tracker. Writes one observation per
    # successful chat response that carries cache token counts (Phases
    # 34/35). Read at routing time by the
    # ``prompt-cache-aware-cheapest`` strategy. None when Redis isn't
    # configured → strategy degrades to plain cheapest (the scorer
    # already falls through cleanly on an empty snapshot).
    from pronaos.core.prompt_cache_observer import PromptCacheObserver

    app.state.prompt_cache_observer = PromptCacheObserver(agent_turn_redis)
    if agent_turn_redis is not None:
        log.info("prompt_cache_observer.redis", redis_url=settings.redis_url)

    # Phase 57: per-team per-fqmn reasoning-ratio observer. Mirrors
    # the prompt-cache observer's shape — records completion/reasoning
    # totals on every chat response that surfaces a non-zero reasoning
    # count (any of the 5 paths from Phase 56). Read at routing time
    # by ``reasoning-aware-cheapest``. None when Redis isn't configured
    # → strategy degrades to plain cheapest.
    from pronaos.core.reasoning_observer import ReasoningObserver

    app.state.reasoning_observer = ReasoningObserver(agent_turn_redis)
    if agent_turn_redis is not None:
        log.info("reasoning_observer.redis", redis_url=settings.redis_url)

    # Phase 49: per-team tool-call result cache. Same Redis instance;
    # records (tool_name, args) → result whenever a chat request
    # contains a ``tool`` role message; injects cached results into
    # subsequent requests with bare assistant.tool_calls. None when
    # Redis isn't configured → feature degrades silently (the chat
    # handler's record + lookup paths see None and short-circuit).
    from pronaos.core.tool_result_cache import ToolResultCache

    app.state.tool_result_cache = ToolResultCache(agent_turn_redis)
    if agent_turn_redis is not None:
        log.info("tool_result_cache.redis", redis_url=settings.redis_url)

    # Phase 33: singleflight registry for concurrent request dedup.
    # Process-local — when concurrent identical requests arrive on a
    # cold cache, the first becomes the leader (does the upstream call
    # + cache write); subsequent arrivals become followers (await the
    # leader's future). Followers see the same result with zero extra
    # upstream cost. Same registry serves chat, embedding, and rerank.
    #
    # Phase 36: when ``singleflight_distributed`` is set AND a Redis
    # URL is configured, use the Redis-backed registry so dedup
    # converges across gateway replicas. Falls back to the in-memory
    # registry on any setup failure (logged + serve continues). Same
    # ``share(key, fn) -> (result, was_follower)`` interface either
    # way — handlers are backend-agnostic.
    from pronaos.core.singleflight import SingleflightRegistry

    if settings.singleflight_distributed and settings.redis_url:
        try:
            import redis.asyncio as redis_async

            from pronaos.core.singleflight_redis import RedisSingleflightRegistry

            sf_redis = redis_async.from_url(settings.redis_url, decode_responses=False)
            # Sanity ping — if Redis is misconfigured we want to know
            # at startup, not on the first cache-miss singleflight.
            await sf_redis.ping()
            app.state.singleflight = RedisSingleflightRegistry[dict[str, Any]](
                sf_redis,
                ttl_seconds=settings.singleflight_ttl_seconds,
            )
            log.info(
                "singleflight.registry.distributed",
                redis_url=settings.redis_url,
                ttl_seconds=settings.singleflight_ttl_seconds,
            )
        except Exception as e:
            log.warning(
                "singleflight.registry.distributed_unavailable",
                error=str(e),
                fallback="in_memory",
            )
            app.state.singleflight = SingleflightRegistry[dict[str, Any]]()
    else:
        app.state.singleflight = SingleflightRegistry[dict[str, Any]]()

    # Phase 7 cache. NullCache when Redis isn't configured; layered
    # (exact + semantic) when both Redis and semantic_cache_enabled=True;
    # exact-only in between.
    cache = await make_cache(settings)
    app.state.cache = cache

    # Phase 8 guardrails. NullEngine when disabled; default rule set
    # (PII regexes + prompt-injection patterns) when enabled.
    app.state.guardrails = make_guardrail_engine(settings)

    # Phase 44 — Llama Guard ML jailbreak classifier. One classifier
    # per process; only constructed when the operator opted in AND
    # the Groq key is configured (we use the existing Groq endpoint to
    # serve Llama Guard 4 / 3). Per-team policy then decides whether
    # the classifier actually runs on each request — see chat.py.
    app.state.llama_guard = None
    if settings.llama_guard_enabled and settings.groq_api_key:
        from pronaos.guardrails.llama_guard import LlamaGuardClassifier

        app.state.llama_guard = LlamaGuardClassifier(
            api_key=settings.groq_api_key,
            model=settings.llama_guard_model,
        )
        log.info(
            "guardrails.llama_guard.registered",
            model=settings.llama_guard_model,
        )

    # Phase 10 audit log. Stateless service — one instance per process.
    app.state.audit_logger = AuditLogger()

    # Phase 59 — async batches reconciliation worker. Single per-process
    # task that wakes every ``batches_poll_interval_seconds`` and polls
    # in-flight batches at the provider, syncs status + counts back to
    # the row, and on completion writes per-sub-request usage rows at
    # the half-priced rate. Operators running multiple gateway replicas
    # should disable this on N-1 replicas (``PRONAOS_BATCHES_WORKER_ENABLED=false``)
    # since the worker has no leader election; per-request usage rows
    # are keyed by ``{batch_id}#{custom_id}`` so duplicate-run noise is
    # surfaced as IntegrityError-then-skip rather than double-billing,
    # but a single worker is the recommended posture.
    app.state.batch_worker = None
    if settings.batches_worker_enabled:
        from pronaos.core.batch_worker import BatchWorker

        app.state.batch_worker = BatchWorker(
            sessionmaker=sessionmaker,
            settings=settings,
            poll_interval_seconds=settings.batches_poll_interval_seconds,
        )
        app.state.batch_worker.start()
        log.info(
            "batches.worker.enabled",
            interval_s=settings.batches_poll_interval_seconds,
        )

    # Phase 48 — MCP server adapter. When enabled, the gateway exposes
    # ``pronaos.chat`` / ``pronaos.embed`` / ``pronaos.rerank`` as MCP
    # tools so MCP-speaking clients (Claude Code, IDE tools) target the
    # gateway directly. The adapter forwards every tool call back
    # through the gateway's own REST surface via loopback HTTP, so
    # auth / quotas / guardrails / caching / routing / audit all apply
    # uniformly to MCP traffic. Optional — operators opt in via
    # PRONAOS_MCP_ENABLED=true; the FastAPI route registration below
    # then mounts the SSE transport at /v1/mcp/sse.
    app.state.mcp_server = None
    app.state.mcp_transport = None
    if settings.mcp_enabled:
        from mcp.server.sse import SseServerTransport

        from pronaos.mcp.server import PronaosMcpServer

        # Loopback target — the same gateway, same port. We resolve
        # localhost:settings.port so the forwarded calls land back on
        # this process's REST surface and inherit the full middleware
        # chain.
        gateway_url = f"http://127.0.0.1:{settings.port}"
        app.state.mcp_server = PronaosMcpServer(gateway_url=gateway_url)
        app.state.mcp_transport = SseServerTransport("/v1/mcp/messages")
        log.info("mcp.adapter.enabled", endpoint="/v1/mcp/sse", gateway_url=gateway_url)

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
        if app.state.batch_worker is not None:
            await app.state.batch_worker.stop()
        await cache.aclose()
        await rate_limiter.aclose()
        await registry.aclose()
        if app.state.llama_guard is not None:
            await app.state.llama_guard.aclose()
        await engine.dispose()
        log.info("pronaos.shutdown")


def _admin_ui_root() -> Path | None:
    """Locate the built Next.js admin UI bundle.

    Returns the directory containing the static export when it exists,
    or ``None`` when the operator hasn't run ``npm run build`` (dev
    workflow). We look at the conventional ``web/out`` location relative
    to the repo root; the path is computed once at import time and
    cached at the function level via the simple presence check below.
    """
    # main.py lives at src/pronaos/main.py — repo root is two parents up.
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "web" / "out"
    if candidate.is_dir() and (candidate / "index.html").is_file():
        return candidate
    return None


def _mount_admin_ui(app: FastAPI) -> None:
    """Mount the static Next.js admin UI at /admin/* when present.

    The Next.js export produces both ``index.html`` (the SPA shell)
    and ``_next/*`` build assets under ``web/out/``. We register a
    StaticFiles mount that serves the directory tree directly + a
    GET /admin route that returns ``index.html`` so a bare
    ``http://gateway/admin`` lands on the SPA.

    When the build hasn't been run yet (dev workflow), we register
    nothing — the operator's Next.js dev server on :3000 handles the
    UI side via its own proxy back to FastAPI.
    """
    root = _admin_ui_root()
    if root is None:
        log.info("admin_ui.skip_mount", reason="web/out not built")
        return
    # Mount the static tree. ``html=True`` makes StaticFiles serve
    # ``index.html`` when a directory is requested, so /admin/, /admin
    # (after a trailing-slash redirect), and direct /admin/_next/...
    # asset requests all resolve correctly.
    app.mount(
        "/admin",
        StaticFiles(directory=str(root), html=True),
        name="admin-ui",
    )

    # Client-side routing fallback: Next.js's static export
    # pre-renders /login.html and /index.html, but a hard refresh on
    # a route like /admin/teams/foo would 404 against the file system.
    # We register a catch-all that returns index.html for any /admin/*
    # path that doesn't match a real file — the SPA's router then
    # resolves the URL on the client.
    @app.get("/admin/{full_path:path}", include_in_schema=False)
    def _admin_spa_fallback(full_path: str) -> Response:  # pragma: no cover
        target = root / full_path
        if target.is_file():
            return FileResponse(str(target))
        index_html = root / "index.html"
        return FileResponse(str(index_html))

    log.info("admin_ui.mounted", path="/admin", root=str(root))


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
                "admin_ui": "/admin/",
            },
        }

    # ---- Admin UI static mount (Phase 62) ----
    # When ``next build`` has produced a static-export bundle at
    # ``web/out/``, we mount it under ``/admin/*`` so the same FastAPI
    # process serves both the JSON API + the React admin shell. In dev
    # the bundle won't exist (the operator runs ``npm run dev`` on
    # :3000 with its own proxy to FastAPI); we silently skip the mount
    # in that case so the dev workflow keeps working unchanged.
    _mount_admin_ui(app)

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
