"""Provider registry.

App-scoped lazy factory for Provider instances. Each provider is constructed
on first use and reused across requests so we share one connection pool per
upstream. The registry owns lifecycle; ``aclose()`` is called from the
FastAPI lifespan on shutdown.

Phase 2 makeup:
- Native adapters (Anthropic today) are built explicitly.
- OpenAI-compatible providers are built from ``providers.catalog`` — no new
  code per provider, just a catalog entry + a configured API key.

Phase 31 adds an embedding-provider sibling registry. Same lazy-factory
shape, separate cache (chat and embedding providers are different types
even when they share an API key and base URL).
"""

from __future__ import annotations

from pronaos.config import Settings
from pronaos.providers.anthropic import AnthropicProvider
from pronaos.providers.base import Provider, ProviderError
from pronaos.providers.bedrock import BedrockProvider
from pronaos.providers.catalog import CATALOG
from pronaos.providers.embeddings import (
    CohereEmbeddingProvider,
    EmbeddingProvider,
    LocalSentenceTransformerEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    VoyageEmbeddingProvider,
)
from pronaos.providers.openai_compat import OpenAICompatibleProvider
from pronaos.providers.rerank import (
    CohereRerankProvider,
    RerankProvider,
    VoyageRerankProvider,
)
from pronaos.providers.vertex import VertexProvider
from pronaos.providers.vertex_auth import VertexAuth, VertexAuthError


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
        # Phase 31: separate embedding-provider cache. Some providers
        # (openai, mistral) appear in both maps with the same API key;
        # they're distinct instances because the adapters are
        # different classes and don't share a connection pool.
        self._embedding_instances: dict[str, EmbeddingProvider] = {}
        # Phase 32: separate rerank-provider cache. Same isolation
        # rationale as embeddings — Cohere and Voyage host both
        # endpoint shapes; we build distinct adapter instances.
        self._rerank_instances: dict[str, RerankProvider] = {}

    # ---- Public API ----------------------------------------------------------

    def get(self, name: str) -> Provider:
        if name in self._instances:
            return self._instances[name]
        provider = self._build(name)
        self._instances[name] = provider
        return provider

    def get_embedding(self, name: str) -> EmbeddingProvider:
        """Return the embedding provider for ``name``. Built lazily.

        Catalog entries without an ``embedding_pricing`` block raise
        :class:`UnknownProviderError` — i.e. you can't ask Groq for
        embeddings because Groq doesn't sell embedding models.
        """
        if name in self._embedding_instances:
            return self._embedding_instances[name]
        provider = self._build_embedding(name)
        self._embedding_instances[name] = provider
        return provider

    def available_keys(self) -> list[str]:
        """Return keys of providers that *could* be built given current config.

        Useful for startup logging and admin-endpoint diagnostics: operators
        reading the logs will see exactly which of the 12 catalog entries
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
            if catalog_key == "bedrock":
                # Bedrock needs BOTH access-key AND secret-key set —
                # a single one signals a half-configured environment
                # that we'd rather surface explicitly than silently
                # advertise.
                if (
                    self._settings.aws_access_key_id
                    and self._settings.aws_secret_access_key
                ):
                    keys.append(catalog_key)
                continue
            if catalog_key == "vertex":
                # Vertex needs BOTH project_id AND service-account JSON
                # set; one without the other is a half-configured
                # environment.
                if (
                    self._settings.vertex_project_id
                    and self._settings.vertex_service_account_json
                ):
                    keys.append(catalog_key)
                continue
            attr = entry.settings_attr
            if attr and getattr(self._settings, attr, None):
                keys.append(catalog_key)
        return keys

    def get_rerank(self, name: str) -> RerankProvider:
        """Return the rerank provider for ``name``. Built lazily.

        Catalog entries without ``rerank_pricing`` raise
        :class:`UnknownProviderError` — i.e. only Cohere and Voyage
        accept rerank requests in the catalog today.
        """
        if name in self._rerank_instances:
            return self._rerank_instances[name]
        provider = self._build_rerank(name)
        self._rerank_instances[name] = provider
        return provider

    def available_embedding_keys(self) -> list[str]:
        """Return embedding-provider keys configured in this environment.

        Same shape as :meth:`available_keys` but filtered to catalog
        entries that declare ``embedding_pricing``. The local-ST
        provider has no API key requirement and is always available
        when sentence-transformers is installed (which it already is —
        the semantic cache needs it).
        """
        keys: list[str] = []
        for catalog_key, entry in CATALOG.items():
            if not entry.embedding_pricing:
                continue
            attr = entry.settings_attr
            if attr and getattr(self._settings, attr, None):
                keys.append(catalog_key)
        # The local provider is always selectable under the "local"
        # key (no catalog entry needed — it's not a billed upstream).
        keys.append("local")
        return keys

    def available_rerank_keys(self) -> list[str]:
        """Return rerank-provider keys configured in this environment.

        Same shape as :meth:`available_embedding_keys` but filtered to
        catalog entries that declare ``rerank_pricing``. No "local"
        rerank — cross-encoder reranking is compute-heavy enough that
        we don't bundle a local fallback.
        """
        keys: list[str] = []
        for catalog_key, entry in CATALOG.items():
            if not entry.rerank_pricing:
                continue
            attr = entry.settings_attr
            if attr and getattr(self._settings, attr, None):
                keys.append(catalog_key)
        return keys

    async def aclose(self) -> None:
        for provider in self._instances.values():
            await provider.aclose()
        self._instances.clear()
        for ep in self._embedding_instances.values():
            await ep.aclose()
        self._embedding_instances.clear()
        for rp in self._rerank_instances.values():
            await rp.aclose()
        self._rerank_instances.clear()

    # ---- Builders ------------------------------------------------------------

    def _build(self, name: str) -> Provider:
        if name == "anthropic":
            return self._build_anthropic()
        if name == "bedrock":
            return self._build_bedrock()
        if name == "vertex":
            return self._build_vertex()
        if name in CATALOG:
            return self._build_from_catalog(name)
        raise UnknownProviderError(f"unknown provider: {name!r}")

    def _build_vertex(self) -> Provider:
        """Construct the Vertex AI adapter. Operator passes the SA JSON
        either inline (PRONAOS_VERTEX_SERVICE_ACCOUNT_JSON='{...}') or
        as a path; auto-detect by leading character."""
        project = self._settings.vertex_project_id
        sa_value = self._settings.vertex_service_account_json
        region = self._settings.vertex_region
        if not project or not sa_value:
            raise ProviderNotConfiguredError(
                "vertex: VERTEX_PROJECT_ID and VERTEX_SERVICE_ACCOUNT_JSON "
                "(or GOOGLE_APPLICATION_CREDENTIALS) must both be set"
            )
        stripped = sa_value.strip()
        try:
            if stripped.startswith("{"):
                auth = VertexAuth.from_json_string(stripped)
            else:
                auth = VertexAuth.from_json_path(stripped)
        except VertexAuthError as e:
            raise ProviderNotConfiguredError(f"vertex: {e}") from e
        return VertexProvider(
            auth=auth,
            project_id=project,
            region=region,
        )

    def _build_bedrock(self) -> Provider:
        ak = self._settings.aws_access_key_id
        sk = self._settings.aws_secret_access_key
        if not ak or not sk:
            raise ProviderNotConfiguredError(
                "bedrock: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must both be set"
            )
        return BedrockProvider(
            access_key_id=ak,
            secret_access_key=sk,
            region=self._settings.aws_region,
            session_token=self._settings.aws_session_token,
        )

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

    # ---- Embedding builders --------------------------------------------------

    def _build_embedding(self, name: str) -> EmbeddingProvider:
        if name == "local":
            # No API key, no catalog entry — local sentence-transformers.
            return LocalSentenceTransformerEmbeddingProvider()
        if name not in CATALOG:
            raise UnknownProviderError(f"unknown embedding provider: {name!r}")
        entry = CATALOG[name]
        if not entry.embedding_pricing:
            raise UnknownProviderError(f"{name}: does not offer embedding models in the catalog")
        api_key = (
            getattr(self._settings, entry.settings_attr, None) if entry.settings_attr else None
        )
        if not api_key:
            raise ProviderNotConfiguredError(f"{name}: {entry.settings_attr.upper()} is not set")

        if entry.embedding_shape == "cohere":
            return CohereEmbeddingProvider(
                api_key=api_key,
                pricing=entry.embedding_pricing,
                base_url=entry.base_url,
            )
        if entry.embedding_shape == "voyage":
            return VoyageEmbeddingProvider(
                api_key=api_key,
                pricing=entry.embedding_pricing,
                base_url=entry.base_url,
            )
        # Default: OpenAI shape (covers openai, mistral, openrouter, together).
        return OpenAICompatibleEmbeddingProvider(
            provider_key=entry.key,
            base_url=entry.base_url,
            api_key=api_key,
            pricing=entry.embedding_pricing,
            default_headers=dict(entry.default_headers),
            auth_header_name=entry.auth.header_name,
            auth_header_format=entry.auth.header_format,
        )

    # ---- Rerank builders -----------------------------------------------------

    def _build_rerank(self, name: str) -> RerankProvider:
        if name not in CATALOG:
            raise UnknownProviderError(f"unknown rerank provider: {name!r}")
        entry = CATALOG[name]
        if not entry.rerank_pricing:
            raise UnknownProviderError(f"{name}: does not offer rerank models in the catalog")
        api_key = (
            getattr(self._settings, entry.settings_attr, None) if entry.settings_attr else None
        )
        if not api_key:
            raise ProviderNotConfiguredError(f"{name}: {entry.settings_attr.upper()} is not set")

        if entry.rerank_shape == "voyage":
            return VoyageRerankProvider(
                api_key=api_key,
                pricing=entry.rerank_pricing,
                base_url=entry.base_url,
            )
        # Default: cohere (the only other shape today).
        return CohereRerankProvider(
            api_key=api_key,
            pricing=entry.rerank_pricing,
            base_url=entry.base_url,
        )
