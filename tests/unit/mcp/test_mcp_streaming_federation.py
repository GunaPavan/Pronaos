"""Tests for the Phase 58 streaming MCP federation wrapper.

The federation loop itself is exercised by Phase 54's tests in
``test_mcp_client_federation.py`` and the live verify script. This
file covers ONLY the new code Phase 58 added:

1. ``_run_mcp_streaming_federation`` synthesises an OpenAI-shape SSE
   stream from a non-streaming federation response.
2. The first SSE chunk carries ``delta.role=assistant``.
3. Content chunks are sized at 64 chars each (matching Phase 28's
   streaming-replay chunking).
4. The terminal chunk carries ``finish_reason`` + tool_calls (if any).
5. The stream ends with ``data: [DONE]\\n\\n``.
6. Federation headers from the inner loop's Response are propagated
   onto the StreamingResponse.
7. ``X-Pronaos-MCP-Streamed: 1`` is stamped.
8. The streaming-federation metric counter is incremented with the
   correct ``result`` label.

The federation loop is mocked at the function boundary — we don't
re-test the multi-turn tool routing here (Phase 54 already does).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from pronaos.api.v1.chat import (
    ChatCompletionBody,
    _run_mcp_streaming_federation,
)
from pronaos.auth.api_keys import Principal


def _principal() -> Principal:
    """A throwaway Principal — the streaming wrapper passes it through
    to the (mocked) loop, so only the required Principal fields matter."""
    return Principal(
        tenant_id="t1",
        tenant_name="tenant-1",
        team_id="team-1",
        team_name="team-1",
        key_id="k1",
        key_prefix="pron_",
        scopes=frozenset({"chat:write"}),
        mcp_client_enabled=True,
    )


def _request() -> Request:
    """A FastAPI Request stub adequate for the wrapper's needs.
    The streaming wrapper doesn't actually use ``request`` directly
    (only the loop does, and that's mocked here)."""
    # Minimal ASGI scope. The wrapper passes ``request`` through to
    # the mocked loop, so the actual content doesn't matter.
    return Request(scope={"type": "http", "method": "POST", "headers": []})


def _body(**overrides: Any) -> ChatCompletionBody:
    """Sample body — the wrapper flips stream=False on the inner copy."""
    base: dict[str, Any] = {
        "model": "groq/llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "pronaos_mcp_servers": [
            {"name": "weather", "command": "python", "args": ["fake.py"]}
        ],
    }
    base.update(overrides)
    return ChatCompletionBody(**base)


async def _collect_sse_chunks(resp: StreamingResponse) -> list[dict[str, Any]]:
    """Drive the StreamingResponse's body iterator and parse the
    ``data:`` SSE lines into payload dicts. Returns one entry per
    chunk; ``[DONE]`` shows up as the sentinel dict ``{"_done": True}``."""
    out: list[dict[str, Any]] = []
    async for raw in resp.body_iterator:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        for piece in text.split("\n\n"):
            piece = piece.strip()
            if not piece.startswith("data:"):
                continue
            payload = piece[len("data:") :].strip()
            if payload == "[DONE]":
                out.append({"_done": True})
            else:
                out.append(json.loads(payload))
    return out


class TestStreamingFederationSynthesis:
    """Federation loop returns its non-streaming payload; the wrapper
    synthesises SSE."""

    @pytest.mark.asyncio
    async def test_first_chunk_has_role_assistant(self) -> None:
        final_payload = {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The current temperature in Tokyo is 17 degrees Celsius.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        loop_mock = AsyncMock(return_value=final_payload)
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop", loop_mock
        ):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            chunks = await _collect_sse_chunks(resp)

        # First chunk = role=assistant, no content, no finish.
        first = chunks[0]
        assert first["choices"][0]["delta"] == {"role": "assistant"}
        assert first["choices"][0]["finish_reason"] is None

    @pytest.mark.asyncio
    async def test_content_chunked_at_64_chars(self) -> None:
        content = "x" * 140  # 140 chars → 3 chunks (64 + 64 + 12).
        final_payload = {
            "id": "chatcmpl_test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop",
            AsyncMock(return_value=final_payload),
        ):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            chunks = await _collect_sse_chunks(resp)

        # Drop role chunk + terminal + [DONE]; what's left is content chunks.
        content_chunks = [
            c
            for c in chunks
            if not c.get("_done")
            and c.get("choices")
            and "content" in (c["choices"][0].get("delta") or {})
        ]
        assert len(content_chunks) == 3
        # Concatenated content must equal the original.
        reconstructed = "".join(
            c["choices"][0]["delta"]["content"] for c in content_chunks
        )
        assert reconstructed == content

    @pytest.mark.asyncio
    async def test_terminal_chunk_carries_finish_reason(self) -> None:
        final_payload = {
            "id": "id1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Done."},
                    "finish_reason": "stop",
                }
            ],
        }
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop",
            AsyncMock(return_value=final_payload),
        ):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            chunks = await _collect_sse_chunks(resp)

        # Find the chunk carrying finish_reason.
        finish_chunks = [
            c
            for c in chunks
            if not c.get("_done")
            and c.get("choices")
            and c["choices"][0].get("finish_reason")
        ]
        assert len(finish_chunks) == 1
        assert finish_chunks[0]["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_stream_ends_with_done_sentinel(self) -> None:
        final_payload = {
            "id": "id1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "x"},
                    "finish_reason": "stop",
                }
            ],
        }
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop",
            AsyncMock(return_value=final_payload),
        ):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            chunks = await _collect_sse_chunks(resp)

        assert chunks[-1] == {"_done": True}

    @pytest.mark.asyncio
    async def test_tool_calls_ride_on_terminal_chunk(self) -> None:
        """If the federation loop's final response includes client-
        supplied (non-federated) tool_calls, they ride on the terminal
        delta so OpenAI-shape consumers get them."""
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "client_tool", "arguments": "{}"},
            }
        ]
        final_payload = {
            "id": "id1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop",
            AsyncMock(return_value=final_payload),
        ):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            chunks = await _collect_sse_chunks(resp)

        terminal = [
            c
            for c in chunks
            if not c.get("_done")
            and c.get("choices")
            and c["choices"][0].get("finish_reason") == "tool_calls"
        ]
        assert len(terminal) == 1
        assert terminal[0]["choices"][0]["delta"]["tool_calls"] == tool_calls


