"""Provider catalog.

The catalog is the data that makes "12 providers supported" a truthful claim
rather than a marketing line. Each entry describes a provider exactly once:

- ``key``: the prefix clients use in ``model`` (e.g. ``groq/llama-3.3-70b``).
- ``base_url``: the provider's OpenAI-compatible endpoint root (no trailing
  ``/chat/completions`` — the adapter appends that).
- ``settings_attr``: the field on ``Settings`` holding the API key.
- ``pricing``: per-model pricing in hundredths-of-a-cent per million tokens.
- ``capabilities``: per-model capability flags (tools, streaming, vision,
  max_context). Used by the cost-aware router (Phase 21) to filter the
  pool of eligible models before scoring.
- ``typical_p50_ms``: approximate provider-level p50 latency in
  milliseconds. Used by latency-weighted routing strategies. Best-effort
  numbers from public benchmarks; refine as you collect real measurements.
- ``auth`` (optional): header name + format, for providers that don't use the
  ``Authorization: Bearer`` convention (Azure OpenAI).

Adding a new provider = one entry in this file. Not one new adapter class.
That is the leverage of the OpenAI-compat pattern.

Native adapters (Anthropic today; Bedrock/Gemini in Phase 2.5) are *not* in
this catalog — they are registered separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pronaos.providers.openai_compat import Pricing


@dataclass(frozen=True, slots=True)
class AuthConfig:
    header_name: str = "Authorization"
    header_format: str = "Bearer {key}"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Per-model capability flags used by the cost-aware router.

    Default values are intentionally conservative — if a model isn't
    explicitly listed in a provider's ``capabilities`` dict, the
    scorer treats it as "supports basic chat + streaming, no tools,
    no vision, 8K context." This keeps the eligibility filter from
    accidentally routing tool-calling requests to a model that doesn't
    understand them.
    """

    supports_tools: bool = False
    supports_streaming: bool = True
    supports_vision: bool = False
    max_context_tokens: int = 8192


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    key: str
    base_url: str
    settings_attr: str
    pricing: dict[str, Pricing] = field(default_factory=dict)
    # Per-model capability matrix. Keyed by the same model name used in
    # ``pricing``. Models not listed get the ModelCapabilities default.
    capabilities: dict[str, ModelCapabilities] = field(default_factory=dict)
    # Official model/pricing reference URL for this provider.
    # When adding a new model: visit this URL, copy the exact model ID
    # from the provider's docs, then add it to ``pricing`` + ``capabilities``
    # above. Do NOT infer model codes — use only what the docs show.
    docs_url: str = ""
    # Best-effort p50 latency for the provider's typical model family.
    # Used by latency-aware routing strategies. None = unknown; the
    # scorer treats unknown as the highest-latency tier.
    typical_p50_ms: int | None = None
    default_headers: dict[str, str] = field(default_factory=dict)
    auth: AuthConfig = field(default_factory=AuthConfig)
    notes: str = ""
    # Phase 31: per-model embedding pricing, separate from ``pricing``.
    # ``output_hcents_per_mtok`` is ignored for embeddings (a vector is
    # not text we charge for). Models present here are the ones the
    # ``/v1/embeddings`` endpoint accepts under this provider key.
    embedding_pricing: dict[str, Pricing] = field(default_factory=dict)
    # Embedding-shape hint: ``"openai"`` (default OpenAI shape), ``"cohere"``,
    # ``"voyage"``. The registry picks the matching adapter from this hint.
    embedding_shape: str = "openai"
    # Phase 32: per-model rerank pricing. ``input_hcents_per_mtok`` semantics
    # differ by ``rerank_shape``:
    #   - "cohere": per-CALL hcents (Cohere bills per "search unit", one
    #     call up to 100 documents = one unit, regardless of token count).
    #   - "voyage": per-million-input-tokens (sum of query + all documents).
    # ``output_hcents_per_mtok`` is unused. The misnaming is intentional —
    # reusing Pricing keeps the catalog uniform; the rerank adapter's
    # cost_hcents() knows how to interpret its own row.
    rerank_pricing: dict[str, Pricing] = field(default_factory=dict)
    # Rerank-shape hint: ``"cohere"`` or ``"voyage"``. Registry picks the
    # matching adapter.
    rerank_shape: str = "cohere"


