"""Streaming cache replay tests (Phase 28).

The cache used to bypass streaming requests entirely — every
``stream=true`` call hit the upstream regardless of cache state. Phase
28 closes that gap by:

1. Capturing inter-chunk timing in ``_sse_openai_chunks`` and writing
   the assembled response (plus ``pronaos.stream_chunks`` metadata) to
   the cache on clean completion.
2. On cache hit with ``body.stream=true``, ``_stream_cached_response``
   replays the stored chunks as SSE at the original cadence.

These tests exercise both halves directly. The full end-to-end path
(through the Anthropic SSE parser) is covered by the existing
``test_streaming_coverage.py`` audit tests; here we keep the focus on
the cache write/replay protocol so the assertions stay clean.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest

from pronaos.api.v1.chat import _sse_openai_chunks, _stream_cached_response
from pronaos.audit.logger import AuditLogger
from pronaos.auth.api_keys import Principal
from pronaos.cache.exact import RedisExactCache
from pronaos.core.quota import QuotaTracker
from pronaos.db.models import Base
from pronaos.db.session import create_engine, create_sessionmaker
from pronaos.guardrails.base import NullGuardrailEngine
from pronaos.providers.base import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    Provider,
)

# --------------------------------------------------------------------------- #
# Helpers — a controllable streaming provider + a no-op chat dependency stack #
# --------------------------------------------------------------------------- #


class _ScriptedStreamProvider(Provider):
    """Yields a scripted list of (text, finish_reason) pairs as chunks."""

    name = "scripted"

    def __init__(self, parts: list[str], *, between_ms: float = 5.0) -> None:
        self._parts = parts
        self._sleep = between_ms / 1000.0

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        sleep_s = self._sleep
        parts = self._parts

        async def _iter() -> AsyncIterator[ChatCompletionChunk]:
            for i, part in enumerate(parts):
                if i > 0 and sleep_s > 0:
                    await asyncio.sleep(sleep_s)
                yield ChatCompletionChunk(
                    content_delta=part,
                    finish_reason=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                )
            # Tail chunk carries usage + finish_reason.
            yield ChatCompletionChunk(
                content_delta="",
                finish_reason="stop",
                prompt_tokens=4,
                completion_tokens=len(parts),
            )

        return _iter()

    def cost_cents(self, p: int, c: int, m: str) -> int:
        return 0


async def _drive_sse(
    *,
    provider: _ScriptedStreamProvider,
    model: str,
    principal: Principal,
    cache: RedisExactCache | None,
    cache_key_payload: dict[str, Any] | None,
    quota: QuotaTracker,
    session: Any,
) -> list[str]:
    """Run ``_sse_openai_chunks`` against a scripted provider and collect SSE
    output. Mirrors what the FastAPI streaming response would do; lets the
    test exercise the capture + cache-write path without ASGI middleware."""
    req = ChatCompletionRequest(
        model=model,
        messages=[{"role": "user", "content": "name a city"}],
    )
    stream = await provider.chat_completion(req)
    out: list[str] = []
    async for chunk in _sse_openai_chunks(
        stream,
        provider=provider,
        model=model,
        principal=principal,
        quota=quota,
        session=session,
        provider_call_start=time.monotonic(),
        guardrails=NullGuardrailEngine(),
        audit=AuditLogger(),
        request_body_for_audit={"messages": [{"role": "user", "content": "name a city"}]},
        cache=cache,
        cache_key_payload=cache_key_payload,
    ):
        out.append(chunk)
    return out


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id="t-cache-test",
        tenant_name="cache-test-tenant",
        team_id="team-cache-test",
        team_name="eng",
        key_id="key-cache-test",
        key_prefix="cache",
        scopes=frozenset({"chat:write"}),
    )


@pytest.fixture
async def quota_session() -> AsyncIterator[tuple[QuotaTracker, Any]]:
    """In-memory SQLite session + quota tracker for the post-stream
    bookkeeping helper. We don't assert on the DB shape here — these
    tests are about cache replay, not audit/usage rows."""
    import os

    os.environ["PRONAOS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    from pronaos.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm() as session:
        yield QuotaTracker(), session
    await engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
async def cache() -> AsyncIterator[RedisExactCache]:
    c = RedisExactCache(fakeredis.aioredis.FakeRedis())
    yield c
    await c.aclose()


# --------------------------------------------------------------------------- #
# Capture: streaming generator records chunk metadata into the cache          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_streaming_writes_chunk_timing_to_cache(
    principal: Principal,
    cache: RedisExactCache,
    quota_session: tuple[QuotaTracker, Any],
) -> None:
    quota, session = quota_session
    provider = _ScriptedStreamProvider(["Hello ", "world", "!"], between_ms=2.0)
    key_payload = {"messages": [{"role": "user", "content": "name a city"}]}

    sse_chunks = await _drive_sse(
        provider=provider,
        model="scripted/test",
        principal=principal,
        cache=cache,
        cache_key_payload=key_payload,
        quota=quota,
        session=session,
    )

    # Generator must have emitted at least the role-marker + one content
    # delta + the closing DONE marker.
    assert any("[DONE]" in c for c in sse_chunks)
    raw = "".join(sse_chunks)
    assert "Hello" in raw
    assert "world" in raw

    # Now read the cache entry directly and verify stream_chunks landed.
    lookup = await cache.get(
        tenant_id=principal.tenant_id, model="scripted/test", key_payload=key_payload
    )
    assert lookup.hit, "streaming completion didn't persist into cache"
    pronaos_meta = (lookup.response or {}).get("pronaos") or {}
    chunks = pronaos_meta.get("stream_chunks")
    assert isinstance(chunks, list)
    assert len(chunks) == 3
    assert chunks[0]["text"] == "Hello "
    assert chunks[1]["text"] == "world"
    assert chunks[2]["text"] == "!"
    # Inter-chunk delays should be positive after the first chunk
    # (the first chunk's delay represents time-to-first-token from
    # provider_call_start; chunks 2 + 3 each waited ~2 ms).
    for entry in chunks:
        assert isinstance(entry["delay_ms"], int | float)
        assert entry["delay_ms"] >= 0


@pytest.mark.asyncio
async def test_streaming_skips_cache_write_when_disabled(
    principal: Principal,
    cache: RedisExactCache,
    quota_session: tuple[QuotaTracker, Any],
) -> None:
    """When cache=None or cache_key_payload=None, the generator must NOT
    write to the cache (mirrors the cache-ineligible path)."""
    quota, session = quota_session
    provider = _ScriptedStreamProvider(["No cache for me"])
    await _drive_sse(
        provider=provider,
        model="scripted/test",
        principal=principal,
        cache=None,  # disabled
        cache_key_payload=None,
        quota=quota,
        session=session,
    )

    # Nothing should be in the cache.
    lookup = await cache.get(
        tenant_id=principal.tenant_id,
        model="scripted/test",
        key_payload={"messages": [{"role": "user", "content": "name a city"}]},
    )
    assert not lookup.hit


# --------------------------------------------------------------------------- #
# Replay: _stream_cached_response emits SSE matching the cached cadence       #
# --------------------------------------------------------------------------- #


async def _collect_sse(resp: Any) -> list[dict[str, Any]]:
    """Drain a StreamingResponse and parse each ``data: {...}`` event."""
    events: list[dict[str, Any]] = []
    body = b""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    for line in body.decode().split("\n\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


@pytest.mark.asyncio
async def test_stream_cached_response_replays_chunks_in_order() -> None:
    """Replay generator walks the stored chunks and emits them as SSE."""
    stored = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "scripted/test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        "pronaos": {
            "provider": "scripted",
            "cost_hcents": 0,
            "stream_chunks": [
                {"text": "Hello ", "delay_ms": 0.5},
                {"text": "world", "delay_ms": 1.0},
                {"text": "!", "delay_ms": 1.0},
            ],
        },
    }
    resp = _stream_cached_response(stored, model="scripted/test")
    events = await _collect_sse(resp)

    # First event: role marker. Then 3 content deltas in order. Then
    # closing chunk with finish_reason.
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    contents = [e["choices"][0]["delta"].get("content") for e in events[1:-1]]
    assert contents == ["Hello ", "world", "!"]
    final = events[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["pronaos"]["cache"] == "hit"
    # The replay also stamps a header on the StreamingResponse.
    assert resp.headers["X-Pronaos-Cache"] == "hit:replay"


@pytest.mark.asyncio
async def test_stream_cached_response_falls_back_to_single_chunk() -> None:
    """When the cached entry has no ``stream_chunks`` (came from a
    non-streaming call), the replay emits the assembled content as a
    single chunk so the client still gets a valid SSE response."""
    stored = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "scripted/test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Madrid is in Spain."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        "pronaos": {"provider": "scripted", "cost_hcents": 0},
    }
    resp = _stream_cached_response(stored, model="scripted/test")
    events = await _collect_sse(resp)

    contents = [
        e["choices"][0]["delta"].get("content")
        for e in events[1:-1]
        if "content" in e["choices"][0]["delta"]
    ]
    assert contents == ["Madrid is in Spain."]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_cached_response_handles_empty_content() -> None:
    """Edge case: cached entry has neither stream_chunks nor non-empty
    content. Replay still emits a valid role + closing chunk."""
    stored = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "scripted/test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 0, "total_tokens": 4},
        "pronaos": {"provider": "scripted", "cost_hcents": 0},
    }
    resp = _stream_cached_response(stored, model="scripted/test")
    events = await _collect_sse(resp)

    # Just opener + closer; no content deltas.
    assert len(events) == 2
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert events[1]["choices"][0]["finish_reason"] == "stop"


# --------------------------------------------------------------------------- #
# Roundtrip: capture-then-replay using ONLY the captured chunks               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capture_then_replay_preserves_content_exactly(
    principal: Principal,
    cache: RedisExactCache,
    quota_session: tuple[QuotaTracker, Any],
) -> None:
    """Drive a streaming call, read the cached entry, replay it through
    _stream_cached_response, and verify the replayed deltas concatenate
    back to the original content."""
    quota, session = quota_session
    parts = ["Lyon ", "is ", "in ", "France."]
    provider = _ScriptedStreamProvider(parts, between_ms=1.0)
    key_payload = {"messages": [{"role": "user", "content": "another french city"}]}

    await _drive_sse(
        provider=provider,
        model="scripted/test",
        principal=principal,
        cache=cache,
        cache_key_payload=key_payload,
        quota=quota,
        session=session,
    )

    lookup = await cache.get(
        tenant_id=principal.tenant_id, model="scripted/test", key_payload=key_payload
    )
    assert lookup.hit
    assert lookup.response is not None

    # Replay the cached entry.
    resp = _stream_cached_response(lookup.response, model="scripted/test")
    events = await _collect_sse(resp)
    replayed_contents = [
        e["choices"][0]["delta"].get("content")
        for e in events
        if "content" in e["choices"][0]["delta"]
    ]
    assert replayed_contents == parts
    assert "".join(replayed_contents) == "".join(parts)


# --------------------------------------------------------------------------- #
# Tool-turn safety: cache write skipped when tool_calls present               #
# --------------------------------------------------------------------------- #


class _ToolCallStreamProvider(Provider):
    """Yields a stream that ends with accumulated tool_calls."""

    name = "scripted-tool"

    async def chat_completion(
        self, req: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        async def _iter() -> AsyncIterator[ChatCompletionChunk]:
            # No text content — model decided to call a tool.
            yield ChatCompletionChunk(
                content_delta="",
                finish_reason=None,
                tool_calls=None,
            )
            # Tail with assembled tool_calls.
            yield ChatCompletionChunk(
                content_delta="",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
                prompt_tokens=4,
                completion_tokens=2,
            )

        return _iter()

    def cost_cents(self, p: int, c: int, m: str) -> int:
        return 0


@pytest.mark.asyncio
async def test_streaming_with_tool_calls_does_not_write_cache(
    principal: Principal,
    cache: RedisExactCache,
    quota_session: tuple[QuotaTracker, Any],
) -> None:
    """When the assembled response carries tool_calls, the cache write
    must be skipped — a future agent turn needs to re-call the model
    with fresh tool results, not get a stale cached tool_calls list."""
    quota, session = quota_session
    provider = _ToolCallStreamProvider()
    key_payload = {"messages": [{"role": "user", "content": "search please"}]}

    req = ChatCompletionRequest(
        model="scripted-tool/test", messages=[{"role": "user", "content": "search"}]
    )
    stream = await provider.chat_completion(req)
    async for _ in _sse_openai_chunks(
        stream,
        provider=provider,
        model="scripted-tool/test",
        principal=principal,
        quota=quota,
        session=session,
        provider_call_start=time.monotonic(),
        guardrails=NullGuardrailEngine(),
        audit=AuditLogger(),
        request_body_for_audit={"messages": [{"role": "user", "content": "search"}]},
        cache=cache,
        cache_key_payload=key_payload,
    ):
        pass

    lookup = await cache.get(
        tenant_id=principal.tenant_id,
        model="scripted-tool/test",
        key_payload=key_payload,
    )
    assert not lookup.hit, "tool-turn stream wrote to cache — would corrupt agent loop"
