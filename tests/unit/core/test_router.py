"""Router unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pronaos.core.router import Router, RoutingError
from pronaos.providers.base import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
)
from pronaos.providers.registry import (
    ProviderRegistry,
    UnknownProviderError,
)


class _FakeProvider(Provider):
    name = "fake"

    def __init__(self, name: str) -> None:
        self.name = name  # type: ignore[misc]

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        raise NotImplementedError

    def cost_cents(self, p: int, c: int, m: str) -> int:
        return 0


class _FakeRegistry(ProviderRegistry):
    def __init__(self, keys: list[str]) -> None:  # type: ignore[super-init-not-called]
        self._available = {k: _FakeProvider(k) for k in keys}
        self._instances = {}

    def get(self, name: str) -> Provider:
        if name in self._available:
            return self._available[name]
        raise UnknownProviderError(f"unknown provider: {name!r}")


@pytest.fixture
def registry() -> _FakeRegistry:
    return _FakeRegistry(["anthropic", "groq", "openai"])


class TestResolve:
    def test_explicit_provider_prefix(self, registry: _FakeRegistry) -> None:
        r = Router(registry)
        plan = r.resolve("anthropic/claude-opus-4-7")
        assert plan.primary.name == "anthropic"
        assert plan.fallbacks == ()

    def test_different_prefix(self, registry: _FakeRegistry) -> None:
        r = Router(registry)
        plan = r.resolve("groq/llama-3.3-70b-versatile")
        assert plan.primary.name == "groq"

    def test_bare_name_with_default(self, registry: _FakeRegistry) -> None:
        r = Router(registry, default_provider="groq")
        plan = r.resolve("llama-3.3-70b-versatile")
        assert plan.primary.name == "groq"

    def test_bare_name_without_default_errors(self, registry: _FakeRegistry) -> None:
        r = Router(registry, default_provider=None)
        with pytest.raises(RoutingError):
            r.resolve("llama-3.3-70b-versatile")

    def test_unknown_provider_errors(self, registry: _FakeRegistry) -> None:
        r = Router(registry)
        with pytest.raises(RoutingError):
            r.resolve("nonexistent/model")

    def test_empty_provider_prefix_errors(self, registry: _FakeRegistry) -> None:
        r = Router(registry)
        with pytest.raises(RoutingError):
            r.resolve("/model")

    def test_empty_model_name_errors(self, registry: _FakeRegistry) -> None:
        r = Router(registry)
        with pytest.raises(RoutingError):
            r.resolve("anthropic/")


class TestFallbackChain:
    def test_fallback_provider_resolved(self, registry: _FakeRegistry) -> None:
        r = Router(
            registry,
            fallback_chains={"anthropic": ["groq", "openai"]},
        )
        plan = r.resolve("anthropic/claude-opus-4-7")
        assert plan.primary.name == "anthropic"
        assert [p.name for p in plan.fallbacks] == ["groq", "openai"]

    def test_missing_fallback_silently_dropped(self) -> None:
        # Only the primary exists — fallbacks listed but not registered are
        # dropped so the chain is still valid with fewer options.
        reg = _FakeRegistry(["anthropic"])
        r = Router(
            reg,
            fallback_chains={"anthropic": ["groq", "openai"]},
        )
        plan = r.resolve("anthropic/claude-opus-4-7")
        assert plan.primary.name == "anthropic"
        assert plan.fallbacks == ()

    def test_no_fallback_for_this_primary(self, registry: _FakeRegistry) -> None:
        r = Router(registry, fallback_chains={"anthropic": ["groq"]})
        plan = r.resolve("openai/gpt-4o")
        assert plan.fallbacks == ()