# Pricing values below are best-effort representative numbers. Update from
# provider price lists as they change; ``0`` means "free tier / not priced".
_FREE = Pricing(0, 0)


# Common capability shorthand. Most modern chat models support tools and
# streaming; vision is the discriminator.
_TOOLS_STREAMING = ModelCapabilities(
    supports_tools=True, supports_streaming=True, supports_vision=False
)
_TOOLS_STREAMING_VISION = ModelCapabilities(
    supports_tools=True, supports_streaming=True, supports_vision=True
)
_STREAMING_ONLY = ModelCapabilities(
    supports_tools=False, supports_streaming=True, supports_vision=False
)


CATALOG: dict[str, ProviderCatalogEntry] = {
    # ------------------------ Free / open-weight hosts ----------------------
    "groq": ProviderCatalogEntry(
        key="groq",
        base_url="https://api.groq.com/openai/v1",
        settings_attr="groq_api_key",
        pricing={
            # Currently supported (May 2026):
            "llama-3.3-70b-versatile": Pricing(59_000, 79_000),
            "llama-3.1-8b-instant": Pricing(5_000, 8_000),
            # Llama 4 family (Maverick + Scout) — public Groq pricing.
            "meta-llama/llama-4-scout-17b-16e-instruct": Pricing(11_000, 34_000),
            # Decommissioned in early 2026 — kept for historical migrations
            # only; Groq will return HTTP 400 for these now.
            "mixtral-8x7b-32768": Pricing(24_000, 24_000),
            "qwen-qwq-32b": Pricing(29_000, 39_000),
        },
        capabilities={
            "llama-3.3-70b-versatile": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
            "llama-3.1-8b-instant": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
            "meta-llama/llama-4-scout-17b-16e-instruct": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                # Llama 4 Scout is multimodal (text + vision) — Phase 41.
                supports_vision=True,
                max_context_tokens=128_000,
            ),
        },
        typical_p50_ms=250,  # Groq is the fast-tier reference.
        notes="Free tier; fast inference; open-weight models.",
        docs_url="https://console.groq.com/docs/models",
    ),
    "together": ProviderCatalogEntry(
        key="together",
        base_url="https://api.together.xyz/v1",
        settings_attr="together_api_key",
        pricing={
            "meta-llama/Llama-3.3-70B-Instruct-Turbo": Pricing(88_000, 88_000),
            "deepseek-ai/DeepSeek-V3": Pricing(125_000, 125_000),
        },
        capabilities={
            "meta-llama/Llama-3.3-70B-Instruct-Turbo": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
            "deepseek-ai/DeepSeek-V3": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=64_000
            ),
        },
        typical_p50_ms=900,
        docs_url="https://docs.together.ai/docs/inference-models",
    ),
    "fireworks": ProviderCatalogEntry(
        key="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        settings_attr="fireworks_api_key",
        pricing={
            "accounts/fireworks/models/llama-v3p3-70b-instruct": Pricing(90_000, 90_000),
        },
        capabilities={
            "accounts/fireworks/models/llama-v3p3-70b-instruct": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
        },
        typical_p50_ms=700,
        docs_url="https://fireworks.ai/models",
    ),
    "cerebras": ProviderCatalogEntry(
        key="cerebras",
        base_url="https://api.cerebras.ai/v1",
        settings_attr="cerebras_api_key",
        pricing={
            "llama-3.3-70b": Pricing(85_000, 120_000),
        },
        capabilities={
            "llama-3.3-70b": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
        },
        typical_p50_ms=200,  # Cerebras is even faster than Groq on big models.
        notes="World's fastest inference on frontier open models.",
        docs_url="https://inference-docs.cerebras.ai/supported-models",
    ),
    "openrouter": ProviderCatalogEntry(
        key="openrouter",
        base_url="https://openrouter.ai/api/v1",
        settings_attr="openrouter_api_key",
        default_headers={
            "HTTP-Referer": "https://github.com/GunaPavan/pronaos",
            "X-Title": "Pronaos",
        },
        typical_p50_ms=1200,  # OR adds a hop; varies with underlying model.
        notes="Unified access to hundreds of models; OR sets its own pricing.",
        docs_url="https://openrouter.ai/models",
    ),
    # ------------------------ Frontier providers ----------------------------
    "openai": ProviderCatalogEntry(
        key="openai",
        base_url="https://api.openai.com/v1",
        settings_attr="openai_api_key",
        pricing={
            "gpt-4o": Pricing(250_000, 1_000_000),
            "gpt-4o-mini": Pricing(15_000, 60_000),
        },
        capabilities={
            "gpt-4o": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=128_000,
            ),
            "gpt-4o-mini": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=128_000,
            ),
        },
        typical_p50_ms=800,
        docs_url="https://platform.openai.com/docs/models",
        # Phase 31: embedding models on the same key+endpoint.
        # Pricing: hcents per million input tokens.
        # text-embedding-3-small: $0.02/Mtok → 2000 hcents
        # text-embedding-3-large: $0.13/Mtok → 13_000 hcents
        # text-embedding-ada-002 (legacy): $0.10/Mtok → 10_000 hcents
        embedding_pricing={
            "text-embedding-3-small": Pricing(2_000, 0),
            "text-embedding-3-large": Pricing(13_000, 0),
            "text-embedding-ada-002": Pricing(10_000, 0),
        },
        embedding_shape="openai",
    ),
    "deepseek": ProviderCatalogEntry(
        key="deepseek",
        base_url="https://api.deepseek.com",
        settings_attr="deepseek_api_key",
        pricing={
            "deepseek-chat": Pricing(27_000, 110_000),
            "deepseek-reasoner": Pricing(55_000, 219_000),
        },
        capabilities={
            "deepseek-chat": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=64_000
            ),
            "deepseek-reasoner": ModelCapabilities(
                supports_tools=False,  # reasoner doesn't expose function calling
                supports_streaming=True,
                max_context_tokens=64_000,
            ),
        },
        typical_p50_ms=1100,
        docs_url="https://api-docs.deepseek.com/quick_start/pricing",
    ),
    "mistral": ProviderCatalogEntry(
        key="mistral",
        base_url="https://api.mistral.ai/v1",
        settings_attr="mistral_api_key",
        pricing={
            "mistral-large-latest": Pricing(200_000, 600_000),
            "mistral-small-latest": Pricing(20_000, 60_000),
        },
        capabilities={
            "mistral-large-latest": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
            "mistral-small-latest": ModelCapabilities(
                supports_tools=True, supports_streaming=True, max_context_tokens=32_000
            ),
        },
        typical_p50_ms=900,
        docs_url="https://docs.mistral.ai/getting-started/models/models_overview/",
        # Mistral's embedding endpoint speaks OpenAI shape. ``mistral-embed``
        # is $0.10/Mtok = 10_000 hcents.
        embedding_pricing={
            "mistral-embed": Pricing(10_000, 0),
        },
        embedding_shape="openai",
    ),
    "perplexity": ProviderCatalogEntry(
        key="perplexity",
        base_url="https://api.perplexity.ai",
        settings_attr="perplexity_api_key",
        pricing={
            "sonar": Pricing(100_000, 100_000),
            "sonar-pro": Pricing(300_000, 1_500_000),
        },
        capabilities={
            "sonar": _STREAMING_ONLY,
            "sonar-pro": _STREAMING_ONLY,
        },
        typical_p50_ms=2000,  # Sonar does live web search; inherently slower.
        docs_url="https://docs.perplexity.ai/models/model-cards",
    ),
    "xai": ProviderCatalogEntry(
        key="xai",
        base_url="https://api.x.ai/v1",
        settings_attr="xai_api_key",
        pricing={
            "grok-4": Pricing(300_000, 1_500_000),
        },
        capabilities={
            "grok-4": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=256_000,
            ),
        },
        typical_p50_ms=1000,
        docs_url="https://docs.x.ai/docs/models",
    ),
    # ------------------------ Embedding-only providers ----------------------
    # These are chat-less catalog entries — pricing/capabilities are empty,
    # only embedding_pricing is populated. The registry handles them by
    # the embedding_shape hint instead of building an OpenAICompatibleProvider.
    "cohere": ProviderCatalogEntry(
        key="cohere",
        base_url="https://api.cohere.com",
        settings_attr="cohere_api_key",
        # Cohere embed-v3 family: $0.10/Mtok = 10_000 hcents
        embedding_pricing={
            "embed-english-v3.0": Pricing(10_000, 0),
            "embed-multilingual-v3.0": Pricing(10_000, 0),
            "embed-english-light-v3.0": Pricing(10_000, 0),
        },
        embedding_shape="cohere",
        # Rerank pricing: per-call (one search unit per call, up to 100 docs).
        # $2 / 1000 search units = $0.002/call = 0.2 cents = 20 hcents per call.
        # The Pricing field input_hcents_per_mtok is reused as "per-call hcents"
        # for cohere rerank — see catalog.py docstring on rerank_pricing.
        rerank_pricing={
            "rerank-english-v3.0": Pricing(20, 0),
            "rerank-multilingual-v3.0": Pricing(20, 0),
            "rerank-english-v3.5": Pricing(20, 0),
        },
        rerank_shape="cohere",
        typical_p50_ms=300,
        notes="Embedding + rerank provider. /v2/embed and /v2/rerank shapes.",
        docs_url="https://docs.cohere.com/docs/models",
    ),
    "voyage": ProviderCatalogEntry(
        key="voyage",
        base_url="https://api.voyageai.com/v1",
        settings_attr="voyage_api_key",
        # Voyage pricing (May 2026):
        # voyage-3: $0.06/Mtok = 6_000 hcents
        # voyage-3-lite: $0.02/Mtok = 2_000 hcents
        # voyage-large-2: $0.12/Mtok = 12_000 hcents
        # voyage-code-2: $0.12/Mtok = 12_000 hcents
        embedding_pricing={
            "voyage-3": Pricing(6_000, 0),
            "voyage-3-lite": Pricing(2_000, 0),
            "voyage-large-2": Pricing(12_000, 0),
            "voyage-code-2": Pricing(12_000, 0),
        },
        embedding_shape="voyage",
        # Rerank pricing: per-token. rerank-2 $0.05/Mtok = 5_000 hcents/Mtok;
        # rerank-lite-2 $0.02/Mtok = 2_000 hcents/Mtok.
        rerank_pricing={
            "rerank-2": Pricing(5_000, 0),
            "rerank-lite-2": Pricing(2_000, 0),
        },
        rerank_shape="voyage",
        typical_p50_ms=400,
        notes="Embedding + rerank provider. Frontier-quality retrieval models.",
        docs_url="https://docs.voyageai.com/docs/embeddings",
    ),
    # ------------------------ AWS Bedrock (native, SigV4-signed) ------------
    # Phase 42 — Bedrock-hosted models behind one provider key. Unlike the
    # OpenAI-compat catalog entries, this one is consumed by ``BedrockProvider``
    # (not ``OpenAICompatibleProvider``); the ``base_url`` is a template that
    # the adapter renders with the configured region at construction time.
    # Auth is SigV4 over httpx, not Bearer — the registry knows to skip the
    # auth header when building this provider.
    #
    # Pricing is in hundredths-of-a-cent per million tokens, matching the
    # rest of the catalog. Source: AWS Bedrock public pricing page,
    # us-east-1 rates as of May 2026.
    #
    # Bedrock model IDs use the AWS-style ``vendor.model-name-version`` shape
    # (e.g. ``anthropic.claude-3-5-haiku-20241022-v1:0``). The colon and dot
    # in the ID are valid in our prefix scheme because the chat handler
    # treats everything after the first ``/`` as the opaque model name.
    "bedrock": ProviderCatalogEntry(
        key="bedrock",
        # The runtime endpoint is per-region; the adapter builds the full
        # URL by interpolating ``{region}`` from settings. We store the
        # template here so static analysis still finds the placeholder.
        base_url="https://bedrock-runtime.{region}.amazonaws.com",
        settings_attr="aws_access_key_id",
        pricing={
            # Anthropic on Bedrock — same model bytes as direct Anthropic
            # API, billed by AWS at the prices below.
            "anthropic.claude-3-5-haiku-20241022-v1:0": Pricing(80_000, 400_000),
            "anthropic.claude-3-5-sonnet-20241022-v2:0": Pricing(300_000, 1_500_000),
            # Meta Llama 3 on Bedrock — text-only.
            "meta.llama3-70b-instruct-v1:0": Pricing(265_000, 350_000),
            "meta.llama3-1-70b-instruct-v1:0": Pricing(265_000, 350_000),
            # Amazon Nova — Amazon's own multimodal foundation model.
            # Nova Pro: $0.80 / Mtok input, $3.20 / Mtok output.
            "amazon.nova-pro-v1:0": Pricing(80_000, 320_000),
            "amazon.nova-lite-v1:0": Pricing(6_000, 24_000),
            # Mistral on Bedrock — Mistral Large v2.
            "mistral.mistral-large-2407-v1:0": Pricing(200_000, 600_000),
        },
        capabilities={
            "anthropic.claude-3-5-haiku-20241022-v1:0": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=200_000,
            ),
            "anthropic.claude-3-5-sonnet-20241022-v2:0": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=200_000,
            ),
            "meta.llama3-70b-instruct-v1:0": ModelCapabilities(
                supports_tools=False,  # Llama on Bedrock has no native tool-call shape today
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=8192,
            ),
            "meta.llama3-1-70b-instruct-v1:0": ModelCapabilities(
                supports_tools=False,
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=128_000,
            ),
            "amazon.nova-pro-v1:0": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=300_000,
            ),
            "amazon.nova-lite-v1:0": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=300_000,
            ),
            "mistral.mistral-large-2407-v1:0": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                max_context_tokens=128_000,
            ),
        },
        typical_p50_ms=1500,  # Bedrock adds an AWS hop; first-token latency
        # is noticeably higher than direct-provider APIs on small prompts.
        notes=(
            "AWS Bedrock-hosted models. SigV4-signed; requires "
            "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (or IRSA in-cluster). "
            "Per-region; set AWS_REGION (default us-east-1)."
        ),
        docs_url="https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html",
    ),
    # ------------------------ Google Cloud Vertex AI ------------------------
    # Vertex hosts foundation models across multiple publishers. The
    # model-ID convention is ``vertex/{publisher}/{model}`` — the
    # adapter strips ``vertex/`` and splits the rest on the first ``/``
    # to route per-publisher.
    "vertex": ProviderCatalogEntry(
        key="vertex",
        # Region-specific endpoint; adapter interpolates region from
        # settings. Stored as a template here so static analysis still
        # spots the placeholder.
        base_url="https://{region}-aiplatform.googleapis.com",
        settings_attr="vertex_project_id",
        pricing={
            # Gemini family — pricing per GCP public pricing as of
            # mid-2026. Values are hundredths-of-a-cent per Mtok
            # (matching every other entry in the catalog).
            # Gemini 1.5 Flash: $0.075/Mtok input, $0.30/Mtok output
            "google/gemini-1.5-flash": Pricing(7_500, 30_000),
            # Gemini 1.5 Pro: $1.25/Mtok input, $5.00/Mtok output
            # (pricing tiers: prompts <128K tokens use these rates)
            "google/gemini-1.5-pro": Pricing(125_000, 500_000),
            # Gemini 2.0 Flash: $0.10/Mtok input, $0.40/Mtok output
            "google/gemini-2.0-flash": Pricing(10_000, 40_000),
            # Gemini 2.5 Pro: $1.25/Mtok input, $10.00/Mtok output
            "google/gemini-2.5-pro": Pricing(125_000, 1_000_000),
            # Claude on Vertex — same model bytes as direct Anthropic;
            # GCP bills at rates that match Anthropic's direct pricing.
            "anthropic/claude-3-5-haiku@20241022": Pricing(80_000, 400_000),
            "anthropic/claude-3-5-sonnet@20241022": Pricing(300_000, 1_500_000),
        },
        capabilities={
            "google/gemini-1.5-flash": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=1_000_000,
            ),
            "google/gemini-1.5-pro": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=2_000_000,
            ),
            "google/gemini-2.0-flash": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=1_000_000,
            ),
            "google/gemini-2.5-pro": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=2_000_000,
            ),
            "anthropic/claude-3-5-haiku@20241022": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=200_000,
            ),
            "anthropic/claude-3-5-sonnet@20241022": ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                max_context_tokens=200_000,
            ),
        },
        typical_p50_ms=1200,  # GCP region adds a hop; comparable to Bedrock
        notes=(
            "Vertex AI hosted models. GCP service-account JWT auth; "
            "requires VERTEX_PROJECT_ID + VERTEX_SERVICE_ACCOUNT_JSON "
            "(or GOOGLE_APPLICATION_CREDENTIALS pointing at the SA "
            "JSON file). Per-region; set VERTEX_REGION (default "
            "us-central1)."
        ),
        docs_url="https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models",
    ),
    # ------------------------ Local / self-hosted ---------------------------
    "ollama": ProviderCatalogEntry(
        key="ollama",
        base_url="http://localhost:11434/v1",
        settings_attr="",  # handled specially: no key; use OLLAMA_ENABLED flag
        pricing={},  # always free (your own hardware)
        # Capabilities depend on which local model is loaded; default
        # capabilities (no tools, streaming, 8K) is the safe assumption.
        typical_p50_ms=400,  # depends entirely on local hardware.
        notes="Run open models locally. No API key; enable with OLLAMA_ENABLED=true.",
        docs_url="https://ollama.com/library",
    ),
}


def all_provider_keys() -> list[str]:
    """Return catalog keys in a stable order."""
    return list(CATALOG.keys())


def get_capabilities(provider_key: str, model_name: str) -> ModelCapabilities:
    """Look up the capability record for a (provider, model) pair.

    Falls back to ``ModelCapabilities()`` defaults if the provider exists
    but the model isn't explicitly listed — conservative behaviour
    appropriate for the routing eligibility filter.
    """
    entry = CATALOG.get(provider_key)
    if entry is None:
        return ModelCapabilities()
    return entry.capabilities.get(model_name, ModelCapabilities())


def get_pricing(provider_key: str, model_name: str) -> Pricing | None:
    """Look up pricing for a (provider, model) pair.

    Returns ``None`` if the model has no published pricing (e.g. Ollama,
    OpenRouter pass-through). Callers must treat None as "ineligible for
    cost-aware routing" — we won't guess at a price.
    """
    entry = CATALOG.get(provider_key)
    if entry is None:
        return None
    return entry.pricing.get(model_name)
