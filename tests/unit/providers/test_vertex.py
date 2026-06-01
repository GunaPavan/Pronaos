"""Unit tests for the GCP Vertex AI provider adapter (Phase 53).

Surfaces under test:

1. **Model-ID parsing**: ``vertex/{publisher}/{model}`` splits cleanly;
   missing-publisher requests fail with a useful error.
2. **Per-family body translators**: Gemini gets the contents/parts/
   generationConfig shape with system → systemInstruction; tools →
   functionDeclarations[]. Anthropic-on-Vertex gets the Messages
   shape with anthropic_version + no model field.
3. **Per-family response parsers**: Gemini's
   candidates[0].content.parts → text + tool_calls; Anthropic's
   content[] → same; both surface usage tokens.
4. **Streaming**: SSE events translated correctly per family,
   including Anthropic-on-Vertex's message_* sequence with tool_use
   args accumulated across input_json_delta frames.
5. **Cost math**: catalog pricing for Gemini and Claude-on-Vertex.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pronaos.providers.base import (
    AuthError,
    ChatCompletionRequest,
    ProviderError,
)
from pronaos.providers.vertex import (
    VertexProvider,
    _build_anthropic_on_vertex_body,
    _build_gemini_body,
    _parse_anthropic_on_vertex_response,
    _parse_gemini_response,
    _split_publisher_model,
    _strip_prefix,
)
from pronaos.providers.vertex_auth import VertexAuth, _ServiceAccountKey

# --------------------------------------------------------------------------- #
# Test fixtures                                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def vertex_auth(rsa_pem: bytes) -> VertexAuth:
    """A VertexAuth whose token exchange is stub-respx'd."""
    sa = _ServiceAccountKey(
        client_email="test-sa@my-project.iam.gserviceaccount.com",
        private_key_pem=rsa_pem,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return VertexAuth(service_account=sa, now_fn=lambda: 1_700_000_000)


def _mock_token_exchange(mock: respx.MockRouter) -> None:
    """Stub the OAuth2 token endpoint with a generic ya29 token."""
    mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "ya29.test-token", "expires_in": 3600},
        )
    )


# --------------------------------------------------------------------------- #
# Model-ID parsing                                                            #
# --------------------------------------------------------------------------- #


class TestModelIdParsing:
    def test_strip_prefix(self) -> None:
        assert _strip_prefix("vertex/google/gemini-1.5-flash") == "google/gemini-1.5-flash"

    def test_split_publisher_model(self) -> None:
        pub, model = _split_publisher_model("google/gemini-1.5-flash")
        assert pub == "google"
        assert model == "gemini-1.5-flash"

    def test_split_handles_anthropic_version_suffix(self) -> None:
        pub, model = _split_publisher_model("anthropic/claude-3-5-haiku@20241022")
        assert pub == "anthropic"
        assert model == "claude-3-5-haiku@20241022"

    def test_missing_publisher_raises(self) -> None:
        with pytest.raises(ProviderError, match="missing publisher prefix"):
            _split_publisher_model("gemini-1.5-flash")


# --------------------------------------------------------------------------- #
# Gemini body translation                                                     #
# --------------------------------------------------------------------------- #


