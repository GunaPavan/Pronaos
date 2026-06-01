"""Tool-calling coverage across the Anthropic + OpenAI-compat adapters.

Two threads worth testing:

1. **Translation correctness** — the helper functions in the Anthropic
   adapter convert OpenAI tool shapes to Anthropic and Anthropic
   tool_use blocks back to OpenAI tool_calls. These are pure functions;
   we test them directly.

2. **End-to-end wire correctness** — a tool-using request through the
   chat endpoint produces an OpenAI-shape response with ``tool_calls``
   regardless of whether the upstream is Anthropic (translated) or
   OpenAI-compat (pass-through). Mocked with respx so the test is
   hermetic and doesn't need real API credentials.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from pronaos.providers.anthropic import (
    ANTHROPIC_API_URL,
    _translate_messages_to_anthropic,
    _translate_tool_choice_to_anthropic,
    _translate_tool_to_anthropic,
    _translate_tool_uses_to_openai,
)

# --------------------------------------------------------------------------- #
# Translation: OpenAI tool definition → Anthropic                              #
# --------------------------------------------------------------------------- #


def test_tool_definition_translates_to_anthropic_shape() -> None:
    """OpenAI ``{"type":"function","function":{name,description,parameters}}``
    must become Anthropic ``{name, description, input_schema}``. This is
    the core schema bridge the gateway provides."""
    oai_tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    out = _translate_tool_to_anthropic(oai_tool)
    assert out["name"] == "get_weather"
    assert out["description"] == "Get the current weather for a city"
    assert out["input_schema"]["properties"]["city"]["type"] == "string"
    assert "function" not in out  # OpenAI nesting gone
    assert "type" not in out  # OpenAI type marker gone


def test_already_anthropic_shape_passes_through_unchanged() -> None:
    """Advanced callers can supply native Anthropic tool shapes; the
    translator must not double-translate them. Defensive — detected
    by presence of top-level ``input_schema``."""
    native = {
        "name": "search",
        "description": "search the web",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    out = _translate_tool_to_anthropic(native)
    assert out is native  # identity — unchanged


# --------------------------------------------------------------------------- #
# Translation: OpenAI tool_choice → Anthropic                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "openai_choice,expected",
    [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ("none", {"type": "auto"}),  # closest equivalent; doc'd in helper
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "tool", "name": "get_weather"},
        ),
    ],
)
def test_tool_choice_translation(
    openai_choice: str | dict[str, Any], expected: dict[str, Any]
) -> None:
    """Each canonical OpenAI tool_choice value maps to the documented
    Anthropic equivalent. Unknown strings fall back to ``auto``."""
    assert _translate_tool_choice_to_anthropic(openai_choice) == expected


# --------------------------------------------------------------------------- #
# Translation: Anthropic tool_use blocks → OpenAI tool_calls                   #
# --------------------------------------------------------------------------- #


def test_tool_use_block_translates_to_openai_tool_call() -> None:
    """An Anthropic content block ``{type:"tool_use", id, name, input}``
    must produce an OpenAI tool_call with ``arguments`` as a JSON-
    encoded STRING (matching OpenAI exactly — not a parsed object)."""
    blocks = [
        {"type": "text", "text": "I'll check the weather."},
        {
            "type": "tool_use",
            "id": "toolu_01abc",
            "name": "get_weather",
            "input": {"city": "Paris"},
        },
    ]
    out = _translate_tool_uses_to_openai(blocks)
    assert len(out) == 1
    call = out[0]
    assert call["id"] == "toolu_01abc"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    # JSON-encoded string (no whitespace) — OpenAI wire format.
    assert call["function"]["arguments"] == '{"city":"Paris"}'
    # And that string round-trips.
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_text_only_response_produces_no_tool_calls() -> None:
    """A response with only text blocks (no tool_use) must produce an
    empty tool_calls list. The caller (provider chunk constructor) then
    sets ``tool_calls=None`` so OpenAI clients don't see an empty array
    where they expect omission."""
    blocks = [{"type": "text", "text": "Paris is the capital."}]
    assert _translate_tool_uses_to_openai(blocks) == []


def test_multiple_tool_uses_in_one_response() -> None:
    """Anthropic can emit multiple tool_use blocks in one response when
    the model wants parallel tool calls. The translator must produce
    one OpenAI tool_call per block, preserving order."""
    blocks = [
        {"type": "tool_use", "id": "a", "name": "f1", "input": {"x": 1}},
        {"type": "text", "text": "and also..."},
        {"type": "tool_use", "id": "b", "name": "f2", "input": {"y": "z"}},
    ]
    out = _translate_tool_uses_to_openai(blocks)
    assert [c["id"] for c in out] == ["a", "b"]
    assert [c["function"]["name"] for c in out] == ["f1", "f2"]


# --------------------------------------------------------------------------- #
# End-to-end: Anthropic adapter wire shape                                    #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_adapter_emits_openai_tool_calls() -> None:
    """A real-shaped Anthropic response with a tool_use block must produce
    a ChatCompletionChunk whose ``tool_calls`` field is OpenAI-shaped.
    This is the integration the gateway promises clients: regardless of
    upstream, the chunk you get back follows the OpenAI schema."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
                "model": "claude-opus-4-7",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 17, "output_tokens": 9},
            },
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "what's the weather in paris"}],
                stream=False,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "weather lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
                tool_choice="auto",
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.tool_calls is not None
    assert len(chunk.tool_calls) == 1
    call = chunk.tool_calls[0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_adapter_sends_translated_tools_body() -> None:
    """Inspect what the adapter actually sent on the wire — the tools
    field in the outgoing JSON must be Anthropic-shaped, not the
    OpenAI shape the caller supplied."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "search",
                            "description": "search",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "search"}},
            )
        )
        async for _ in stream:
            pass
    finally:
        await provider.aclose()

    sent = json.loads(route.calls[0].request.content)
    # Anthropic-shaped tool: name + description + input_schema at the top.
    assert sent["tools"][0]["name"] == "search"
    assert "input_schema" in sent["tools"][0]
    assert "function" not in sent["tools"][0]
    # tool_choice forced to a specific tool → Anthropic's "tool" type.
    assert sent["tool_choice"] == {"type": "tool", "name": "search"}


# --------------------------------------------------------------------------- #
# End-to-end: OpenAI-compat adapter pass-through                              #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_openai_compat_adapter_passes_tools_through() -> None:
    """The OpenAI-compat adapter must pass OpenAI tool shapes verbatim
    upstream — Groq, OpenAI, Together, Fireworks etc. all expect them
    in that exact shape. The body sent to the wire must contain the
    OpenAI ``type: function`` nesting, NOT translated."""
    from pronaos.providers.base import ChatCompletionRequest
    from pronaos.providers.openai_compat import OpenAICompatibleProvider

    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    route = respx.post(groq_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"q":"hi"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            },
        )
    )

    provider = OpenAICompatibleProvider(
        provider_key="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk_test",
        pricing={},  # not exercised in this test
    )
    try:
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            }
        ]
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="groq/llama-3.1-8b-instant",
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                tools=oai_tools,
                tool_choice="auto",
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # 1. Wire body: tools sent verbatim, including ``type: function`` nesting.
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"] == oai_tools  # verbatim
    assert sent["tool_choice"] == "auto"

    # 2. Response chunk surfaces OpenAI tool_calls intact.
    assert chunks[0].tool_calls is not None
    assert chunks[0].tool_calls[0]["function"]["name"] == "search"


# --------------------------------------------------------------------------- #
# Streaming tools: SSE accumulator + final chunk shape                        #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_openai_compat_streaming_tool_calls_accumulator() -> None:
    """OpenAI streams tool_calls as a sequence of ``delta.tool_calls``
    fragments keyed by ``index``: the first fragment carries ``id`` +
    ``function.name`` (and usually an empty ``arguments`` string),
    each subsequent fragment appends a chunk of ``function.arguments``.
    The adapter must accumulate these by index and emit the assembled
    tool_call list on a single tail ``ChatCompletionChunk``.

    Multi-tool: this test exercises TWO tool calls at indices 0 and 1,
    interleaved across the stream, to prove the dict-by-index design
    survives parallel tool calls."""
    from pronaos.providers.base import ChatCompletionRequest
    from pronaos.providers.openai_compat import OpenAICompatibleProvider

    # SSE chunks following the OpenAI streaming-tools wire format.
    # Each "data: {...}" line is one server-sent chunk.
    sse_lines = [
        # Role marker (no tool yet).
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        # First tool: id + name (empty args).
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
        # First tool: args streamed in two fragments.
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Paris\\"}"}}]},"finish_reason":null}]}',
        # Second tool starts at index 1: id + name.
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"call_b","type":"function","function":{"name":"get_time","arguments":""}}]},"finish_reason":null}]}',
        # Second tool: args in one fragment.
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\\"tz\\":\\"UTC\\"}"}}]},"finish_reason":null}]}',
        # Final chunk: finish_reason=tool_calls, usage attached.
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":12,"completion_tokens":24}}',
        "data: [DONE]",
    ]
    sse_body = ("\n\n".join(sse_lines) + "\n\n").encode()

    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    respx.post(groq_url).mock(
        return_value=httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    provider = OpenAICompatibleProvider(
        provider_key="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk_test",
        pricing={},
    )
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="groq/llama-3.1-8b-instant",
                messages=[{"role": "user", "content": "weather + time?"}],
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "weather",
                            "parameters": {"type": "object"},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "description": "time",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
                tool_choice="auto",
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # The final chunk carries the assembled tool_calls list. There may be
    # leading content/tool chunks with empty deltas — we care about the tail.
    tail = chunks[-1]
    assert tail.tool_calls is not None, "tail chunk must surface assembled tool_calls"
    assert tail.finish_reason == "tool_calls"
    assert tail.prompt_tokens == 12
    assert tail.completion_tokens == 24

    # Two tools, in index order.
    assert len(tail.tool_calls) == 2
    a, b = tail.tool_calls[0], tail.tool_calls[1]

    assert a["id"] == "call_a"
    assert a["type"] == "function"
    assert a["function"]["name"] == "get_weather"
    # Arguments were streamed across two fragments; must be concatenated
    # into a valid JSON string.
    assert a["function"]["arguments"] == '{"city":"Paris"}'
    assert json.loads(a["function"]["arguments"]) == {"city": "Paris"}

    assert b["id"] == "call_b"
    assert b["function"]["name"] == "get_time"
    assert json.loads(b["function"]["arguments"]) == {"tz": "UTC"}


@respx.mock
@pytest.mark.asyncio
async def test_streaming_tool_call_emits_sse_event_via_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a real streaming response carrying tool_call fragments
    must surface in the SSE stream the chat handler sends to the client.
    The handler emits one ``delta.tool_calls`` event per assembled tool
    call (OpenAI streaming-tools shape), with the final ``finish_reason``
    set to ``tool_calls``. This is the wire contract clients consume."""
    from pronaos.audit.logger import AuditLogger
    from pronaos.auth.api_keys import generate_api_key, hash_key
    from pronaos.config import get_settings
    from pronaos.core.quota import QuotaTracker
    from pronaos.core.ratelimit import InMemoryRateLimiter
    from pronaos.core.router import Router
    from pronaos.db.models import ApiKey, Base, Team, Tenant
    from pronaos.db.session import create_engine, create_sessionmaker
    from pronaos.main import create_app
    from pronaos.providers.registry import ProviderRegistry

    # Force in-memory SQLite + register a fake Groq key so the registry
    # constructs the OpenAI-compat adapter for ``groq/*`` models. The
    # adapter's wire calls go to respx, never to api.groq.com.
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    full, prefix = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="acme-tool-stream")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.flush()
        key = ApiKey(
            team_id=team.id,
            prefix=prefix,
            key_hash=hash_key(full),
            scopes="chat:write",
            label="tool-stream",
        )
        session.add(key)
        await session.commit()

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()
    app.state.audit_logger = AuditLogger()

    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_xyz","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"Paris\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":7,"completion_tokens":13}}',
        "data: [DONE]",
    ]
    sse_body = ("\n\n".join(sse_lines) + "\n\n").encode()
    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    respx.post(groq_url).mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(sse_body),
            headers={"content-type": "text/event-stream"},
        )
    )

    transport = httpx.ASGITransport(app=app)
    received_chunks: list[str] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            async with c.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {full}"},
                json={
                    "model": "groq/llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": "weather in paris"}],
                    "stream": True,
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "weather",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    "tool_choice": "auto",
                },
            ) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    received_chunks.append(chunk)
    finally:
        await registry.aclose()
        await engine.dispose()
        get_settings.cache_clear()

    body = "".join(received_chunks)
    # Parse SSE events from the wire body.
    events: list[dict[str, Any]] = []
    for raw in body.split("\n\n"):
        line = raw.strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))

    # The handler must have emitted at least one SSE event with
    # delta.tool_calls carrying the assembled tool call (id + name +
    # full arguments — not the per-fragment slice).
    tool_events = [
        e
        for e in events
        if e.get("choices") and (e["choices"][0].get("delta") or {}).get("tool_calls")
    ]
    assert tool_events, "expected at least one SSE event with delta.tool_calls"
    tc = tool_events[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["id"] == "call_xyz"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}

    # The terminal event must set finish_reason=tool_calls so OpenAI
    # clients route the response to their tool-call handler.
    terminal = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert terminal, "expected a terminal event with finish_reason set"
    assert terminal[-1]["choices"][0]["finish_reason"] == "tool_calls"


