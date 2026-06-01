"""Tests for the Phase 48 MCP server adapter.

Two surfaces:

- ``PronaosMcpServer`` construction + tool registration shape
  (no transport — uses the SDK's in-memory client/server pairing).
- The bearer-token ContextVar plumbing (set / read / reset).

The end-to-end SSE-transport path is covered by the live verify
script (``scripts/verify_mcp_server.py``); reproducing that here
would require spinning up a real httpx connection and the SSE
SDK's bidirectional streams.
"""

from __future__ import annotations

import pytest

from pronaos.mcp.server import (
    PronaosMcpServer,
    current_bearer_token,
    reset_bearer_token,
    set_bearer_token,
)


class TestBearerTokenContextVar:
    """The token ContextVar isolates concurrent connections."""

    def test_default_is_none(self) -> None:
        # In a fresh task, the contextvar is unset.
        assert current_bearer_token() is None

    def test_set_and_read(self) -> None:
        reset = set_bearer_token("pn_test_abc")
        try:
            assert current_bearer_token() == "pn_test_abc"
        finally:
            reset_bearer_token(reset)
        assert current_bearer_token() is None

    @pytest.mark.asyncio
    async def test_isolated_across_tasks(self) -> None:
        # Each asyncio task gets its own contextvar copy on creation.
        import asyncio

        seen: dict[str, str | None] = {}

        async def setter(label: str, token: str) -> None:
            reset = set_bearer_token(token)
            try:
                await asyncio.sleep(0)
                seen[label] = current_bearer_token()
            finally:
                reset_bearer_token(reset)

        # Run two tasks concurrently with different tokens.
        await asyncio.gather(
            setter("a", "pn_test_a"),
            setter("b", "pn_test_b"),
        )
        assert seen == {"a": "pn_test_a", "b": "pn_test_b"}
        # Parent task's view still unset.
        assert current_bearer_token() is None


class TestPronaosMcpServerConstruction:
    def test_server_has_pronaos_name(self) -> None:
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        assert srv.mcp.name == "pronaos"

    def test_gateway_url_trailing_slash_stripped(self) -> None:
        # Trailing slash on the base URL must be stripped so the
        # loopback paths don't double-slash.
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080/")
        # No public accessor; check via the underlying attr.
        assert srv._gateway_url == "http://127.0.0.1:8080"


class TestToolDescriptors:
    """The MCP tools/list response — schema shape, names, descriptions."""

    @pytest.mark.asyncio
    async def test_lists_three_tools(self) -> None:
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        # Use the SDK's registered handler directly. The handler the
        # decorator stored has the name we registered; call it.
        request_handlers = srv.mcp.request_handlers
        # The handler registry uses the MCP request-type class as key.
        from mcp.types import ListToolsRequest

        handler = request_handlers[ListToolsRequest]
        # MCP request shape — empty params object.
        result = await handler(ListToolsRequest(method="tools/list", params=None))
        # Result is a ServerResult wrapping a ListToolsResult.
        list_result = result.root
        names = {tool.name for tool in list_result.tools}
        assert names == {"pronaos.chat", "pronaos.embed", "pronaos.rerank"}

    @pytest.mark.asyncio
    async def test_chat_tool_schema_requires_model_and_messages(self) -> None:
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        from mcp.types import ListToolsRequest

        handler = srv.mcp.request_handlers[ListToolsRequest]
        result = await handler(ListToolsRequest(method="tools/list", params=None))
        list_result = result.root
        chat = next(t for t in list_result.tools if t.name == "pronaos.chat")
        assert chat.inputSchema["type"] == "object"
        assert set(chat.inputSchema["required"]) == {"model", "messages"}
        assert "max_tokens" in chat.inputSchema["properties"]
        assert "temperature" in chat.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_embed_tool_schema_accepts_string_or_array(self) -> None:
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        from mcp.types import ListToolsRequest

        handler = srv.mcp.request_handlers[ListToolsRequest]
        result = await handler(ListToolsRequest(method="tools/list", params=None))
        list_result = result.root
        embed = next(t for t in list_result.tools if t.name == "pronaos.embed")
        assert set(embed.inputSchema["required"]) == {"model", "input"}
        # The ``input`` field accepts either a string or a list of strings —
        # mirrors the REST endpoint's accept shape.
        input_schema = embed.inputSchema["properties"]["input"]
        assert "oneOf" in input_schema

    @pytest.mark.asyncio
    async def test_rerank_tool_schema_requires_query_and_documents(self) -> None:
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        from mcp.types import ListToolsRequest

        handler = srv.mcp.request_handlers[ListToolsRequest]
        result = await handler(ListToolsRequest(method="tools/list", params=None))
        list_result = result.root
        rerank = next(t for t in list_result.tools if t.name == "pronaos.rerank")
        assert set(rerank.inputSchema["required"]) == {
            "model",
            "query",
            "documents",
        }


