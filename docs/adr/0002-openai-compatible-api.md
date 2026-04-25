# ADR 0002 — OpenAI-compatible API surface

- Status: Accepted
- Date: 2026-04-22

## Context

Clients integrate with the gateway via some HTTP API. Options:

1. Ship a proprietary API (full design freedom).
2. Ship OpenAI's `/v1/chat/completions` shape as the canonical surface.
3. Support multiple native shapes (OpenAI, Anthropic Messages, etc.) side-by-side.

## Decision

Adopt the **OpenAI-compatible** API (option 2) as the canonical client contract. Support the Anthropic Messages shape as a secondary adapter in phase 4+.

## Rationale

- Every major LLM client library already speaks this shape.
- Zero-migration story: `client = OpenAI(base_url="https://gateway.internal/v1")` and existing apps route through the gateway unchanged.
- Portkey, LiteLLM, OpenRouter, Groq, Together, and vLLM all converged on this surface. Bucking the convention loses compatibility with zero upside.
- Provider-specific features (Anthropic tools, Bedrock guardrails, Gemini safety) are modelled as routing-time *provider hints* rather than API-surface divergence.

## Consequences

- Non-OpenAI features that don't map cleanly (e.g. Anthropic cache_control) are exposed via optional `extra_body` fields; we document each explicitly.
- Streaming follows OpenAI's SSE conventions verbatim.
- `model` field accepts provider-prefixed names (`anthropic/claude-opus-4-7`, `bedrock/anthropic.claude-sonnet-4-6`) as well as bare model names with tenant-level default routing.
