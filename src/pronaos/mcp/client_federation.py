"""MCP client-side federation (Phase 54).

Closes the bidirectional MCP narrative. Phases 48-51 made Pronaos an
MCP **server** (gateway exposes ``pronaos.*`` tools to clients).
Phase 54 makes it an MCP **client**: a chat request can reference
external MCP servers, the gateway federates their tools into the
chat completion, dispatches tool_calls back to the right server,
and loops until the LLM stops calling tools.

v1 scope
--------
- **stdio transport only.** SSE / streamable HTTP are follow-ups.
- **Non-streaming chat only.** Streaming with mid-tool-call buffering
  is a follow-up.
- **Spawn per request.** Each chat completion opens its own
  subprocess connections and closes them on completion. No persistent
  pool yet — keeps lifecycle simple; pooling is a follow-up.
- **Tool namespace prefixing.** Every discovered tool's name is
  rewritten to ``{server-name}.{tool-name}`` before being surfaced
  to the LLM. Avoids collisions and makes routing trivial: peel
  the prefix to find the server.
- **Per-team enable flag.** Teams without ``mcp_client_enabled`` cannot
  reference MCP servers in chat completions; this is a per-team
  policy because stdio MCP servers spawn subprocesses on the gateway
  host (security-sensitive).

Architecture
------------
::

    chat request body:
      pronaos_mcp_servers: [{name: "weather", command: "weather-mcp", args: [...], env: {}}]
                │
                ▼
    McpFederation context manager
        │ spawn subprocess via mcp.client.stdio.stdio_client(...)
        │ open ClientSession, initialize, list_tools
        │ register each tool as "{server-name}.{tool-name}"
        │
        ▼
    federated_tool_schemas() → OpenAI-shape tool defs merged with body.tools
        │
        ▼
    chat handler upstream call (with augmented tools array)
        │
        ▼
    LLM response carries tool_calls?
        │
        ├─ no → return final response
        └─ yes → for each tool_call:
            │     parse "{prefix}.{tool}", look up server, dispatch via call_tool
            │     append synthetic `tool` role message with the CallToolResult
            ▼
        loop (up to max_iterations)
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pronaos.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Spec + dataclasses                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One external MCP server's connection spec.

    ``name`` is the operator-facing label; the gateway uses it as the
    tool-name prefix (``{name}.{tool}``). Must be ``[a-z0-9_-]+`` so
    OpenAI-compat clients see a sane tool name.

    ``command`` + ``args`` + ``env`` define the subprocess to spawn —
    same shape as ``mcp.client.stdio.StdioServerParameters``.
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpServerSpec:
        """Validate + construct from the chat request body shape.

        Raises ``ValueError`` on malformed input; the chat handler
        catches and re-raises as a 422 with detail.
        """
        name = data.get("name")
        command = data.get("command")
        if not isinstance(name, str) or not name:
            raise ValueError("mcp_servers[*].name must be a non-empty string")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"mcp_servers[*].name must be [A-Za-z0-9_-]+; got {name!r}"
            )
        if not isinstance(command, str) or not command:
            raise ValueError("mcp_servers[*].command must be a non-empty string")
        args_raw = data.get("args") or []
        if not isinstance(args_raw, list) or any(
            not isinstance(a, str) for a in args_raw
        ):
            raise ValueError("mcp_servers[*].args must be a list of strings")
        env_raw = data.get("env") or {}
        if not isinstance(env_raw, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in env_raw.items()
        ):
            raise ValueError("mcp_servers[*].env must be {str: str}")
        return cls(name=name, command=command, args=list(args_raw), env=dict(env_raw))


@dataclass(slots=True)
class FederatedTool:
    """One tool discovered from an MCP server, namespace-prefixed."""

    server_name: str
    original_name: str
    prefixed_name: str  # f"{server_name}.{original_name}"
    description: str
    input_schema: dict[str, Any]


class _SessionHandle:
    """Internal wrapper bundling a stdio client + ClientSession + tools.

    The two ``async with`` context managers from the SDK
    (``stdio_client`` and ``ClientSession``) must be entered + exited
    in nested order. We hold both via ``AsyncExitStack`` so the
    federation can manage many sessions cleanly.
    """

    __slots__ = ("session", "spec", "tools")

    def __init__(
        self,
        *,
        session: ClientSession,
        spec: McpServerSpec,
        tools: list[FederatedTool],
    ) -> None:
        self.session = session
        self.spec = spec
        self.tools = tools


# --------------------------------------------------------------------------- #
# Federation context manager                                                  #
# --------------------------------------------------------------------------- #


class McpFederation:
    """Open + manage a pool of MCP-client sessions for one chat request.

    Async context-manager: spawns all servers in parallel on ``__aenter__``,
    closes them all on ``__aexit__`` (cleanly even on partial failure).

    Usage::

        async with McpFederation(specs) as fed:
            tools = fed.federated_tool_schemas()
            # ... pass to chat completion ...
            result = await fed.call_tool("weather.get_forecast", {"city": "Tokyo"})
    """

    def __init__(self, specs: list[McpServerSpec]) -> None:
        # Reject duplicate server names upfront — routing depends on
        # the prefix being unique.
        names = [s.name for s in specs]
        if len(names) != len(set(names)):
            raise ValueError(
                "duplicate server names in mcp_servers; each spec.name must "
                "be unique within one request"
            )
        self._specs = specs
        self._stack = contextlib.AsyncExitStack()
        self._handles: dict[str, _SessionHandle] = {}
        # ``_failed`` records servers we couldn't open — they're omitted
        # from federation but don't fail the whole chat. The chat handler
        # decides whether to surface them via response headers.
        self._failed: dict[str, str] = {}

    async def __aenter__(self) -> McpFederation:
        await self._stack.__aenter__()
        # Open servers SEQUENTIALLY. The SDK's ``stdio_client`` uses
        # anyio task groups internally; opening multiple stdio_clients
        # concurrently via ``asyncio.gather`` parks each session's
        # task group in a sub-task, and the AsyncExitStack later
        # tries to close them all from the outer task — anyio
        # rejects the cross-task close with "Attempted to exit cancel
        # scope in a different task than it was entered in". Sequential
        # open keeps everything anchored in the caller's task, at the
        # cost of N*spawn time (typical N=1-3, so cost is bounded).
        for spec in self._specs:
            await self._open_one(spec)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._stack.__aexit__(exc_type, exc_val, exc_tb)

    async def _open_one(self, spec: McpServerSpec) -> None:
        """Open one MCP server connection. Failures degrade gracefully —
        the server is skipped but the rest of the federation continues."""
        try:
            params = StdioServerParameters(
                command=spec.command,
                args=spec.args,
                env={**os.environ, **spec.env} if spec.env else None,
            )
            read_stream, write_stream = await self._stack.enter_async_context(
                stdio_client(params)
            )
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            tools_result = await session.list_tools()
            tools = [
                FederatedTool(
                    server_name=spec.name,
                    original_name=t.name,
                    prefixed_name=f"{spec.name}.{t.name}",
                    description=t.description or "",
                    input_schema=dict(t.inputSchema or {}),
                )
                for t in tools_result.tools
            ]
            self._handles[spec.name] = _SessionHandle(
                session=session, spec=spec, tools=tools
            )
            log.info(
                "mcp.client.federation.opened",
                extra={
                    "server_name": spec.name,
                    "command": spec.command,
                    "tool_count": len(tools),
                },
            )
        except Exception as e:
            # Swallow the failure: federation continues with the remaining
            # servers. The chat handler decides whether to surface this
            # via headers.
            self._failed[spec.name] = type(e).__name__ + ": " + str(e)[:200]
            log.warning(
                "mcp.client.federation.open_failed",
                extra={"server_name": spec.name, "error": self._failed[spec.name]},
            )

    # ---- Public API ----------------------------------------------------------

    def federated_tool_schemas(self) -> list[dict[str, Any]]:
        """Return all federated tools in OpenAI-shape (the wire shape
        Pronaos's chat handler already expects for the ``tools``
        request field).

        Each tool's name is the prefixed form (``{server-name}.{tool-name}``).
        Schemas are passed through verbatim from the MCP server's
        ``tools/list`` output.
        """
        out: list[dict[str, Any]] = []
        for handle in self._handles.values():
            for tool in handle.tools:
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.prefixed_name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                )
        return out

    def is_federated_tool_name(self, name: str) -> bool:
        """``name`` matches a federated tool iff ``{server}.{tool}``
        resolves to an open session + a discovered tool name."""
        if "." not in name:
            return False
        server, _, tool = name.partition(".")
        handle = self._handles.get(server)
        if handle is None:
            return False
        return any(t.original_name == tool for t in handle.tools)

    async def call_tool(
        self, prefixed_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch a federated tool_call and return a serialisable result.

        Returns a dict shaped for the chat handler to inject as a
        ``tool`` role message::

            {"is_error": bool, "content": str, "raw_content": [...]}

        Errors flow through as ``is_error=True`` with the error text in
        ``content`` rather than raising — agent loops can recover from
        tool errors gracefully.
        """
        if "." not in prefixed_name:
            return {
                "is_error": True,
                "content": f"unknown federated tool: {prefixed_name!r}",
            }
        server, _, original = prefixed_name.partition(".")
        handle = self._handles.get(server)
        if handle is None:
            failed_reason = self._failed.get(server)
            if failed_reason:
                return {
                    "is_error": True,
                    "content": (
                        f"federated server {server!r} failed to open: "
                        f"{failed_reason}"
                    ),
                }
            return {
                "is_error": True,
                "content": f"federated server {server!r} not registered",
            }
        try:
            result = await handle.session.call_tool(original, arguments)
        except Exception as e:
            return {
                "is_error": True,
                "content": f"federated tool {prefixed_name!r} raised: {e}",
            }
        return _serialise_call_tool_result(result)

    @property
    def opened_server_names(self) -> list[str]:
        return list(self._handles.keys())

    @property
    def failed_server_names(self) -> dict[str, str]:
        return dict(self._failed)


