"""Provider interface.

Every upstream LLM (OpenAI, Anthropic, Bedrock, Gemini, ...) is expressed as a
concrete subclass of `Provider`. The router selects a provider per request and
the provider handles: request translation, the upstream call (streamed or not),
response translation back to OpenAI-compatible shape, and token accounting.

Design notes:
- `chat_completion` is an async generator so streaming is natural and
  backpressure is preserved. Non-streaming callers just consume once.
- Providers must not perform their own retries; retry policy is centralised in
  the router so it sees the full error budget.
- Providers must translate upstream error shapes into `ProviderError` subclasses
  so the router can classify them (retryable, rate-limited, auth, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChatCompletionRequest:
    """OpenAI-compatible chat completion request, post-auth, post-policy."""

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChatCompletionChunk:
    """One chunk of a streamed response, or the full response when stream=False."""

    content_delta: str
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict[str, Any] | None = None


class ProviderError(Exception):
    """Base class for provider errors. Carries retry semantics.

    ``status`` and ``retryable`` are class defaults; either can be overridden
    per instance at construction so adapters don't need to subclass for every
    shade of upstream failure.
    """

    retryable: bool = False
    status: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        if retryable is not None:
            self.retryable = retryable


class RateLimitError(ProviderError):
    retryable = True
    status = 429


class AuthError(ProviderError):
    retryable = False
    status = 401


class UpstreamTimeoutError(ProviderError):
    retryable = True
    status = 504


class Provider(ABC):
    """Abstract upstream LLM provider."""

    name: str

    @abstractmethod
    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Execute a chat completion and yield chunks.

        For `stream=False` callers, yield a single chunk containing the full
        response. For `stream=True`, yield incremental deltas.
        """
        ...

    @abstractmethod
    def cost_cents(self, prompt_tokens: int, completion_tokens: int, model: str) -> int:
        """Return the cost of a completion in hundredths of a cent for accurate accounting."""
        ...

    async def aclose(self) -> None:
        """Release any provider-owned resources (connection pool, auth token, etc.).

        Default is a no-op so stateless providers don't need boilerplate.
        """
        return None