# --------------------------------------------------------------------------- #
# Tool-result loop: OpenAI tool messages → Anthropic tool_result content      #
# --------------------------------------------------------------------------- #


def test_tool_result_message_translates_to_anthropic_user_block() -> None:
    """A ``{"role":"tool", "tool_call_id":X, "content":Y}`` OpenAI message
    must become an Anthropic ``user`` message with a single
    ``tool_result`` content block. This is the leg that lets clients
    return tool results and continue the agent conversation."""
    out = _translate_messages_to_anthropic(
        [
            {"role": "user", "content": "weather in paris?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Paris"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"temp_c":12,"condition":"cloudy"}',
            },
        ]
    )
    # Three Anthropic messages: user prompt, assistant tool_use, user tool_result.
    assert len(out) == 3
    user_prompt, assistant, tool_result_msg = out

    # 1. User prompt passes through as string content.
    assert user_prompt == {"role": "user", "content": "weather in paris?"}

    # 2. Assistant echo gets translated into a tool_use content block.
    assert assistant["role"] == "assistant"
    tu = assistant["content"][0]
    assert tu["type"] == "tool_use"
    assert tu["id"] == "call_1"
    assert tu["name"] == "get_weather"
    assert tu["input"] == {"city": "Paris"}  # parsed object, not JSON string

    # 3. Tool result becomes a USER message with a tool_result block.
    assert tool_result_msg["role"] == "user"
    blk = tool_result_msg["content"][0]
    assert blk["type"] == "tool_result"
    assert blk["tool_use_id"] == "call_1"
    assert blk["content"] == '{"temp_c":12,"condition":"cloudy"}'