class TestGeminiBody:
    def test_messages_to_contents_with_role_mapping(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/google/gemini-1.5-flash",
            messages=[
                {"role": "user", "content": "say hi"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "thanks"},
            ],
        )
        body = _build_gemini_body(req)
        contents = body["contents"]
        assert len(contents) == 3
        # Vertex says ``model`` for the assistant role.
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"
        assert contents[0]["parts"] == [{"text": "say hi"}]
        assert contents[1]["parts"] == [{"text": "Hi there!"}]

    def test_system_message_hoisted_to_systemInstruction(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/google/gemini-1.5-flash",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hi"},
            ],
        )
        body = _build_gemini_body(req)
        # System lives at the top level, NOT inside contents[].
        assert "systemInstruction" in body
        assert body["systemInstruction"] == {"parts": [{"text": "You are a helpful assistant."}]}
        # contents only has the user turn.
        assert len(body["contents"]) == 1
        assert body["contents"][0]["role"] == "user"

    def test_max_tokens_and_temperature_in_generationConfig(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/google/gemini-1.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=512,
            temperature=0.7,
        )
        body = _build_gemini_body(req)
        gc = body["generationConfig"]
        assert gc["maxOutputTokens"] == 512
        assert gc["temperature"] == 0.7

    def test_tools_to_functionDeclarations(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/google/gemini-1.5-flash",
            messages=[{"role": "user", "content": "weather?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        )
        body = _build_gemini_body(req)
        # Vertex wraps function decls in one tools[0] object.
        assert len(body["tools"]) == 1
        decls = body["tools"][0]["functionDeclarations"]
        assert len(decls) == 1
        assert decls[0]["name"] == "get_weather"
        assert decls[0]["parameters"]["properties"]["city"]["type"] == "string"


# --------------------------------------------------------------------------- #
# Anthropic-on-Vertex body translation                                        #
# --------------------------------------------------------------------------- #


class TestAnthropicOnVertexBody:
    def test_messages_shape_with_anthropic_version_and_no_model(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/anthropic/claude-3-5-haiku@20241022",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=256,
        )
        body = _build_anthropic_on_vertex_body(req)
        assert body["anthropic_version"] == "vertex-2023-10-16"
        assert body["max_tokens"] == 256
        # Bedrock-on-Vertex puts model in URL, NOT in body.
        assert "model" not in body
        # Messages survive as the Anthropic shape (no system in messages).
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_system_hoisted_out_of_messages(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/anthropic/claude-3-5-haiku@20241022",
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
        )
        body = _build_anthropic_on_vertex_body(req)
        assert body["system"] == "be terse"
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_tools_translated_to_anthropic_shape(self) -> None:
        req = ChatCompletionRequest(
            model="vertex/anthropic/claude-3-5-haiku@20241022",
            messages=[{"role": "user", "content": "?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        body = _build_anthropic_on_vertex_body(req)
        assert len(body["tools"]) == 1
        tool = body["tools"][0]
        assert tool["name"] == "get_weather"
        # Anthropic shape: input_schema, NOT parameters.
        assert "input_schema" in tool
        assert "parameters" not in tool


# --------------------------------------------------------------------------- #
# Per-family response parsing                                                 #
# --------------------------------------------------------------------------- #


class TestGeminiResponseParse:
    def test_text_response_with_usage(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Hello there!"}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 8,
                "candidatesTokenCount": 4,
                "totalTokenCount": 12,
            },
        }
        chunk = _parse_gemini_response(data)
        assert chunk.content_delta == "Hello there!"
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 8
        assert chunk.completion_tokens == 4

    def test_max_tokens_finish_maps_to_length(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "partial"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ],
        }
        chunk = _parse_gemini_response(data)
        assert chunk.finish_reason == "length"

    def test_safety_finish_maps_to_content_filter(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": []},
                    "finishReason": "SAFETY",
                }
            ],
        }
        chunk = _parse_gemini_response(data)
        assert chunk.finish_reason == "content_filter"

    def test_function_call_translates_to_openai_tool_call(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_weather",
                                    "args": {"city": "Tokyo"},
                                }
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
        }
        chunk = _parse_gemini_response(data)
        assert chunk.tool_calls is not None
        assert len(chunk.tool_calls) == 1
        tc = chunk.tool_calls[0]
        assert tc["function"]["name"] == "get_weather"
        # ``arguments`` is a JSON string per OpenAI shape.
        assert json.loads(tc["function"]["arguments"]) == {"city": "Tokyo"}


class TestAnthropicOnVertexResponseParse:
    def test_text_response_with_usage(self) -> None:
        data = {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.content_delta == "Hi!"
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 10
        assert chunk.completion_tokens == 3

    def test_tool_use_translates(self) -> None:
        data = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_x",
                    "name": "get_weather",
                    "input": {"city": "Tokyo"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 12},
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.finish_reason == "tool_calls"
        assert chunk.tool_calls is not None
        assert len(chunk.tool_calls) == 1
        tc = chunk.tool_calls[0]
        assert tc["id"] == "toolu_x"
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "Tokyo"}

    def test_thinking_block_surfaces_on_vertex(self) -> None:
        """Phase 56: Anthropic-on-Vertex returns the same
        type:thinking content blocks as direct + Bedrock Anthropic.
        Parser must extract reasoning_content + estimate count."""
        data = {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "GCP-side reasoning.",
                    "signature": "opaque",
                },
                {"type": "text", "text": "Done on Vertex."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 22},
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.content_delta == "Done on Vertex."
        assert chunk.reasoning_content == "GCP-side reasoning."
        # 19 chars → ceil(19/4) = 5 tokens.
        assert chunk.reasoning_tokens == 5

    def test_anthropic_on_vertex_no_thinking_unaffected(self) -> None:
        """Plain Anthropic-on-Vertex response: reasoning fields stay
        at 0/None."""
        data = {
            "content": [{"type": "text", "text": "Hi."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.reasoning_tokens == 0
        assert chunk.reasoning_content is None

    def test_prompt_cache_fields_extracted(self) -> None:
        """Phase 55: Anthropic-on-Vertex returns the same
        cache_creation_input_tokens + cache_read_input_tokens
        usage fields as direct Anthropic. Parser must surface them
        for downstream weighted cost math + FinOps headers."""
        data = {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 800,
                "cache_read_input_tokens": 3200,
            },
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.prompt_tokens == 100
        assert chunk.completion_tokens == 20
        assert chunk.cache_creation_tokens == 800
        assert chunk.cache_read_tokens == 3200

    def test_prompt_cache_fields_default_to_zero_when_absent(self) -> None:
        data = {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        chunk = _parse_anthropic_on_vertex_response(data)
        assert chunk.cache_creation_tokens == 0
        assert chunk.cache_read_tokens == 0


# --------------------------------------------------------------------------- #
# End-to-end non-streaming via respx                                          #
# --------------------------------------------------------------------------- #


class TestEndToEndNonStreaming:
    @pytest.mark.asyncio
    async def test_gemini_chat_completion_url_and_auth(self, vertex_auth: VertexAuth) -> None:
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            route = mock.post(
                re.compile(
                    r".*/projects/my-project/locations/us-central1"
                    r"/publishers/google/models/gemini-1\.5-flash:generateContent$"
                )
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "Hello!"}],
                                },
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 5,
                            "candidatesTokenCount": 1,
                        },
                    },
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/google/gemini-1.5-flash",
                    messages=[{"role": "user", "content": "hi"}],
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        assert route.called
        # Authorization header carried the bearer token from the SA flow.
        forwarded = route.calls.last.request
        assert forwarded.headers["authorization"] == "Bearer ya29.test-token"
        assert forwarded.headers["content-type"] == "application/json"
        # One chunk yielded, carrying the full assistant text.
        assert len(chunks) == 1
        assert chunks[0].content_delta == "Hello!"
        assert chunks[0].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_anthropic_on_vertex_chat_completion_url(self, vertex_auth: VertexAuth) -> None:
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            mock.post(
                re.compile(
                    r".*/publishers/anthropic/models/claude-3-5-haiku@20241022:generateContent$"
                )
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "Hi!"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 7, "output_tokens": 2},
                    },
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/anthropic/claude-3-5-haiku@20241022",
                    messages=[{"role": "user", "content": "hi"}],
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        assert len(chunks) == 1
        assert chunks[0].content_delta == "Hi!"
        assert chunks[0].finish_reason == "stop"
        assert chunks[0].prompt_tokens == 7
        assert chunks[0].completion_tokens == 2

    @pytest.mark.asyncio
    async def test_unknown_publisher_raises(self, vertex_auth: VertexAuth) -> None:
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        try:
            req = ChatCompletionRequest(
                model="vertex/meta/llama-3-on-vertex",
                messages=[{"role": "user", "content": "hi"}],
            )
            with pytest.raises(ProviderError, match="unsupported publisher 'meta'"):
                gen = await prov.chat_completion(req)
                [c async for c in gen]
        finally:
            await prov.aclose()

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self, vertex_auth: VertexAuth) -> None:
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            mock.post(re.compile(r".*:generateContent$")).mock(
                return_value=httpx.Response(
                    403,
                    json={
                        "error": {
                            "code": 403,
                            "message": "Permission denied on resource project.",
                            "status": "PERMISSION_DENIED",
                        }
                    },
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/google/gemini-1.5-flash",
                    messages=[{"role": "user", "content": "hi"}],
                )
                with pytest.raises(AuthError, match="Permission denied"):
                    gen = await prov.chat_completion(req)
                    [c async for c in gen]
            finally:
                await prov.aclose()


# --------------------------------------------------------------------------- #
# End-to-end streaming via respx                                              #
# --------------------------------------------------------------------------- #


class TestEndToEndStreamingGemini:
    @pytest.mark.asyncio
    async def test_streaming_text_chunks_then_finish(self, vertex_auth: VertexAuth) -> None:
        sse_body = (
            'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"text":", "}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"text":"world!"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[]},"finishReason":"STOP"}],'
            '"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":3}}\n\n'
        )
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            route = mock.post(re.compile(r".*:streamGenerateContent.*")).mock(
                return_value=httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/google/gemini-1.5-flash",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        # URL must use ?alt=sse for Gemini streaming
        forwarded = route.calls.last.request
        assert "alt=sse" in str(forwarded.url)
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["Hello", ", ", "world!"]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 4
        assert terminal.completion_tokens == 3


class TestEndToEndStreamingAnthropic:
    @pytest.mark.asyncio
    async def test_streaming_text_deltas_and_terminal(self, vertex_auth: VertexAuth) -> None:
        # Anthropic-on-Vertex emits the standard Anthropic SSE shape.
        sse_body = (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":", world!"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            route = mock.post(re.compile(r".*:streamRawPredict$")).mock(
                return_value=httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/anthropic/claude-3-5-haiku@20241022",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        # URL targets streamRawPredict (NOT streamGenerateContent).
        forwarded = route.calls.last.request
        forwarded_body = json.loads(forwarded.content)
        assert forwarded_body["stream"] is True
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["Hello", ", world!"]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 5
        assert terminal.completion_tokens == 4

    @pytest.mark.asyncio
    async def test_streaming_cache_tokens_surface_on_terminal_chunk(
        self, vertex_auth: VertexAuth
    ) -> None:
        """Phase 55: Anthropic-on-Vertex streaming exposes
        cache_creation_input_tokens + cache_read_input_tokens
        on the message_start usage block — same shape as direct
        Anthropic. Translator captures them and emits on the
        terminal chunk for FinOps headers + weighted cost math."""
        sse_body = (
            'data: {"type":"message_start","message":{"usage":{'
            '"input_tokens":50,"output_tokens":0,'
            '"cache_creation_input_tokens":1000,"cache_read_input_tokens":4000'
            "}}}\n\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"cached"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":6}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            mock.post(re.compile(r".*:streamRawPredict$")).mock(
                return_value=httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/anthropic/claude-3-5-haiku@20241022",
                    messages=[{"role": "user", "content": "cached?"}],
                    stream=True,
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 50
        assert terminal.completion_tokens == 6
        assert terminal.cache_creation_tokens == 1000
        assert terminal.cache_read_tokens == 4000

    @pytest.mark.asyncio
    async def test_streaming_tool_use_accumulates_args(self, vertex_auth: VertexAuth) -> None:
        sse_body = (
            'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"tool_use","id":"toolu_X","name":"get_weather"}}\n\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"input_json_delta","partial_json":"\\"Tokyo\\"}"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )
        with respx.mock(assert_all_called=True) as mock:
            _mock_token_exchange(mock)
            mock.post(re.compile(r".*:streamRawPredict$")).mock(
                return_value=httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                )
            )
            prov = VertexProvider(
                auth=vertex_auth,
                project_id="my-project",
                region="us-central1",
            )
            try:
                req = ChatCompletionRequest(
                    model="vertex/anthropic/claude-3-5-haiku@20241022",
                    messages=[{"role": "user", "content": "weather?"}],
                    stream=True,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                )
                chunks = [c async for c in await prov.chat_completion(req)]
            finally:
                await prov.aclose()
        terminal = chunks[-1]
        assert terminal.finish_reason == "tool_calls"
        assert terminal.tool_calls is not None
        assert len(terminal.tool_calls) == 1
        tc = terminal.tool_calls[0]
        assert tc["id"] == "toolu_X"
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "Tokyo"}


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


class TestCostMath:
    def test_gemini_flash_pricing(self, vertex_auth: VertexAuth) -> None:
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        # Gemini 1.5 Flash: $0.075/Mtok input = 7500 hcents/Mtok
        # 10_000 input tokens -> 7500 * 10_000 / 1_000_000 = 75 hcents
        # Output: $0.30/Mtok = 30_000 hcents/Mtok
        # 5_000 output -> 150 hcents
        # Total: 225 hcents
        cost = prov.cost_cents(
            prompt_tokens=10_000,
            completion_tokens=5_000,
            model="vertex/google/gemini-1.5-flash",
        )
        assert cost == 225

    def test_unknown_model_returns_zero(self, vertex_auth: VertexAuth) -> None:
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        cost = prov.cost_cents(
            prompt_tokens=100,
            completion_tokens=50,
            model="vertex/unknown/model",
        )
        assert cost == 0

    def test_gemini_thoughts_token_count_added_to_completion(self, vertex_auth: VertexAuth) -> None:
        """Phase 56: Gemini 2.0 Flash Thinking / 2.5 Pro report
        ``usageMetadata.thoughtsTokenCount`` as a SEPARATE billable
        count that's EXCLUDED from ``candidatesTokenCount``. Without
        Pronaos's fix, cost math would under-bill by 100% of the
        thinking portion. Verify the parser ADDS thoughts to the
        chunk's ``completion_tokens`` so downstream cost math is
        accurate, AND surfaces the raw count on ``reasoning_tokens``
        for the FinOps header."""
        data = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "The answer."}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 20,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 500,
                "totalTokenCount": 550,
            },
        }
        chunk = _parse_gemini_response(data)
        # candidates + thoughts = 30 + 500 = 530 billable output tokens.
        assert chunk.completion_tokens == 530
        assert chunk.reasoning_tokens == 500
        assert chunk.prompt_tokens == 20

    def test_gemini_no_thoughts_token_count_unaffected(self, vertex_auth: VertexAuth) -> None:
        """Regression: a Gemini non-thinking model leaves
        thoughtsTokenCount absent. completion_tokens must equal
        candidatesTokenCount alone (NO synthetic addition)."""
        data = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Hi."}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        }
        chunk = _parse_gemini_response(data)
        assert chunk.completion_tokens == 3
        assert chunk.reasoning_tokens == 0

    def test_anthropic_on_vertex_cache_weighted_math(self, vertex_auth: VertexAuth) -> None:
        """Phase 55: Anthropic-on-Vertex applies the same weighted
        prompt-cache pricing as direct Anthropic and Anthropic-on-Bedrock —
        1.25x for cache creation, 0.10x for cache reads."""
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        # Haiku 3.5 on Vertex: input 80_000 hcents/Mtok, output 400_000 hcents/Mtok.
        # 1000 non-cached input → 80 hcents
        # 500 cache_creation @ 1.25x → 50 hcents
        # 200 cache_read @ 0.10x → 1 hcent (integer truncation)
        # 500 output → 200 hcents
        # Total = 331 hcents
        cost = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="vertex/anthropic/claude-3-5-haiku@20241022",
            cache_creation_tokens=500,
            cache_read_tokens=200,
        )
        assert cost == 80 + 50 + 1 + 200  # 331

    def test_anthropic_on_vertex_cache_heavy_read_workload(self, vertex_auth: VertexAuth) -> None:
        """Cache-read-dominated workload (RAG re-prompt) costs much less
        than the same workload without the cache, confirming the 0.10x
        multiplier is wired correctly on Vertex too."""
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        # Sonnet 3.5 on Vertex: input 300_000 hcents/Mtok, output 1_500_000.
        # 100 non-cached + 10_000 cache_read + 200 output:
        # 100 input → 30 hcents
        # 10_000 cache_read @ 0.10x → 300 hcents
        # 200 output → 300 hcents
        # Total = 630 hcents
        with_cache = prov.cost_cents(
            prompt_tokens=100,
            completion_tokens=200,
            model="vertex/anthropic/claude-3-5-sonnet@20241022",
            cache_creation_tokens=0,
            cache_read_tokens=10_000,
        )
        assert with_cache == 30 + 300 + 300

    def test_gemini_publisher_ignores_cache_args(self, vertex_auth: VertexAuth) -> None:
        """Regression: passing cache_* args on a non-Anthropic publisher
        must NOT alter Gemini cost. Gemini does have a context-cache
        feature, but it bills through a separate price line (not the
        Anthropic 1.25x/0.10x scheme), so the cache-aware branch in
        VertexProvider.cost_cents skips it. This guards against the
        wrong-multiplier bug if someone forgets the publisher check."""
        prov = VertexProvider(
            auth=vertex_auth,
            project_id="my-project",
            region="us-central1",
        )
        cost_with = prov.cost_cents(
            prompt_tokens=10_000,
            completion_tokens=5_000,
            model="vertex/google/gemini-1.5-flash",
            cache_creation_tokens=999,
            cache_read_tokens=999,
        )
        cost_without = prov.cost_cents(
            prompt_tokens=10_000,
            completion_tokens=5_000,
            model="vertex/google/gemini-1.5-flash",
        )
        assert cost_with == cost_without == 225
