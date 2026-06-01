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
    """OpenAI-compatible chat completion request, post-auth, post-policy.

    ``tools`` is the OpenAI shape:
        ``[{"type":"function","function":{"name":..., "description":..., "parameters":...}}]``

    Providers that natively speak this shape (Groq, OpenAI, all OpenAI-compat
    providers) get a verbatim pass-through. Native Anthropic translates into
    ``{"name":..., "description":..., "input_schema":...}`` on the way out.

    ``tool_choice`` is "auto" | "none" | {"type":"function","function":{"name":...}}
    same as OpenAI. Anthropic translates to its own ``tool_choice`` shape.
    """

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    # Phase 39 — OpenAI-shape response_format. Adapters that natively
    # support it (OpenAI-compat path) forward it verbatim to the
    # upstream wire body; Anthropic ignores it (no native equivalent
    # today — the chat handler injects a schema-guided system message
    # in that path before calling the adapter).
    response_format: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChatCompletionChunk:
    """One chunk of a streamed response, or the full response when stream=False.

    ``tool_calls`` carries OpenAI-shape tool invocations the model emitted.
    Providers must translate native shapes (Anthropic's ``tool_use`` content
    block, etc.) into this canonical form before returning. Shape:
        ``[{"id":..., "type":"function", "function":{"name":..., "arguments":<json-string>}}]``

    The ``arguments`` field is a JSON-encoded string (matching OpenAI exactly),
    not a parsed object — this lets clients that pin the OpenAI schema
    consume the response without any reshaping.

    Phase 34: ``cache_creation_tokens`` + ``cache_read_tokens`` surface
    Anthropic's prompt-caching usage block. Both default to 0 on the
    OpenAI-compat path (OpenAI doesn't ship prompt caching today).
    Anthropic adapter sets them from ``usage.cache_creation_input_tokens``
    and ``usage.cache_read_input_tokens`` respectively, on both
    streaming (``message_start`` event) and non-streaming paths.

    Phase 56: ``reasoning_tokens`` + ``reasoning_content`` surface the
    extended-thinking / chain-of-thought signal across providers.
    Per-provider semantics:

    - **OpenAI o1/o3 + DeepSeek R1** (OpenAI-compat path): both expose
      ``usage.completion_tokens_details.reasoning_tokens``; that count
      is ALREADY included in ``completion_tokens``, so cost math is
      unchanged — Pronaos surfaces it for visibility. DeepSeek
      additionally carries ``message.reasoning_content`` (the CoT
      text); OpenAI o-series does not expose CoT text. Pronaos
      preserves whichever the upstream sends as ``reasoning_content``.
    - **Anthropic extended thinking** (direct + Bedrock + Vertex):
      thinking blocks appear as ``content[i].type == "thinking"`` with
      their own ``thinking`` text field. Anthropic does NOT expose a
      separate thinking-token count — those tokens ARE counted toward
      ``usage.output_tokens``. Pronaos estimates ``reasoning_tokens``
      from the thinking text length (~4 chars/token, char-bounded
      lower bound) for visibility; cost math is unchanged because
      output_tokens already includes them.
    - **Gemini thinking models** (2.0 Flash Thinking, 2.5 Pro):
      ``usageMetadata.thoughtsTokenCount`` is a SEPARATE billable
      count that Gemini EXCLUDES from ``candidatesTokenCount``.
      Pronaos's Vertex adapter ADDS it to ``completion_tokens`` so
      cost math bills it correctly — without this, Pronaos was
      under-counting Gemini thinking-mode spend by 100% of the
      thinking portion. The raw thoughts count also lands in
      ``reasoning_tokens`` for visibility.

    All providers default ``reasoning_tokens`` to 0 and
    ``reasoning_content`` to None when the upstream doesn't expose
    them — non-reasoning models see zero behavioural change.
    """

    content_delta: str
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: list[dict[str, Any]] | None = None
    raw: dict[str, Any] | None = None
    # Phase 34: Anthropic prompt-cache stats. None when the provider
    # doesn't expose them; integers when the provider does (including 0).
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    # Phase 56: reasoning / extended-thinking surface. ``reasoning_tokens``
    # is the count the upstream reports (OpenAI/DeepSeek) or that Pronaos
    # estimates (Anthropic) / extracts from a separate billable field
    # (Gemini). ``reasoning_content`` is the CoT text when the upstream
    # ships it (DeepSeek R1, Anthropic thinking blocks) — None
    # otherwise. Defaults make the field optional for every adapter.
    reasoning_tokens: int | None = None
    reasoning_content: str | None = None


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
    def cost_cents(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        *,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> int:
        """Return the cost of a completion in hundredths of a cent.

        ``cache_creation_tokens`` and ``cache_read_tokens`` are Anthropic
        prompt-cache fields (Phase 34). Providers that don't support
        prompt caching ignore them — the default of 0 makes existing
        adapters callable without modification.

        ``prompt_tokens`` should be the count of *non-cached* input tokens
        (Anthropic's ``input_tokens`` field excludes cache_read/creation
        already). Providers without prompt caching pass the full prompt
        count as before.
        """
        ...

    async def aclose(self) -> None:
        """Release any provider-owned resources (connection pool, auth token, etc.).

        Default is a no-op so stateless providers don't need boilerplate.
        """
        return None