def test_multiple_tool_results_coalesce_into_one_user_message() -> None:
    """Anthropic requires that ALL tool_result blocks for one assistant
    turn arrive in a single user message. OpenAI splits them across
    multiple ``role:"tool"`` messages. The translator must coalesce
    consecutive tool messages into one Anthropic user message with
    multiple tool_result blocks, preserving order."""
    out = _translate_messages_to_anthropic(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "f1", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "f2", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "result-a"},
            {"role": "tool", "tool_call_id": "b", "content": "result-b"},
        ]
    )
    # 1 assistant message + 1 user message holding BOTH tool_results.
    assert len(out) == 2
    assert out[0]["role"] == "assistant"
    user = out[1]
    assert user["role"] == "user"
    assert len(user["content"]) == 2
    assert [b["tool_use_id"] for b in user["content"]] == ["a", "b"]
    assert [b["content"] for b in user["content"]] == ["result-a", "result-b"]


def test_assistant_text_and_tool_calls_become_mixed_anthropic_blocks() -> None:
    """An assistant message with BOTH textual narration and tool_calls
    must become Anthropic content with a text block followed by tool_use
    blocks. Anthropic's content list preserves order; the model uses the
    interleaving as part of its reasoning trace."""
    out = _translate_messages_to_anthropic(
        [
            {
                "role": "assistant",
                "content": "Let me check the weather.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Tokyo"}',
                        },
                    }
                ],
            }
        ]
    )
    assert len(out) == 1
    blocks = out[0]["content"]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "Let me check the weather."}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"] == {"city": "Tokyo"}


