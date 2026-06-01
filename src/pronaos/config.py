"""Application configuration.

All settings are loaded from environment variables (or a .env file in dev).
Config is a Pydantic model so it is validated at startup — the process refuses
to boot with an invalid configuration rather than failing mid-request.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    development = "development"
    staging = "staging"
    production = "production"


class LogLevel(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRONAOS_",
        extra="ignore",
        case_sensitive=False,
    )

    # Core
    env: Environment = Environment.development
    log_level: LogLevel = LogLevel.info
    host: str = "0.0.0.0"  # noqa: S104 — server intentionally binds all interfaces
    port: int = 8080

    # Security
    secret_key: str = Field(default="change-me-in-production", min_length=8)
    # ``NoDecode`` opts out of pydantic-settings' source-level JSON parsing so
    # our CSV-or-JSON validator below handles the raw string from .env.
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # Data plane
    # Default is SQLite for zero-friction local dev. Production should set
    # a Postgres URL such as:
    #   postgresql+asyncpg://pronaos:pronaos@postgres:5432/pronaos
    database_url: str = "sqlite+aiosqlite:///./pronaos.db"
    # ``None`` means in-memory rate limiter (zero-install dev path). Set to
    # a Redis URL (e.g. ``redis://localhost:6379/0``) in prod to share quota
    # state across multiple workers.
    redis_url: str | None = None
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    # Semantic cache (Phase 7.2). Opt-in because the local embedding model
    # boots PyTorch (~1-2 s startup, ~250 MB RAM). Production deployments
    # set this true; CI / dev sessions keep it off unless explicitly tested.
    semantic_cache_enabled: bool = False
    # Cosine similarity threshold for an L2 hit. 0.95 = strong paraphrase
    # match for all-MiniLM-L6-v2. Lower = more hits but more false positives.
    semantic_cache_threshold: float = 0.95

    # Guardrails (Phase 8.1). On by default — REDACT for PII, LOG_ONLY for
    # prompt injection. Set false to disable for interactive prompt-
    # engineering workflows where false-positive redactions are a nuisance.
    guardrails_enabled: bool = True

    # ML-based PII detection via Presidio (Phase 22). OFF by default
    # because Presidio pulls in spaCy + ML models (~600 MB disk, ~250 MB
    # RAM, ~1-2 s startup cost). Operators on regulated workloads should
    # set true; the per-team policy then decides which entity types to
    # actually run. Recall delta over regex measured at empirical claim #9.
    presidio_enabled: bool = False
    # Minimum confidence (0..1) for a Presidio hit to count. Lower =
    # more recall, more false positives. 0.5 is Presidio's own default
    # threshold for its recognizers.
    presidio_min_score: float = 0.5

    # Phase 44 — Llama Guard ML jailbreak / unsafe-content classifier.
    # When enabled at the operator level, every team can opt in via its
    # ``guardrail_policy`` JSON: ``{"llama_guard": {"enabled": true}}``.
    # Llama Guard runs as an async pre-check in front of the regex /
    # Presidio detectors. It calls Llama Guard 4 (or 3) via the existing
    # GROQ_API_KEY — no additional provider key required. The classifier
    # itself fails open: a Groq outage or model error degrades to "safe"
    # so the gateway keeps serving (regex + Presidio are still in place).
    # Recall delta over regex measured at empirical claim #31.
    llama_guard_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PRONAOS_LLAMA_GUARD_ENABLED", "LLAMA_GUARD_ENABLED"),
    )
    # Default Llama Guard model. Operators can override per-team via
    # ``guardrail_policy.llama_guard.model`` (e.g. switch to Llama Guard
    # 3 8B on Groq for lower latency at a small recall cost).
    llama_guard_model: str = Field(
        default="groq/meta-llama/llama-prompt-guard-2-86m",
        validation_alias=AliasChoices("PRONAOS_LLAMA_GUARD_MODEL", "LLAMA_GUARD_MODEL"),
    )

    # Distributed circuit-breaker state (Phase 25). OFF by default
    # because a single-process gateway doesn't benefit and the
    # in-memory breaker is lower-latency. Set true when running
    # multiple gateway replicas behind a load balancer so they share
    # trip decisions via Redis (requires ``redis_url`` to be set).
    # Multi-replica convergence measured at empirical claim #12.
    circuit_breaker_distributed: bool = False

    # Phase 48 — Native MCP (Model Context Protocol) server adapter.
    # When enabled, the gateway exposes its functionality as MCP tools
    # over SSE at ``/v1/mcp/sse`` and an HTTP message-post endpoint at
    # ``/v1/mcp/messages``. MCP-speaking clients (Claude Code, IDE
    # integrations, Anthropic's own apps) can target the gateway
    # directly with all of Pronaos's auth/quota/audit/routing applied
    # automatically. Authentication uses the same bearer-token API key
    # mechanism as the existing REST endpoints. Empirical claim #35.
    mcp_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PRONAOS_MCP_ENABLED", "MCP_ENABLED"),
    )

    # Phase 59 — async batches reconciliation worker. When true, a
    # single background asyncio task polls all non-terminal batches
    # every ``batches_poll_interval_seconds``, updates the row's
    # status + counts, and on completion writes per-sub-request usage
    # rows at the half-priced rate. Operators running multiple
    # gateway replicas should enable on exactly ONE replica because
    # the worker has no leader election (idempotent writes guard
    # against duplicate-billing but cause harmless IntegrityError-
    # then-skip noise in logs). Default ON because most deployments
    # are single-process; flip to false when running N>1 replicas.
    batches_worker_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PRONAOS_BATCHES_WORKER_ENABLED",
            "BATCHES_WORKER_ENABLED",
        ),
    )
    batches_poll_interval_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "PRONAOS_BATCHES_POLL_INTERVAL_SECONDS",
            "BATCHES_POLL_INTERVAL_SECONDS",
        ),
    )

    # Distributed singleflight dedup (Phase 36). When true AND
    # ``redis_url`` is set, the gateway collapses concurrent identical
    # cache-miss requests across replicas via a Redis-coordinated
    # claim-or-await pattern. Default false because the in-memory
    # singleflight (Phase 33) is sufficient for single-process and
    # already saves the within-replica wins. Enable for multi-replica
    # deployments where bursty identical-input workloads (RAG
    # ingestion, retry storms) can otherwise produce one upstream call
    # per replica. Empirical demonstration at claim #23.
    singleflight_distributed: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PRONAOS_SINGLEFLIGHT_DISTRIBUTED",
            "SINGLEFLIGHT_DISTRIBUTED",
        ),
    )
    # TTL (seconds) on the Redis singleflight key. Bounds the absolute
    # follower wait if the leader crashes mid-call; defaults to 60s,
    # well above any reasonable upstream LLM latency.
    singleflight_ttl_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "PRONAOS_SINGLEFLIGHT_TTL_SECONDS",
            "SINGLEFLIGHT_TTL_SECONDS",
        ),
    )

    # Phase 40 — quality regression monitoring. Default judge model
    # used when a team's ``quality_judge_model`` column is NULL.
    # gpt-4o-mini is cheap (~$0.15/Mtok input) and consistent enough
    # for quality monitoring — operators on a tighter budget point
    # this at a Groq Llama if cost matters more than judge precision.
    quality_default_judge_model: str = Field(
        default="openai/gpt-4o-mini",
        validation_alias=AliasChoices(
            "PRONAOS_QUALITY_DEFAULT_JUDGE_MODEL",
            "QUALITY_DEFAULT_JUDGE_MODEL",
        ),
    )

    # OIDC / SSO admin auth (Phase 26). When ``oidc_issuer`` is set
    # the gateway accepts Bearer JWTs alongside API keys. The JWT
    # must be signed by the issuer's JWKS, have a matching ``aud``
    # claim (if ``oidc_audience`` is set), and carry a ``sub`` that
    # matches some tenant's ``oidc_subject`` column. NULL issuer
    # disables the OIDC path entirely — API-key auth is unaffected.
    oidc_issuer: str | None = None
    # Audience expected on inbound JWTs. If set, must match the JWT's
    # ``aud`` claim or the token is rejected. Operator-controlled
    # belt-and-braces against accidental cross-IdP token reuse.
    # Leaving NULL accepts any audience — fine for single-IdP setups.
    oidc_audience: str | None = None
    # Optional override for the JWKS URL. When unset (the common case)
    # the gateway fetches the JWKS URL from the OIDC discovery
    # document at ``{issuer}/.well-known/openid-configuration``.
    oidc_jwks_url: str | None = None

    # Observability
    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "pronaos"

    # Provider credentials — accept both provider-native names
    # (ANTHROPIC_API_KEY) and PRONAOS_-prefixed names so deployments can choose
    # their namespace convention without code changes.
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    groq_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_GROQ_API_KEY", "GROQ_API_KEY"),
    )
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    together_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_TOGETHER_API_KEY", "TOGETHER_API_KEY"),
    )
    fireworks_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_FIREWORKS_API_KEY", "FIREWORKS_API_KEY"),
    )
    perplexity_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_PERPLEXITY_API_KEY", "PERPLEXITY_API_KEY"),
    )
    xai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_XAI_API_KEY", "XAI_API_KEY"),
    )
    cerebras_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_CEREBRAS_API_KEY", "CEREBRAS_API_KEY"),
    )
    mistral_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    )
    # Phase 31: embedding-only providers (no chat surface).
    cohere_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_COHERE_API_KEY", "COHERE_API_KEY"),
    )
    voyage_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_VOYAGE_API_KEY", "VOYAGE_API_KEY"),
    )
    azure_openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"),
    )
    azure_openai_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_ENDPOINT"),
    )
    # Local/self-hosted OpenAI-compat endpoint (Ollama by default).
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        validation_alias=AliasChoices("PRONAOS_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"),
    )
    ollama_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PRONAOS_OLLAMA_ENABLED", "OLLAMA_ENABLED"),
    )
    # Phase 42 — AWS Bedrock native adapter. The presence of both
    # ``aws_access_key_id`` AND ``aws_secret_access_key`` enables the
    # ``bedrock`` provider key. ``aws_region`` defaults to ``us-east-1``
    # — Bedrock model availability is per-region, so an explicit region
    # is required for any non-US-East-1 deployment. We deliberately do
    # NOT support IAM role assumption from the environment in this
    # phase; that's a follow-up for in-cluster deployments where the
    # gateway pod runs with an IRSA / instance profile.
    aws_access_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    aws_secret_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_AWS_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    aws_session_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_AWS_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
    )
    aws_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("PRONAOS_AWS_REGION", "AWS_REGION"),
    )

    # Phase 53 — GCP Vertex AI native adapter. The presence of
    # ``vertex_project_id`` + ``vertex_service_account_json``
    # enables the ``vertex`` provider key. ``vertex_region``
    # defaults to ``us-central1`` (broadest Vertex model
    # availability). The SA JSON setting accepts EITHER an inline
    # JSON string OR a path to a JSON file on disk — the auth
    # helper auto-detects which it received.
    vertex_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PRONAOS_VERTEX_PROJECT_ID", "VERTEX_PROJECT_ID"),
    )
    vertex_region: str = Field(
        default="us-central1",
        validation_alias=AliasChoices("PRONAOS_VERTEX_REGION", "VERTEX_REGION"),
    )
    vertex_service_account_json: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PRONAOS_VERTEX_SERVICE_ACCOUNT_JSON",
            "VERTEX_SERVICE_ACCOUNT_JSON",
            # Accept the standard GCP env var name too so operators
            # who already point GOOGLE_APPLICATION_CREDENTIALS at the
            # SA JSON file get it for free.
            "GOOGLE_APPLICATION_CREDENTIALS",
        ),
    )

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept CSV or JSON for list fields from .env files.

        pydantic-settings defaults to JSON for complex types, which is hostile
        to plain .env usage. This validator lets callers write either
        ``PRONAOS_ALLOWED_ORIGINS=https://a.com,https://b.com`` or valid JSON.
        """
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                return v  # let pydantic parse it as JSON
            return [item.strip() for item in s.split(",") if item.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.env is Environment.production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so config is parsed exactly once per process.
    """
    return Settings()