class TestToolCallForwarding:
    """The tool-call dispatcher routes by name and forwards via httpx.

    Uses respx to intercept the loopback HTTP call so we can assert
    on the forwarded request shape and serve a synthetic response.
    """

    @pytest.mark.asyncio
    async def test_chat_call_forwards_to_v1_chat_completions(self) -> None:
        import json

        import respx
        from httpx import Response
        from mcp.types import CallToolRequest, CallToolRequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        from mcp.types import CallToolRequest as _CallToolRequest

        handler = srv.mcp.request_handlers[_CallToolRequest]

        reset = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                route = mock.post("/v1/chat/completions").mock(
                    return_value=Response(
                        200,
                        json={
                            "id": "chatcmpl-1",
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "hello",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 1,
                                "total_tokens": 6,
                            },
                        },
                    )
                )
                req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="pronaos.chat",
                        arguments={
                            "model": "groq/llama-3.1-8b-instant",
                            "messages": [{"role": "user", "content": "say hi"}],
                            "max_tokens": 5,
                        },
                    ),
                )
                result = await handler(req)
        finally:
            reset_bearer_token(reset)

        # respx saw the forwarded call.
        assert route.called
        forwarded_request = route.calls.last.request
        assert forwarded_request.headers["authorization"] == "Bearer pn_test_xyz"
        forwarded_body = json.loads(forwarded_request.content)
        assert forwarded_body["model"] == "groq/llama-3.1-8b-instant"
        assert forwarded_body["messages"][0]["content"] == "say hi"
        # MCP result carries the gateway's JSON response as TextContent.
        call_result = result.root
        assert call_result.isError is False
        assert len(call_result.content) == 1
        text = call_result.content[0].text
        payload = json.loads(text)
        assert payload["choices"][0]["message"]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_call_without_bearer_token_raises(self) -> None:
        # No set_bearer_token() before invocation → tool handler must
        # fail loudly so wiring bugs surface immediately rather than
        # producing a confusing upstream 401.
        from mcp.types import CallToolRequest, CallToolRequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        handler = srv.mcp.request_handlers[CallToolRequest]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="pronaos.chat",
                arguments={"model": "x", "messages": []},
            ),
        )
        # Server wraps exceptions in a CallToolResult with isError=True
        # rather than re-raising. Either is acceptable; we assert the
        # outward shape, not the implementation detail.
        result = await handler(req)
        call_result = result.root
        assert call_result.isError is True
        text = call_result.content[0].text
        assert "no bearer token" in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_iserror(self) -> None:
        from mcp.types import CallToolRequest, CallToolRequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        handler = srv.mcp.request_handlers[CallToolRequest]
        reset = set_bearer_token("pn_test_abc")
        try:
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="pronaos.nonexistent",
                    arguments={},
                ),
            )
            result = await handler(req)
        finally:
            reset_bearer_token(reset)
        call_result = result.root
        assert call_result.isError is True

    @pytest.mark.asyncio
    async def test_embed_call_forwards_to_v1_embeddings(self) -> None:
        import json

        import respx
        from httpx import Response
        from mcp.types import CallToolRequest, CallToolRequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        handler = srv.mcp.request_handlers[CallToolRequest]
        reset = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                route = mock.post("/v1/embeddings").mock(
                    return_value=Response(
                        200,
                        json={
                            "data": [{"embedding": [0.1, 0.2], "index": 0}],
                            "model": "openai/text-embedding-3-small",
                            "usage": {"prompt_tokens": 2, "total_tokens": 2},
                        },
                    )
                )
                req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="pronaos.embed",
                        arguments={
                            "model": "openai/text-embedding-3-small",
                            "input": "hello",
                        },
                    ),
                )
                result = await handler(req)
        finally:
            reset_bearer_token(reset)
        assert route.called
        call_result = result.root
        assert call_result.isError is False
        payload = json.loads(call_result.content[0].text)
        assert payload["model"] == "openai/text-embedding-3-small"


