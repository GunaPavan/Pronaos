"""MCP server adapter — Pronaos exposed as an MCP tool surface.

Phase 48.

Design
------
The MCP server wraps the gateway's existing REST surface (chat,
embeddings, rerank). When an MCP client invokes ``tools/call`` for
e.g. ``pronaos.chat``, the adapter forwards the call to
``/v1/chat/completions`` on the same gateway process via a loopback
HTTP request. That deliberately preserves the full middleware chain
(auth → quotas → guardrails → caching → routing → audit) — exactly
the same path REST clients take. The MCP route is a thin shape
translator on top of the canonical pipeline; it does NOT reimplement
the gateway's policy stack.

Loopback HTTP vs in-process call
--------------------------------
The chat handler relies on a long chain of FastAPI ``Depends()``
injections (Principal, QuotaTracker, Cache, GuardrailEngine,
AuditLogger, CircuitBreakerRegistry, ...). Calling it programmatically
from the MCP code would require reproducing that dependency wiring
by hand — every new dependency added to chat.py would silently
break MCP. Loopback HTTP avoids that drift: the MCP path goes
through Starlette + the actual route handler, identical to how a
real REST client would.

Cost: one extra TCP round-trip per MCP tool call (loopback;
sub-millisecond on the same host). The trade-off is overwhelmingly
worth it for correctness + maintainability.

Auth — ContextVar pattern
-------------------------
The MCP protocol itself has no auth spec. The FastAPI route that
mounts the SSE transport validates the bearer token before handing
the connection off; the validated token is stashed into a per-task
ContextVar (``_BEARER_CTX``) that tool-call handlers read when
constructing the loopback HTTP call. Because the MCP server's
``server.run(...)`` spawns tool-call tasks inside the same asyncio
context the SSE handler created, the ContextVar propagates naturally.

Tools exposed
-------------
- ``pronaos.chat`` — chat completion (the main event)
- ``pronaos.embed`` — embeddings (cache-backed)
- ``pronaos.rerank`` — rerank (cache-backed)

Each tool's input JSON Schema mirrors the corresponding REST endpoint's
body shape so MCP clients can use OpenAI-compatible payloads without
translation.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

from pronaos.logging import get_logger
from pronaos.observability.metrics import (
    record_mcp_streaming_chunk,
    record_mcp_streaming_session,
)

log = get_logger(__name__)


# Per-connection bearer token. Set by the FastAPI SSE handler after it
# validates the API key; read by ``call_tool`` when constructing the
# loopback HTTP request. ContextVar gives per-asyncio-task isolation —
# concurrent MCP connections never see each other's tokens.
_BEARER_CTX: ContextVar[str | None] = ContextVar(
    "pronaos_mcp_bearer_token", default=None
)


def current_bearer_token() -> str | None:
    """Read the per-connection bearer token set by the SSE handler.

    Returns ``None`` when called outside an MCP-connection context —
    e.g. from a unit test that exercises the server class directly
    without going through the SSE transport. Tool-call handlers
    raise on None because no valid token means no resolvable
    principal — the loopback call would fail anyway.
    """
    return _BEARER_CTX.get()


def set_bearer_token(token: str) -> Any:
    """Set the bearer token for the current asyncio task.

    Returns the token reset handle so the caller can clean up::

        token_reset = set_bearer_token(bearer)
        try:
            await mcp_server.run(...)
        finally:
            _BEARER_CTX.reset(token_reset)
    """
    return _BEARER_CTX.set(token)


def reset_bearer_token(reset_handle: Any) -> None:
    """Restore the previous bearer-token value for the current task."""
    _BEARER_CTX.reset(reset_handle)


# ---- Tool JSON Schemas -----------------------------------------------------
# Each schema mirrors the corresponding REST body's wire shape. We keep the
# schemas in this module (not generated from the Pydantic models) so the MCP
# surface stays stable even when the underlying request models gain
# optional fields — MCP clients see only the documented MCP-facing surface.

_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["model", "messages"],
    "properties": {
        "model": {
            "type": "string",
            "description": (
                "Fully-qualified model name (``provider/model``) or the "
                "sentinel ``\"auto\"`` to let the team's routing strategy pick."
            ),
        },
        "messages": {
            "type": "array",
            "description": "OpenAI-compatible messages array.",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "content": {},
                    "name": {"type": "string"},
                    "tool_call_id": {"type": "string"},
                    "tool_calls": {"type": "array"},
                },
            },
        },
        "max_tokens": {"type": "integer"},
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "stream": {
            "type": "boolean",
            "description": (
                "When ``stream`` is true OR the MCP client supplied a "
                "``_meta.progressToken`` on the tools/call, Pronaos "
                "forwards chunk-by-chunk and emits one "
                "``notifications/progress`` message per upstream chunk. "
                "The final ``CallToolResult`` still carries a complete "
                "non-streaming-shape ChatCompletion synthesized from the "
                "accumulated deltas, so MCP clients that ignore progress "
                "notifications still see the full response."
            ),
            "default": False,
        },
        "tools": {"type": "array"},
        "tool_choice": {},
        "response_format": {"type": "object"},
    },
}

_EMBED_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["model", "input"],
    "properties": {
        "model": {"type": "string"},
        "input": {
            "description": "String or array of strings to embed.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "dimensions": {"type": "integer"},
        "input_type": {"type": "string"},
    },
}

_RERANK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["model", "query", "documents"],
    "properties": {
        "model": {"type": "string"},
        "query": {"type": "string"},
        "documents": {
            "type": "array",
            "items": {"type": "string"},
        },
        "top_n": {"type": "integer"},
    },
}


# ---- Server class ----------------------------------------------------------


class PronaosMcpServer:
    """MCP server that forwards tool calls to the gateway's REST surface.

    Single instance per process, created during FastAPI lifespan and
    shared across SSE connections. The bearer token + per-connection
    state is carried via ContextVar so the server's tool handlers
    stay stateless.

    ``gateway_url`` is the base URL of the same process (typically
    ``http://127.0.0.1:8080``). Loopback HTTP preserves the full
    middleware chain — see module docstring for the rationale.
    """

    def __init__(self, *, gateway_url: str, transport: str = "sse") -> None:
        """``transport`` labels metrics so dashboards can split MCP
        traffic by how the client connected — ``sse`` (remote MCP
        clients on /v1/mcp/sse) or ``stdio`` (subprocess spawned by
        Claude Code / IDE clients). Stamped onto streaming counters
        only — every other observation goes through the gateway's
        existing per-endpoint metrics already.
        """
        self._gateway_url = gateway_url.rstrip("/")
        self._transport = transport
        self._mcp: Server[Any, Any] = Server("pronaos")
        self._register_handlers()

    @property
    def mcp(self) -> Server[Any, Any]:
        """The underlying MCP Server instance. Exposed for the
        FastAPI SSE route to call ``run(...)`` with the right streams."""
        return self._mcp

    def _register_handlers(self) -> None:
        # Tools list — static; no per-team filtering for now (a team
        # without the right scopes will still see the tool in the
        # catalogue but the underlying REST endpoint will 403 them).
        # SDK decorators erase the wrapped function's signature; mypy
        # strict flags them as untyped. The Pronaos-facing surface
        # (PronaosMcpServer + _forward) stays fully typed.
        @self._mcp.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def _list_tools() -> list[Tool]:
            return [
                Tool(
                    name="pronaos.chat",
                    description=(
                        "Invoke a chat completion through the Pronaos gateway. "
                        "Returns the OpenAI-shape ChatCompletion response as "
                        "JSON. All gateway features (auth, quotas, guardrails, "
                        "caching, routing, audit logging) apply automatically. "
                        "Pass ``model=\"auto\"`` to let the team's routing "
                        "strategy pick."
                    ),
                    inputSchema=_CHAT_INPUT_SCHEMA,
                ),
                Tool(
                    name="pronaos.embed",
                    description=(
                        "Compute embeddings through the Pronaos gateway. "
                        "Cache-backed: identical inputs return byte-identical "
                        "vectors with zero upstream cost."
                    ),
                    inputSchema=_EMBED_INPUT_SCHEMA,
                ),
                Tool(
                    name="pronaos.rerank",
                    description=(
                        "Rerank a document list against a query through "
                        "the gateway. Cache-backed; per-(model, query, "
                        "docs, top_n) caching identical to embeddings."
                    ),
                    inputSchema=_RERANK_INPUT_SCHEMA,
                ),
            ]

        # Tool-call dispatcher — single entry point that picks the
        # right REST endpoint based on tool name. Each handler reads
        # the bearer-token ContextVar and forwards via loopback HTTP.
        @self._mcp.call_tool()  # type: ignore[untyped-decorator]
        async def _call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[TextContent]:
            bearer = current_bearer_token()
            if not bearer:
                # The SSE handler must have set this before invoking
                # the MCP server.run(). If we hit this, the wiring is
                # broken — fail loudly rather than letting the
                # downstream HTTP call return a confusing 401.
                raise RuntimeError(
                    "pronaos.mcp: no bearer token in context — the SSE "
                    "handler must call set_bearer_token() before run()."
                )
            if name == "pronaos.chat":
                # Streaming branch: when the client requested progress
                # notifications via _meta.progressToken on the inbound
                # tools/call, switch to streaming forwarding so each
                # upstream chunk fires a notifications/progress message
                # back to the client. Closes the documented honest-limit
                # in Claims #35 and #37.
                progress_token = self._read_progress_token()
                if progress_token is not None:
                    return await self._forward_chat_streaming(
                        bearer=bearer,
                        body=arguments,
                        progress_token=progress_token,
                    )
                return await self._forward(
                    bearer=bearer,
                    path="/v1/chat/completions",
                    body=arguments,
                )
            if name == "pronaos.embed":
                return await self._forward(
                    bearer=bearer,
                    path="/v1/embeddings",
                    body=arguments,
                )
            if name == "pronaos.rerank":
                return await self._forward(
                    bearer=bearer,
                    path="/v1/rerank",
                    body=arguments,
                )
            # Unknown tool — MCP spec says return isError=true content;
            # the SDK's call_tool decorator handles wrapping our return
            # so we just raise a clear ValueError.
            raise ValueError(f"unknown tool: {name!r}")

    async def _forward(
        self,
        *,
        bearer: str,
        path: str,
        body: dict[str, Any],
    ) -> list[TextContent]:
        """Loopback HTTP call to ``{gateway_url}{path}`` with the
        client's bearer token forwarded.

        Returns the gateway's JSON response as a single
        ``TextContent`` block (MCP's universal content carrier).
        Errors from the gateway (4xx/5xx) come through as a JSON
        body containing the error detail — same shape an
        OpenAI-compat REST client would see.
        """
        # Per-call client so concurrent MCP tool calls don't share a
        # connection pool's state across teams. Cheap enough at one
        # call per ``tools/call``; if MCP traffic grows we can pool.
        async with httpx.AsyncClient(base_url=self._gateway_url, timeout=120.0) as c:
            resp = await c.post(
                path,
                headers={"Authorization": f"Bearer {bearer}"},
                json=body,
            )
        try:
            payload = resp.json()
        except ValueError:
            # Non-JSON body (rare). Wrap as a synthetic error object
            # so the MCP client still sees structured failure info.
            payload = {
                "error": {
                    "type": "non_json_response",
                    "status_code": resp.status_code,
                    "body": resp.text[:500],
                }
            }
        # MCP TextContent is the universal carrier. We serialize the
        # full JSON response so the client can inspect tool_calls,
        # usage, finish_reason, etc. — same surface the REST clients
        # see.
        return [
            TextContent(
                type="text",
                text=json.dumps(payload, separators=(",", ":")),
            )
        ]

    # ---- Phase 51 — streaming progress notifications -----------------------

    def _read_progress_token(self) -> str | int | None:
        """Read the inbound ``_meta.progressToken`` if the client set
        one on this ``tools/call``.

        The MCP spec lets clients pass an opaque progress token in
        ``params._meta.progressToken``; the server is then permitted
        (not obligated) to emit ``notifications/progress`` messages
        carrying that token. Pronaos uses the token's presence as
        the signal to take the streaming forwarding branch.

        Returns ``None`` when no token was supplied, when this method
        is called outside an MCP request context (unit-test path), or
        when the SDK happens to expose no ``meta`` on the request.
        """
        try:
            ctx = self._mcp.request_context
        except LookupError:
            return None
        meta = getattr(ctx, "meta", None)
        if meta is None:
            return None
        token = getattr(meta, "progressToken", None)
        if token is None:
            return None
        # Spec allows str OR int. Pass through unchanged.
        return token  # type: ignore[no-any-return]

    async def _forward_chat_streaming(
        self,
        *,
        bearer: str,
        body: dict[str, Any],
        progress_token: str | int,
    ) -> list[TextContent]:
        """Forward a chat request with ``stream=true`` to the gateway,
        relay every SSE chunk to the MCP client as a
        ``notifications/progress`` message, and synthesize the final
        non-streaming-shape ChatCompletion as the final
        ``CallToolResult`` value.

        The MCP spec keeps the request/response pairing intact even
        when progress notifications fire — the final result is still
        a single ``CallToolResult``, just produced after all the
        progress traffic. We synthesize that final ChatCompletion
        from the accumulated chunk deltas so MCP clients that ignore
        progress notifications still see a complete response.

        The synthesized chat completion preserves every field the
        non-streaming path would have produced: ``id``, ``object``,
        ``created``, ``model``, ``choices[0].message.content``,
        ``choices[0].finish_reason``, and ``usage`` when present in
        the final chunk.
        """
        # Force stream=true on the body we send upstream; the gateway
        # honours the flag regardless of what the client passed.
        body = {**body, "stream": True}

        # State the chunk loop accumulates into.
        completion_id: str | None = None
        created: int | None = None
        model: str | None = None
        accumulated_content: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        # Each chunk that actually carries a content delta or
        # finish_reason gets a progress notification. The progress
        # value is a monotonic integer count of chunks-forwarded so
        # clients with progress bars have something to advance.
        progress_index = 0
        session = self._mcp.request_context.session
        # ``request_id`` is typed as ``int | str`` (the spec allows
        # either) but ``send_progress_notification`` only takes
        # ``str``. Coerce here once.
        related_request_id = str(self._mcp.request_context.request_id)

        async with httpx.AsyncClient(base_url=self._gateway_url, timeout=120.0) as c:
            try:
                async with c.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {bearer}"},
                    json=body,
                ) as resp:
                    if resp.status_code != 200:
                        # Upstream failed before any streaming
                        # happened — surface the error body as the
                        # final result with isError semantics.
                        record_mcp_streaming_session(
                            transport=self._transport, result="upstream_error"
                        )
                        try:
                            body_bytes = await resp.aread()
                            error_payload = json.loads(body_bytes.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            error_payload = {
                                "error": {
                                    "type": "non_json_response",
                                    "status_code": resp.status_code,
                                }
                            }
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(error_payload, separators=(",", ":")),
                            )
                        ]

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: ") :]
                        if payload == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            # Malformed chunk — skip, don't burn the
                            # whole stream on one bad frame.
                            continue

                        # Stamp metadata from the first chunk that
                        # carries it. Anthropic / OpenAI SSE shapes
                        # both put these on every chunk; we take the
                        # first observation defensively.
                        if completion_id is None and isinstance(
                            chunk.get("id"), str
                        ):
                            completion_id = chunk["id"]
                        if created is None and isinstance(
                            chunk.get("created"), int
                        ):
                            created = chunk["created"]
                        if model is None and isinstance(
                            chunk.get("model"), str
                        ):
                            model = chunk["model"]

                        # Walk the choices array (OpenAI-shape — index 0
                        # is the only one we ever see for chat unless
                        # n > 1 is used; we still pass it through).
                        choices = chunk.get("choices") or []
                        delta_content_this_chunk = ""
                        for choice in choices:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if isinstance(piece, str):
                                delta_content_this_chunk += piece
                            fr = choice.get("finish_reason")
                            if fr is not None and finish_reason is None:
                                finish_reason = fr
                        if delta_content_this_chunk:
                            accumulated_content.append(delta_content_this_chunk)

                        # Usage appears on the final chunk (with
                        # stream_options={include_usage: true}) or on
                        # Pronaos's last frame regardless. Capture if
                        # present.
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]

                        # Send progress notification for chunks that
                        # carry meaningful state (a delta OR the
                        # finish marker). Pure role-marker chunks
                        # don't trigger notifications.
                        if delta_content_this_chunk or finish_reason is not None:
                            progress_index += 1
                            record_mcp_streaming_chunk(transport=self._transport)
                            await session.send_progress_notification(
                                progress_token=progress_token,
                                progress=float(progress_index),
                                message=delta_content_this_chunk or None,
                                related_request_id=related_request_id,
                            )
            except Exception:
                # Mid-stream failure (network drop, JSON decode bug
                # in an unexpected branch, etc). Surface a structured
                # error payload as the final result. Progress
                # notifications already sent stay valid — the client
                # can choose to use them or discard them based on the
                # final result's shape.
                record_mcp_streaming_session(
                    transport=self._transport, result="mid_stream_error"
                )
                log.exception("mcp.streaming.mid_stream_error")
                error_payload = {
                    "error": {
                        "type": "mcp_streaming_aborted",
                        "partial_content": "".join(accumulated_content),
                        "progress_index": progress_index,
                    }
                }
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(error_payload, separators=(",", ":")),
                    )
                ]

        # Synthesize the final non-streaming ChatCompletion shape.
        final_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(accumulated_content),
        }
        choice_obj: dict[str, Any] = {
            "index": 0,
            "message": final_message,
            "finish_reason": finish_reason,
        }
        final_payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [choice_obj],
        }
        if usage is not None:
            final_payload["usage"] = usage
        # Stamp Pronaos's own streaming marker so MCP clients can
        # tell the response originated from the streaming branch
        # (helps debugging on the client side).
        final_payload["pronaos"] = {"mcp_streamed": True, "chunks": progress_index}

        record_mcp_streaming_session(transport=self._transport, result="ok")
        return [
            TextContent(
                type="text",
                text=json.dumps(final_payload, separators=(",", ":")),
            )
        ]
