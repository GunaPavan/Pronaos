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
    # Best-effort p50 latency for the provider's typical model family.
    # Used by latency-aware routing strategies. None = unknown; the
    # scorer treats unknown as the highest-latency tier.
    typical_p50_ms: int | None = None
    default_headers: dict[str, str] = field(default_factory=dict)
    auth: AuthConfig = field(default_factory=AuthConfig)
    notes: str = ""


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
                supports_tools=True, supports_streaming=True, max_context_tokens=128_000
            ),
        },
        typical_p50_ms=250,  # Groq is the fast-tier reference.
        notes="Free tier; fast inference; open-weight models.",
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
