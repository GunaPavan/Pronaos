"""Model routing.

Parses the client's ``model`` field and resolves it to a primary provider plus
an optional fallback chain. Keeps the parsing one-way (provider determined
from the request, never inferred from secondary context) so the behaviour is
easy to reason about and test.

Phase 2 uses a static default for bare model names; per-tenant routing is a
Phase 3+ concern that layers on top of this without changing the interface.
"""

from __future__ import annotations

from dataclasses import dataclass

from pronaos.providers.base import Provider, ProviderError
from pronaos.providers.registry import (
    ProviderRegistry,
    UnknownProviderError,
)


class RoutingError(ProviderError):
    """Routing failed: unknown provider, or no route configured."""

    retryable = False
    status = 400


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """The ordered provider chain for a single request."""

    primary: Provider
    fallbacks: tuple[Provider, ...] = ()

    def chain(self) -> tuple[Provider, ...]:
        return (self.primary, *self.fallbacks)


class Router:
    """Resolves ``model`` strings to providers via the registry.

    Parsing rules:
    - ``<provider_key>/<model_name>`` — explicit; goes to that provider.
    - Bare ``<model_name>`` — routes to ``default_provider`` (Phase 2) or a
      per-tenant override (later phase).
    - Fallbacks are resolved from ``fallback_chains`` keyed by primary
      provider key; unknown primaries yield a ``RoutingError``.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        default_provider: str | None = None,
        fallback_chains: dict[str, list[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._default = default_provider
        self._fallbacks = fallback_chains or {}

    def resolve(self, model: str) -> RoutingPlan:
        provider_key, _ = self._split(model)
        try:
            primary = self._registry.get(provider_key)
        except UnknownProviderError as e:
            raise RoutingError(str(e)) from e

        fallback_keys = self._fallbacks.get(provider_key, [])
        fallbacks: list[Provider] = []
        for fk in fallback_keys:
            try:
                fallbacks.append(self._registry.get(fk))
            except ProviderError:
                # Skip fallbacks we can't construct (missing key, disabled).
                # Dropping silently is acceptable here because fallbacks are
                # best-effort; the chain is still valid with fewer options.
                continue

        return RoutingPlan(primary=primary, fallbacks=tuple(fallbacks))

    # ---- Helpers -------------------------------------------------------------

    def _split(self, model: str) -> tuple[str, str]:
        if "/" in model:
            provider_key, _, name = model.partition("/")
            if not provider_key or not name:
                raise RoutingError(f"malformed model string: {model!r}")
            return provider_key, name

        if self._default is None:
            raise RoutingError(f"bare model name {model!r} and no default provider configured")
        return self._default, model
