"""Live integration test against Groq's free tier.

Skipped by default. Run intentionally with:

    GROQ_API_KEY=gsk_... pytest -m integration tests/integration/test_groq_live.py

Free on Groq's free tier — no credit card, generous rate limits. Great way to
prove the gateway works end-to-end without spending money.
"""

from __future__ import annotations

import os

import pytest

from pronaos.providers.base import ChatCompletionRequest
from pronaos.providers.catalog import CATALOG
from pronaos.providers.openai_compat import OpenAICompatibleProvider

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set; run with key to enable.",
    ),
]


MODEL = "llama-3.1-8b-instant"  # fastest + cheapest on the Groq free tier


def _make_provider() -> OpenAICompatibleProvider:
    entry = CATALOG["groq"]
    return OpenAICompatibleProvider(
        provider_key=entry.key,
        base_url=entry.base_url,
        api_key=os.environ["GROQ_API_KEY"],
        pricing=entry.pricing,
        default_headers=dict(entry.default_headers),
        auth_header_name=entry.auth.header_name,
        auth_header_format=entry.auth.header_format,
    )


@pytest.mark.asyncio
async def test_live_non_streaming_returns_content() -> None:
    provider = _make_provider()
    try:
        req = ChatCompletionRequest(
            model=f"groq/{MODEL}",
            messages=[{"role": "user", "content": "reply with the single word: pong"}],
            max_tokens=16,
        )
        stream = await provider.chat_completion(req)
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.content_delta.strip() != ""
    assert chunk.completion_tokens and chunk.completion_tokens > 0


@pytest.mark.asyncio
async def test_live_streaming_yields_at_least_one_delta() -> None:
    provider = _make_provider()
    deltas: list[str] = []
    try:
        req = ChatCompletionRequest(
            model=f"groq/{MODEL}",
            messages=[{"role": "user", "content": "count: one two three"}],
            max_tokens=32,
            stream=True,
        )
        stream = await provider.chat_completion(req)
        async for chunk in stream:
            if chunk.content_delta:
                deltas.append(chunk.content_delta)
    finally:
        await provider.aclose()

    assert deltas, "expected at least one content delta from Groq"
    assert any(d.strip() for d in deltas)
