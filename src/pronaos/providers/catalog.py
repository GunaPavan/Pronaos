"""Provider catalog.

The catalog is the data that makes "12+ providers supported" a truthful claim
rather than a marketing line. Each entry describes a provider exactly once:

- ``key``: the prefix clients use in ``model`` (e.g. ``groq/llama-3.3-70b``).
- ``base_url``: the provider's OpenAI-compatible endpoint root (no trailing
  ``/chat/completions`` — the adapter appends that).
- ``settings_attr``: the field on ``Settings`` holding the API key.
- ``pricing``: per-model pricing in hundredths-of-a-cent per million tokens.
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
class ProviderCatalogEntry:
    key: str
    base_url: str
    settings_attr: str
    pricing: dict[str, Pricing] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)
    auth: AuthConfig = field(default_factory=AuthConfig)
    notes: str = ""


# Pricing values below are best-effort representative numbers. Update from
# provider price lists as they change; ``0`` means "free tier / not priced".
_FREE = Pricing(0, 0)


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
    ),
    "fireworks": ProviderCatalogEntry(
        key="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        settings_attr="fireworks_api_key",
        pricing={
            "accounts/fireworks/models/llama-v3p3-70b-instruct": Pricing(90_000, 90_000),
        },
    ),
    "cerebras": ProviderCatalogEntry(
        key="cerebras",
        base_url="https://api.cerebras.ai/v1",
        settings_attr="cerebras_api_key",
        pricing={
            "llama-3.3-70b": Pricing(85_000, 120_000),
        },
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
    ),
    "deepseek": ProviderCatalogEntry(
        key="deepseek",
        base_url="https://api.deepseek.com",
        settings_attr="deepseek_api_key",
        pricing={
            "deepseek-chat": Pricing(27_000, 110_000),
            "deepseek-reasoner": Pricing(55_000, 219_000),
        },
    ),
    "mistral": ProviderCatalogEntry(
        key="mistral",
        base_url="https://api.mistral.ai/v1",
        settings_attr="mistral_api_key",
        pricing={
            "mistral-large-latest": Pricing(200_000, 600_000),
            "mistral-small-latest": Pricing(20_000, 60_000),
        },
    ),
    "perplexity": ProviderCatalogEntry(
        key="perplexity",
        base_url="https://api.perplexity.ai",
        settings_attr="perplexity_api_key",
        pricing={
            "sonar": Pricing(100_000, 100_000),
            "sonar-pro": Pricing(300_000, 1_500_000),
        },
    ),
    "xai": ProviderCatalogEntry(
        key="xai",
        base_url="https://api.x.ai/v1",
        settings_attr="xai_api_key",
        pricing={
            "grok-4": Pricing(300_000, 1_500_000),
        },
    ),
    # ------------------------ Local / self-hosted ---------------------------
    "ollama": ProviderCatalogEntry(
        key="ollama",
        base_url="http://localhost:11434/v1",
        settings_attr="",  # handled specially: no key; use OLLAMA_ENABLED flag
        pricing={},  # always free (your own hardware)
        notes="Run open models locally. No API key; enable with OLLAMA_ENABLED=true.",
    ),
}


def all_provider_keys() -> list[str]:
    """Return catalog keys in a stable order."""
    return list(CATALOG.keys())