# --------------------------------------------------------------------------- #
# Phase 51 — streaming progress notifications                                  #
# --------------------------------------------------------------------------- #
#
# These tests exercise ``_forward_chat_streaming`` directly. Setting up the
# full SDK in-memory client/server pairing JUST to drive a progressToken
# round trip is more harness than the change deserves, and the live verify
# script (``scripts/verify_mcp_streaming.py``) covers the SDK's actual
# dispatch path against a real subprocess. The unit tests focus on the new
# code: progress-token detection, SSE chunk parsing, notification fan-out,
# and final-payload synthesis.


class _CapturingSession:
    """Stand-in for ``ServerSession`` that records the progress
    notifications a handler emits — enough surface to assert on
    fan-out behaviour without a real SDK transport."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, object]] = []

    async def send_progress_notification(
        self,
        progress_token: str | int,
        progress: float,
        total: float | None = None,
        message: str | None = None,
        related_request_id: str | None = None,
    ) -> None:
        self.notifications.append(
            {
                "progress_token": progress_token,
                "progress": progress,
                "total": total,
                "message": message,
                "related_request_id": related_request_id,
            }
        )


def _build_streaming_sse_body(
    chunks: list[str], *, final_usage: dict[str, int] | None = None
) -> str:
    """Synthesize an OpenAI-shape SSE body: one ``data:`` frame per
    content delta plus a final frame with ``finish_reason=stop`` and
    the optional usage block, then ``[DONE]``."""
    import json as _json

    lines: list[str] = []
    completion_id = "chatcmpl-test-stream"
    for i, delta in enumerate(chunks):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": 1700000000 + i,
            "model": "groq/llama-3.1-8b-instant",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": delta},
                    "finish_reason": None,
                }
            ],
        }
        lines.append("data: " + _json.dumps(chunk))
        lines.append("")  # SSE event terminator
    # Final frame: finish_reason=stop + optional usage
    final_chunk: dict[str, object] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 1700000099,
        "model": "groq/llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    if final_usage is not None:
        final_chunk["usage"] = final_usage
    lines.append("data: " + _json.dumps(final_chunk))
    lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines) + "\n"


class TestStreamingProgressNotifications:
    @pytest.mark.asyncio
    async def test_streaming_fires_one_progress_per_content_chunk(self) -> None:
        """Each SSE chunk with a non-empty content delta produces one
        progress notification carrying that delta as ``message``.

        Asserts:
        - len(notifications) ≥ N (one per delta) + 1 for the
          finish-reason frame.
        - Progress values are strictly monotonic 1.0, 2.0, 3.0, ...
        - ``message`` payloads concatenate to the full assistant text.
        - Final ``CallToolResult`` carries the accumulated content +
          synthesized ``finish_reason=stop`` + usage block.
        """
        import json

        import respx
        from httpx import Response

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        sse_body = _build_streaming_sse_body(
            ["Hello", ", ", "world", "!"],
            final_usage={
                "prompt_tokens": 5,
                "completion_tokens": 4,
                "total_tokens": 9,
            },
        )

        # Set up a fake RequestContext on the SDK's request_ctx
        # ContextVar so the streaming branch can read
        # ``self._mcp.request_context.session`` + ``.request_id``.
        from mcp.server.lowlevel.server import request_ctx
        from mcp.shared.context import RequestContext
        from mcp.types import RequestParams

        session = _CapturingSession()
        ctx = RequestContext(
            request_id="req-123",
            meta=RequestParams.Meta(progressToken="prog-abc"),
            session=session,  # type: ignore[arg-type]
            lifespan_context=None,
        )
        ctx_token = request_ctx.set(ctx)
        reset_bearer = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                mock.post("/v1/chat/completions").mock(
                    return_value=Response(
                        200,
                        text=sse_body,
                        headers={"content-type": "text/event-stream"},
                    )
                )
                result = await srv._forward_chat_streaming(
                    bearer="pn_test_xyz",
                    body={
                        "model": "groq/llama-3.1-8b-instant",
                        "messages": [{"role": "user", "content": "say hi"}],
                    },
                    progress_token="prog-abc",
                )
        finally:
            reset_bearer_token(reset_bearer)
            request_ctx.reset(ctx_token)

        # Notifications: 4 content chunks + 1 finish-reason frame = 5.
        assert len(session.notifications) == 5

        # Progress monotonic 1..5
        progresses = [n["progress"] for n in session.notifications]
        assert progresses == [1.0, 2.0, 3.0, 4.0, 5.0]

        # First four notifications carry deltas as ``message``; the
        # fifth (finish-reason frame) has ``message=None`` because
        # its delta is empty.
        messages = [n["message"] for n in session.notifications]
        assert messages[:4] == ["Hello", ", ", "world", "!"]
        assert messages[4] is None

        # Every notification carries the progressToken + the related
        # request_id we stamped on the ContextVar.
        for n in session.notifications:
            assert n["progress_token"] == "prog-abc"
            assert n["related_request_id"] == "req-123"

        # Final result: one TextContent holding a complete
        # ChatCompletion-shape payload.
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["object"] == "chat.completion"
        assert payload["id"] == "chatcmpl-test-stream"
        assert payload["model"] == "groq/llama-3.1-8b-instant"
        assert payload["choices"][0]["message"]["content"] == "Hello, world!"
        assert payload["choices"][0]["finish_reason"] == "stop"
        # Usage block preserved end-to-end.
        assert payload["usage"]["total_tokens"] == 9
        # Pronaos marker so clients can tell the response came via
        # streaming.
        assert payload["pronaos"] == {"mcp_streamed": True, "chunks": 5}

    @pytest.mark.asyncio
    async def test_no_progress_token_uses_non_streaming_path(self) -> None:
        """When the inbound tools/call has no ``_meta.progressToken``,
        the chat tool takes the non-streaming branch and the gateway
        sees ``stream`` absent/false on the forwarded request."""
        import json

        import respx
        from httpx import Response
        from mcp.types import CallToolRequest, CallToolRequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        handler = srv.mcp.request_handlers[CallToolRequest]

        reset = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                route = mock.post("/v1/chat/completions").mock(
                    return_value=Response(
                        200,
                        json={
                            "id": "chatcmpl-1",
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "hello",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    )
                )
                # No _meta on the request → no progress token.
                req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="pronaos.chat",
                        arguments={
                            "model": "groq/llama-3.1-8b-instant",
                            "messages": [{"role": "user", "content": "say hi"}],
                        },
                    ),
                )
                result = await handler(req)
        finally:
            reset_bearer_token(reset)

        # Non-streaming path: forwarded body should NOT carry
        # ``stream=true`` because we never opted into streaming.
        forwarded_body = json.loads(route.calls.last.request.content)
        assert forwarded_body.get("stream") is not True

        # Final result equals the synthetic non-streaming response.
        call_result = result.root
        assert call_result.isError is False
        payload = json.loads(call_result.content[0].text)
        assert payload["choices"][0]["message"]["content"] == "hello"
        # The non-streaming path does NOT stamp ``pronaos.mcp_streamed``
        # — that marker only appears on responses synthesized from the
        # streaming branch.
        assert "pronaos" not in payload or "mcp_streamed" not in payload.get("pronaos", {})

    @pytest.mark.asyncio
    async def test_streaming_branch_forces_stream_true_on_upstream(self) -> None:
        """The forwarded loopback request MUST carry ``stream=true``
        regardless of what the MCP client passed in ``arguments``.

        A client that supplies a progressToken AND ``stream=false``
        in arguments has expressed contradictory intent. The progress
        token wins (it's the more recent / explicit signal), and we
        force ``stream=true`` so the SSE chunks actually flow."""
        import json

        import respx
        from httpx import Response

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        sse_body = _build_streaming_sse_body(["a"])

        from mcp.server.lowlevel.server import request_ctx
        from mcp.shared.context import RequestContext
        from mcp.types import RequestParams

        session = _CapturingSession()
        ctx = RequestContext(
            request_id="req-456",
            meta=RequestParams.Meta(progressToken=42),  # int token also legal
            session=session,  # type: ignore[arg-type]
            lifespan_context=None,
        )
        ctx_token = request_ctx.set(ctx)
        reset_bearer = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                route = mock.post("/v1/chat/completions").mock(
                    return_value=Response(
                        200,
                        text=sse_body,
                        headers={"content-type": "text/event-stream"},
                    )
                )
                await srv._forward_chat_streaming(
                    bearer="pn_test_xyz",
                    body={
                        "model": "groq/llama-3.1-8b-instant",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,  # contradictory — progress token wins
                    },
                    progress_token=42,
                )
        finally:
            reset_bearer_token(reset_bearer)
            request_ctx.reset(ctx_token)

        forwarded_body = json.loads(route.calls.last.request.content)
        assert forwarded_body["stream"] is True
        # The int progress token survives untouched.
        assert session.notifications[0]["progress_token"] == 42

    @pytest.mark.asyncio
    async def test_streaming_upstream_error_surfaces_as_iserror(self) -> None:
        """When the upstream loopback returns non-200 before any
        chunk arrives, the final result is the error JSON and zero
        progress notifications fire. The streaming-session metric
        records ``upstream_error``."""
        import json

        import respx
        from httpx import Response
        from mcp.server.lowlevel.server import request_ctx
        from mcp.shared.context import RequestContext
        from mcp.types import RequestParams

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        session = _CapturingSession()
        ctx = RequestContext(
            request_id="req-789",
            meta=RequestParams.Meta(progressToken="prog-err"),
            session=session,  # type: ignore[arg-type]
            lifespan_context=None,
        )
        ctx_token = request_ctx.set(ctx)
        reset_bearer = set_bearer_token("pn_test_xyz")
        try:
            with respx.mock(base_url="http://127.0.0.1:8080") as mock:
                mock.post("/v1/chat/completions").mock(
                    return_value=Response(
                        429,
                        json={
                            "detail": {
                                "type": "quota_exhausted",
                                "remaining_tokens": 0,
                            }
                        },
                    )
                )
                result = await srv._forward_chat_streaming(
                    bearer="pn_test_xyz",
                    body={
                        "model": "groq/llama-3.1-8b-instant",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    progress_token="prog-err",
                )
        finally:
            reset_bearer_token(reset_bearer)
            request_ctx.reset(ctx_token)

        # Zero progress notifications — error happened before any
        # streaming body arrived.
        assert session.notifications == []
        # Final result carries the upstream's error body verbatim.
        payload = json.loads(result[0].text)
        assert payload["detail"]["type"] == "quota_exhausted"

    @pytest.mark.asyncio
    async def test_read_progress_token_outside_request_context_returns_none(
        self,
    ) -> None:
        """``_read_progress_token`` returns None when invoked outside
        an MCP request context (unit-test / direct-call path) instead
        of raising LookupError. Lets the chat dispatcher fall through
        to the non-streaming branch transparently."""
        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        assert srv._read_progress_token() is None

    @pytest.mark.asyncio
    async def test_read_progress_token_returns_none_when_meta_absent(
        self,
    ) -> None:
        """In-request context but no ``_meta`` block → return None
        without raising. Common shape: a client that doesn't request
        progress at all."""
        from mcp.server.lowlevel.server import request_ctx
        from mcp.shared.context import RequestContext

        srv = PronaosMcpServer(gateway_url="http://127.0.0.1:8080")
        ctx = RequestContext(
            request_id="req-no-meta",
            meta=None,
            session=_CapturingSession(),  # type: ignore[arg-type]
            lifespan_context=None,
        )
        ctx_token = request_ctx.set(ctx)
        try:
            assert srv._read_progress_token() is None
        finally:
            request_ctx.reset(ctx_token)