# --------------------------------------------------------------------------- #
# CallToolResult → JSON-serialisable dict                                     #
# --------------------------------------------------------------------------- #


def _serialise_call_tool_result(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """Flatten an MCP CallToolResult into the dict the chat handler
    will use to build a ``tool`` role message.

    MCP returns content as a list of typed parts (TextContent /
    ImageContent / EmbeddedResource). For OpenAI-shape tool result
    messages we need a single string ``content`` field; we
    concatenate text parts and stash the raw list under ``raw_content``
    so callers that want richer access can reach for it.
    """
    text_parts: list[str] = []
    raw: list[dict[str, Any]] = []
    for part in result.content or []:
        # ``TextContent``: most common
        text = getattr(part, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
        # Capture the raw shape (model_dump if pydantic, dict-like otherwise)
        if hasattr(part, "model_dump"):
            raw.append(part.model_dump(mode="json"))
        else:
            raw.append({"_type": type(part).__name__})
    return {
        "is_error": bool(result.isError),
        "content": "\n".join(text_parts) if text_parts else json.dumps(raw),
        "raw_content": raw,
    }


# --------------------------------------------------------------------------- #
# Convenience: parse + open in one shot                                       #
# --------------------------------------------------------------------------- #


@contextlib.asynccontextmanager
async def open_federation(
    raw_specs: list[dict[str, Any]],
) -> AsyncIterator[McpFederation]:
    """Parse raw spec dicts (from request body) into ``McpServerSpec``
    instances and yield an open federation. Raises ``ValueError`` on
    malformed spec input (the chat handler catches and 422s)."""
    specs = [McpServerSpec.from_dict(s) for s in raw_specs]
    async with McpFederation(specs) as fed:
        yield fed
