"""Provider registry.

App-scoped lazy factory for Provider instances. Each provider is constructed
on first use and reused across requests so we share one connection pool per
upstream. The registry owns lifecycle; ``aclose()`` is called from the
FastAPI lifespan on shutdown.

Phase 2 makeup:
- Native adapters (Anthropic today) are built explicitly.
- OpenAI-compatible providers are built from ``providers.catalog`` — no new
  code per provider, just a catalog entry + a configured API key.
"""

from __future__ import annotations

from pronaos.config import Settings
from pronaos.providers.anthropic import AnthropicProvider
from pronaos.providers.base import Provider, ProviderError
from pronaos.providers.catalog import CATALOG
from pronaos.providers.openai_compat import OpenAICompatibleProvider


class ProviderNotConfiguredError(ProviderError):
    """The requested provider has no credentials configured."""

    retryable = False
    status = 503


class UnknownProviderError(ProviderError):
    """The requested provider is not registered."""

    retryable = False
    status = 400


class ProviderRegistry:
    """Lazy, app-scoped provider factory."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._instances: dict[str, Provider] = {}

    # ---- Public API ----------------------------------------------------------

    def get(self, name: str) -> Provider:
        if name in self._instances:
            return self._instances[name]
        provider = self._build(name)
        self._instances[name] = provider
        return provider

    def available_keys(self) -> list[str]:
        """Return keys of providers that *could* be built given current config.

        Useful for startup logging and admin-endpoint diagnostics: recruiters
        reading the logs will see exactly which of the 12+ catalog entries
        are live in this environment.
        """
        keys: list[str] = []
        if self._settings.anthropic_api_key:
            keys.append("anthropic")
        for catalog_key, entry in CATALOG.items():
            if catalog_key == "ollama":
                if self._settings.ollama_enabled:
                    keys.append(catalog_key)
                continue
            attr = entry.settings_attr
            if attr and getattr(self._settings, attr, None):
                keys.append(catalog_key)
        return keys

    async def aclose(self) -> None:
        for provider in self._instances.values():
            await provider.aclose()
        self._instances.clear()

    # ---- Builders ------------------------------------------------------------

    def _build(self, name: str) -> Provider:
        if name == "anthropic":
            return self._build_anthropic()
        if name in CATALOG:
            return self._build_from_catalog(name)
        raise UnknownProviderError(f"unknown provider: {name!r}")

    def _build_anthropic(self) -> Provider:
        key = self._settings.anthropic_api_key
        if not key:
            raise ProviderNotConfiguredError("anthropic: ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(api_key=key)

    def _build_from_catalog(self, name: str) -> Provider:
        entry = CATALOG[name]

        # Ollama is a local provider with no API key; it's enabled via a flag.
        if name == "ollama":
            if not self._settings.ollama_enabled:
                raise ProviderNotConfiguredError(
                    "ollama: set OLLAMA_ENABLED=true to use the local Ollama endpoint"
                )
            return OpenAICompatibleProvider(
                provider_key=entry.key,
                base_url=self._settings.ollama_base_url,
                api_key="ollama",  # dummy — Ollama ignores auth
                pricing=entry.pricing,
                default_headers=dict(entry.default_headers),
                auth_header_name=entry.auth.header_name,
                auth_header_format=entry.auth.header_format,
            )

        api_key = getattr(self._settings, entry.settings_attr, None)
        if not api_key:
            raise ProviderNotConfiguredError(f"{name}: {entry.settings_attr.upper()} is not set")

        base_url = entry.base_url
        # Azure OpenAI base URL is per-tenant; allow override via settings.
        if name == "openai" and self._settings.azure_openai_endpoint:
            pass  # openai-compat points at api.openai.com; azure is a separate key
        # (We keep azure as a *separate* catalog entry in a later phase; for
        # now users with only an Azure deployment should set OPENAI_API_KEY
        # and OPENAI_BASE_URL via an override — tracked as a future task.)

        return OpenAICompatibleProvider(
            provider_key=entry.key,
            base_url=base_url,
            api_key=api_key,
            pricing=entry.pricing,
            default_headers=dict(entry.default_headers),
            auth_header_name=entry.auth.header_name,
            auth_header_format=entry.auth.header_format,
        )