class TestStreamingFederationHeaders:
    @pytest.mark.asyncio
    async def test_propagates_loop_headers(self) -> None:
        """Federation telemetry headers stamped on the inner Response
        must carry onto the StreamingResponse."""

        async def fake_loop(*, request, body, response, principal):
            response.headers["X-Pronaos-MCP-Federated-Servers"] = "weather"
            response.headers["X-Pronaos-MCP-Iterations"] = "2"
            return {
                "id": "id1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }

        with patch("pronaos.api.v1.chat._run_mcp_federation_loop", fake_loop):
            resp = await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )

        assert resp.headers["X-Pronaos-MCP-Federated-Servers"] == "weather"
        assert resp.headers["X-Pronaos-MCP-Iterations"] == "2"
        assert resp.headers["X-Pronaos-MCP-Streamed"] == "1"
        assert resp.media_type == "text/event-stream"


class TestStreamingFederationMetrics:
    @pytest.mark.asyncio
    async def test_ok_result_recorded(self) -> None:
        final_payload = {
            "id": "id1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop",
            AsyncMock(return_value=final_payload),
        ), patch(
            "pronaos.api.v1.chat.record_mcp_streaming_federation_session"
        ) as rec:
            await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            rec.assert_called_once_with(result="ok")

    @pytest.mark.asyncio
    async def test_invalid_spec_recorded(self) -> None:
        """If the federation loop raises HTTPException with
        ``mcp_invalid_spec`` detail, the wrapper records invalid_spec
        and re-raises so FastAPI returns the 422."""

        async def fake_loop(**kw):
            raise HTTPException(
                status_code=422,
                detail={"type": "mcp_invalid_spec", "message": "bad"},
            )

        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop", fake_loop
        ), patch(
            "pronaos.api.v1.chat.record_mcp_streaming_federation_session"
        ) as rec:
            with pytest.raises(HTTPException) as exc:
                await _run_mcp_streaming_federation(
                    request=_request(),
                    body=_body(),
                    principal=_principal(),
                )
            assert exc.value.status_code == 422
            rec.assert_called_once_with(result="invalid_spec")

    @pytest.mark.asyncio
    async def test_max_iterations_recorded(self) -> None:
        """If the inner loop hit the iteration cap, the
        X-Pronaos-MCP-Max-Iterations-Reached header signals it.
        The wrapper translates that into a max_iterations result."""

        async def fake_loop(*, request, body, response, principal):
            response.headers["X-Pronaos-MCP-Max-Iterations-Reached"] = "1"
            return {
                "id": "id1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
            }

        with patch(
            "pronaos.api.v1.chat._run_mcp_federation_loop", fake_loop
        ), patch(
            "pronaos.api.v1.chat.record_mcp_streaming_federation_session"
        ) as rec:
            await _run_mcp_streaming_federation(
                request=_request(),
                body=_body(),
                principal=_principal(),
            )
            rec.assert_called_once_with(result="max_iterations")


class TestEntrypoint422GateRemoved:
    """The Phase 54 422 ``mcp_streaming_unsupported`` gate is removed.
    Verified by importing the chat module and confirming the error
    string no longer appears in source."""

    def test_no_streaming_unsupported_error_string(self) -> None:
        from pathlib import Path

        chat_src = Path("src/pronaos/api/v1/chat.py").read_text(encoding="utf-8")
        assert "mcp_streaming_unsupported" not in chat_src, (
            "The streaming gate should be removed (Phase 58 closes it)."
        )