def test_invalid_json_arguments_dont_crash_translator() -> None:
    """Defensive: providers occasionally emit malformed JSON in
    ``function.arguments`` (truncated streams, model errors). The
    translator must not crash — wrap the raw string in an ``_raw``
    field so the upstream model at least sees something."""
    out = _translate_messages_to_anthropic(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "broken",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{not valid json"},
                    }
                ],
            }
        ]
    )
    tu = out[0]["content"][0]
    assert tu["type"] == "tool_use"
    assert tu["input"] == {"_raw": "{not valid json"}


def test_plain_user_messages_pass_through_unchanged() -> None:
    """The translator must not touch messages that don't involve tools.
    Plain user / assistant text turns should come through as Anthropic
    string-content messages — the simplest, most common case."""
    out = _translate_messages_to_anthropic(
        [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
    )
    assert out == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_adapter_sends_translated_tool_result_body() -> None:
    """End-to-end: an OpenAI-shape request including a tool message in
    its history must reach Anthropic's wire with the proper
    ``user/tool_result`` shape, the ``assistant/tool_use`` echo, and
    the original user text — in the correct order."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_02",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "It is 12°C and cloudy."}],
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[
                    {"role": "user", "content": "what's the weather in paris"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "tu_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Paris"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "tu_1",
                        "content": '{"temp_c":12,"condition":"cloudy"}',
                    },
                ],
                stream=False,
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # 1. The chunk surfaces the final assistant text (no tool_use this time).
    assert chunks[0].content_delta == "It is 12°C and cloudy."

    # 2. The wire body has the canonical Anthropic shape.
    sent = json.loads(route.calls[0].request.content)
    msgs = sent["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "what's the weather in paris"}
    # Assistant echo: tool_use block with parsed input.
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[1]["content"][0]["id"] == "tu_1"
    assert msgs[1]["content"][0]["input"] == {"city": "Paris"}
    # Tool result: user message with tool_result block.
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": '{"temp_c":12,"condition":"cloudy"}',
    }


# --------------------------------------------------------------------------- #
# Tool-result loop: schema acceptance (chat handler request model)            #
# --------------------------------------------------------------------------- #


def test_chat_message_schema_accepts_tool_role() -> None:
    """The relaxed ChatMessage schema must accept the three shapes the
    agent loop produces, in addition to plain user/system messages."""
    from pronaos.api.v1.chat import ChatMessage

    # 1. role=tool with tool_call_id + content
    tool_msg = ChatMessage.model_validate(
        {"role": "tool", "tool_call_id": "x", "content": "result"}
    )
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "x"

    # 2. role=assistant with content=null + tool_calls
    asst_msg = ChatMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
        }
    )
    assert asst_msg.role == "assistant"
    assert asst_msg.content is None
    assert asst_msg.tool_calls is not None and len(asst_msg.tool_calls) == 1

    # 3. Plain user message (regression check — old shape still works)
    user_msg = ChatMessage.model_validate({"role": "user", "content": "hi"})
    assert user_msg.role == "user"
    assert user_msg.content == "hi"


def test_dump_message_preserves_null_content_for_assistant_echo() -> None:
    """When the client echoes the previous assistant turn back into a new
    request, the message has ``content: null`` and ``tool_calls`` set.
    Our serializer strips ``None`` from optional fields generally — but
    MUST preserve ``content: null`` here because OpenAI's spec mandates
    the content key be present (and some strict providers reject the
    message if it's missing entirely)."""
    from pronaos.api.v1.chat import ChatMessage, _dump_message

    asst = ChatMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
        }
    )
    dumped = _dump_message(asst)
    assert "content" in dumped
    assert dumped["content"] is None
    assert dumped["tool_calls"][0]["id"] == "a"
    # Optional fields that ARE None get stripped.
    assert "tool_call_id" not in dumped
    assert "name" not in dumped

    # Plain user message — content present, all None extras stripped.
    user = ChatMessage.model_validate({"role": "user", "content": "hi"})
    dumped = _dump_message(user)
    assert dumped == {"role": "user", "content": "hi"}


