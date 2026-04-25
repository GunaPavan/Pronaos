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
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

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