# --------------------------------------------------------------------------- #
# Tool-result loop: cache must NOT serve agent-loop continuations             #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_tool_turn_request_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent's turn-2 request — same user prompt as turn 1, but with a
    tool-result message appended — must NOT serve a cached response from
    turn 1. The L2 semantic cache embeds only the user prompt, so without
    an explicit bypass it would happily return turn 1's response (which
    is a different finish_reason, different content) — a quiet
    correctness bug that breaks every agent loop.

    Regression for: live verify caught this during the round-trip
    against Groq when ``hit:semantic:1.0000`` came back instead of the
    expected fresh provider call.
    """
    import fakeredis.aioredis

    from pronaos.audit.logger import AuditLogger
    from pronaos.auth.api_keys import generate_api_key, hash_key
    from pronaos.cache.exact import RedisExactCache
    from pronaos.config import get_settings
    from pronaos.core.quota import QuotaTracker
    from pronaos.core.ratelimit import InMemoryRateLimiter
    from pronaos.core.router import Router
    from pronaos.db.models import ApiKey, Base, Team, Tenant
    from pronaos.db.session import create_engine, create_sessionmaker
    from pronaos.main import create_app
    from pronaos.providers.registry import ProviderRegistry

    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    full, prefix = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="acme-tool-cache")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng")
        session.add(team)
        await session.flush()
        session.add(
            ApiKey(
                team_id=team.id,
                prefix=prefix,
                key_hash=hash_key(full),
                scopes="chat:write",
                label="cache-bypass-test",
            )
        )
        await session.commit()

    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()
    app.state.cache = RedisExactCache(fakeredis.aioredis.FakeRedis())
    app.state.audit_logger = AuditLogger()

    # Mock Groq. If the test ever hits cache instead of the provider,
    # the route's call_count stays at zero and the assertion fails.
    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    route = respx.post(groq_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-fresh",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Mumbai is humid at 33°C.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 7},
            },
        )
    )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # Turn-2-style request: same user prompt PLUS a prior
            # assistant tool_call + a tool-result message.
            resp = await c.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {full}"},
                json={
                    "model": "groq/llama-3.1-8b-instant",
                    "messages": [
                        {"role": "user", "content": "weather in mumbai?"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city":"Mumbai"}',
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "t1",
                            "content": '{"temp_c":33}',
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "weather",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                },
            )
    finally:
        await registry.aclose()
        await app.state.cache.aclose()
        await engine.dispose()
        get_settings.cache_clear()

    assert resp.status_code == 200
    # Cache header proves the path bypassed the cache layer entirely
    # rather than missing the hash but writing.
    assert resp.headers.get("x-pronaos-cache") == "skip"
    # And the provider WAS called (would be 0 if served from cache).
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Anthropic streaming tool_use (Phase 16)                                     #
# --------------------------------------------------------------------------- #
#
# Anthropic's streaming SSE encodes tool calls differently from OpenAI:
#
#   - ``content_block_start`` carries the tool's id + name, with empty input
#   - One or more ``content_block_delta`` events with delta.type ==
#     ``input_json_delta`` carry partial_json fragments that, when
#     concatenated, parse as the tool's argument object
#   - ``content_block_stop`` closes the block
#
# Multiple parallel tool calls live at different content_block ``index``
# values. The adapter must accumulate per-index and produce one OpenAI
# tool_call per block on the tail chunk — symmetric with the OpenAI-compat
# adapter's streaming-tools accumulator.


def _anthropic_sse_event(payload: dict[str, Any]) -> bytes:
    """Build a single Anthropic SSE event (event: line + data: line + blank).

    We include the ``event:`` line because real Anthropic streams emit it;
    the adapter uses ``data:`` only but tests should mirror the wire format
    to catch any future regex tightening."""
    etype = payload["type"]
    data = json.dumps(payload)
    return f"event: {etype}\ndata: {data}\n\n".encode()


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_streaming_single_tool_use_accumulator() -> None:
    """A streamed tool_use response with fragmented input_json_delta must
    assemble into a single OpenAI tool_call on the tail chunk, with
    ``arguments`` as the concatenated JSON string."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    events = b"".join(
        [
            _anthropic_sse_event(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_01",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-opus-4-7",
                        "stop_reason": None,
                        "usage": {"input_tokens": 17, "output_tokens": 0},
                    },
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_01abc",
                        "name": "get_weather",
                        "input": {},
                    },
                }
            ),
            # Two fragments — together they make `{"city":"Paris"}`.
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
                }
            ),
            _anthropic_sse_event({"type": "content_block_stop", "index": 0}),
            _anthropic_sse_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 9},
                }
            ),
            _anthropic_sse_event({"type": "message_stop"}),
        ]
    )

    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(events),
            headers={"content-type": "text/event-stream"},
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "weather in paris?"}],
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # The tail chunk carries the assembled tool_calls.
    tail = chunks[-1]
    assert tail.tool_calls is not None
    assert tail.finish_reason == "tool_calls"
    assert len(tail.tool_calls) == 1

    tc = tail.tool_calls[0]
    assert tc["id"] == "toolu_01abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    # Arguments came in two SSE fragments — they must be concatenated
    # into a single valid JSON string. This is the core accumulator
    # correctness check.
    assert tc["function"]["arguments"] == '{"city":"Paris"}'
    assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_streaming_parallel_tool_use() -> None:
    """Anthropic emits parallel tool calls as separate content_block
    indices, interleaved across the stream. The accumulator must track
    them independently and emit BOTH in the tail chunk, preserving
    index order."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    events = b"".join(
        [
            _anthropic_sse_event(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_02",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-opus-4-7",
                        "stop_reason": None,
                        "usage": {"input_tokens": 25, "output_tokens": 0},
                    },
                }
            ),
            # First tool at index 0: get_weather(Paris)
            _anthropic_sse_event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "get_weather",
                        "input": {},
                    },
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":"Paris"}'},
                }
            ),
            _anthropic_sse_event({"type": "content_block_stop", "index": 0}),
            # Second tool at index 1: get_time(UTC) — interleaved order
            # in the wire would be the same blocks for a different index.
            _anthropic_sse_event(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_b",
                        "name": "get_time",
                        "input": {},
                    },
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"tz":"UTC"}'},
                }
            ),
            _anthropic_sse_event({"type": "content_block_stop", "index": 1}),
            _anthropic_sse_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 18},
                }
            ),
        ]
    )

    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(events),
            headers={"content-type": "text/event-stream"},
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "weather + time?"}],
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "weather",
                            "parameters": {"type": "object"},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "description": "time",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    tail = chunks[-1]
    assert tail.tool_calls is not None
    assert tail.finish_reason == "tool_calls"
    assert len(tail.tool_calls) == 2

    # Preserved in content_block index order.
    a, b = tail.tool_calls[0], tail.tool_calls[1]
    assert a["id"] == "toolu_a"
    assert a["function"]["name"] == "get_weather"
    assert json.loads(a["function"]["arguments"]) == {"city": "Paris"}

    assert b["id"] == "toolu_b"
    assert b["function"]["name"] == "get_time"
    assert json.loads(b["function"]["arguments"]) == {"tz": "UTC"}


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_streaming_mixed_text_and_tool_use() -> None:
    """A response that emits BOTH narration text and a tool call must
    yield both: text chunks during the stream (one per text_delta) and
    the assembled tool_call on the tail chunk."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    events = b"".join(
        [
            _anthropic_sse_event(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_03",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-opus-4-7",
                        "stop_reason": None,
                        "usage": {"input_tokens": 12, "output_tokens": 0},
                    },
                }
            ),
            # Text block first (index 0)
            _anthropic_sse_event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Checking weather..."},
                }
            ),
            _anthropic_sse_event({"type": "content_block_stop", "index": 0}),
            # Tool_use block at index 1
            _anthropic_sse_event(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_x",
                        "name": "get_weather",
                        "input": {},
                    },
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":"Tokyo"}'},
                }
            ),
            _anthropic_sse_event({"type": "content_block_stop", "index": 1}),
            _anthropic_sse_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 14},
                }
            ),
        ]
    )

    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(events),
            headers={"content-type": "text/event-stream"},
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "weather?"}],
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # One text chunk + one tail chunk with the tool_call.
    text_chunks = [c for c in chunks if c.content_delta]
    assert any("Checking weather" in c.content_delta for c in text_chunks)

    tail = chunks[-1]
    assert tail.tool_calls is not None
    assert len(tail.tool_calls) == 1
    assert tail.tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tail.tool_calls[0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert tail.finish_reason == "tool_calls"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_streaming_no_tools_still_returns_text() -> None:
    """Regression: the streaming path must still work for a plain
    text-only response (the original Phase-2 use case). Adding the
    tool_use accumulator must not have broken the simple case."""
    from pronaos.providers.anthropic import AnthropicProvider
    from pronaos.providers.base import ChatCompletionRequest

    events = b"".join(
        [
            _anthropic_sse_event(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_x",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-opus-4-7",
                        "stop_reason": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Paris."},
                }
            ),
            _anthropic_sse_event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                }
            ),
        ]
    )

    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(events),
            headers={"content-type": "text/event-stream"},
        )
    )

    provider = AnthropicProvider(api_key="test")
    try:
        stream = await provider.chat_completion(
            ChatCompletionRequest(
                model="anthropic/claude-opus-4-7",
                messages=[{"role": "user", "content": "capital of france?"}],
                stream=True,
            )
        )
        chunks = [c async for c in stream]
    finally:
        await provider.aclose()

    # Text comes through, tail has finish_reason="stop", NO tool_calls.
    assert any(c.content_delta == "Paris." for c in chunks)
    tail = chunks[-1]
    assert tail.tool_calls is None
    assert tail.finish_reason == "stop"
