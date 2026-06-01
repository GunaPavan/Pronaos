"""Tests for the MCP client federation primitives (Phase 54).

Surfaces under test:

1. ``McpServerSpec.from_dict`` validation — rejects malformed input
   with useful errors; accepts clean input.
2. ``_serialise_call_tool_result`` — flattens CallToolResult into the
   dict shape the chat handler injects as a ``tool`` role message.
3. ``McpFederation`` lifecycle — open multiple servers, route
   tool_calls by prefix, fail-isolated when one server is down.
4. ``is_federated_tool_name`` predicate — false for non-federated
   tools (the chat handler uses this to decide whether to dispatch).
5. ``open_federation`` parses raw spec dicts and yields a federation.

The chat-handler integration is exercised by the live verify script
(``scripts/verify_mcp_client.py``); reproducing the full FastAPI +
loopback HTTP loop here would require spinning up a real server
side-by-side with the MCP server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from pronaos.mcp.client_federation import (
    McpFederation,
    McpServerSpec,
    _serialise_call_tool_result,
    open_federation,
)


class TestMcpServerSpecValidation:
    def test_accepts_clean_input(self) -> None:
        spec = McpServerSpec.from_dict(
            {
                "name": "weather",
                "command": "weather-mcp",
                "args": ["--region", "us"],
                "env": {"KEY": "val"},
            }
        )
        assert spec.name == "weather"
        assert spec.command == "weather-mcp"
        assert spec.args == ["--region", "us"]
        assert spec.env == {"KEY": "val"}

    def test_rejects_missing_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            McpServerSpec.from_dict({"command": "x"})

    def test_rejects_missing_command(self) -> None:
        with pytest.raises(ValueError, match="command"):
            McpServerSpec.from_dict({"name": "w"})

    def test_rejects_dotted_name(self) -> None:
        # Dotted names would collide with the tool-name prefixing scheme
        # ({server}.{tool}) — reject upfront.
        with pytest.raises(ValueError, match="A-Za-z0-9"):
            McpServerSpec.from_dict({"name": "my.weather", "command": "x"})

    def test_rejects_non_string_args(self) -> None:
        with pytest.raises(ValueError, match="args"):
            McpServerSpec.from_dict({"name": "x", "command": "y", "args": ["ok", 7]})

    def test_rejects_non_string_env_values(self) -> None:
        with pytest.raises(ValueError, match="env"):
            McpServerSpec.from_dict({"name": "x", "command": "y", "env": {"K": 1}})

    def test_args_defaults_to_empty_list(self) -> None:
        spec = McpServerSpec.from_dict({"name": "x", "command": "y"})
        assert spec.args == []
        assert spec.env == {}


class TestSerialiseCallToolResult:
    def test_text_content_concatenated(self) -> None:
        result = CallToolResult(
            content=[
                TextContent(type="text", text="Hello "),
                TextContent(type="text", text="world!"),
            ],
            isError=False,
        )
        out = _serialise_call_tool_result(result)
        assert out["is_error"] is False
        assert out["content"] == "Hello \nworld!"
        assert len(out["raw_content"]) == 2

    def test_is_error_propagates(self) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text="upstream blew up")],
            isError=True,
        )
        out = _serialise_call_tool_result(result)
        assert out["is_error"] is True
        assert "upstream blew up" in out["content"]

    def test_empty_content_serialises_as_empty_string_or_json(self) -> None:
        result = CallToolResult(content=[], isError=False)
        out = _serialise_call_tool_result(result)
        # No text parts → either empty content string or JSON-dump of empty raw_content
        assert out["is_error"] is False
        assert out["raw_content"] == []


class TestDuplicateNames:
    @pytest.mark.asyncio
    async def test_duplicate_server_names_rejected(self) -> None:
        specs = [
            McpServerSpec(name="dup", command="a"),
            McpServerSpec(name="dup", command="b"),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            async with McpFederation(specs) as _:
                pass


class TestFailedOpenIsolation:
    @pytest.mark.asyncio
    async def test_unreachable_server_recorded_in_failed(self, tmp_path: Path) -> None:
        """When a spec's command can't be spawned (binary doesn't
        exist), the federation should record it in ``failed_server_names``
        but the federation itself opens cleanly with zero working
        servers."""
        nonexistent = tmp_path / "nonexistent-mcp-binary"
        specs = [
            McpServerSpec(
                name="broken",
                command=str(nonexistent),
                args=[],
            )
        ]
        async with McpFederation(specs) as fed:
            assert fed.opened_server_names == []
            assert "broken" in fed.failed_server_names
            # Tools list is empty because no server is open.
            assert fed.federated_tool_schemas() == []
            # Routing a tool_call on the failed server returns an error
            # result instead of raising — agent loops recover from this.
            result = await fed.call_tool("broken.something", {})
            assert result["is_error"] is True
            assert "failed to open" in result["content"]


class TestIsFederatedToolName:
    @pytest.mark.asyncio
    async def test_unknown_name_returns_false(self, tmp_path: Path) -> None:
        """``is_federated_tool_name`` is the gate the chat handler uses
        to decide 'should I dispatch this tool_call to a federation
        server, or is it a client-supplied tool?' Must be conservative."""
        # Empty federation (no servers opened) — every name is non-federated.
        async with McpFederation([]) as fed:
            assert fed.is_federated_tool_name("not_prefixed") is False
            assert fed.is_federated_tool_name("missing.tool") is False
            # Even an empty string doesn't match.
            assert fed.is_federated_tool_name("") is False


class TestOpenFederationConvenience:
    @pytest.mark.asyncio
    async def test_open_federation_parses_specs(self, tmp_path: Path) -> None:
        """``open_federation`` validates + opens in one step. Bad spec
        → ValueError at parse time."""
        raw = [{"name": "ok", "command": str(tmp_path / "nope")}]
        async with open_federation(raw) as fed:
            assert "ok" in fed.failed_server_names  # binary doesn't exist
            assert fed.opened_server_names == []

    @pytest.mark.asyncio
    async def test_open_federation_rejects_bad_specs(self) -> None:
        with pytest.raises(ValueError, match="name"):
            async with open_federation(
                [{"command": "missing-name"}]  # no name
            ) as _:
                pass


class TestCallToolUnknownPrefix:
    @pytest.mark.asyncio
    async def test_unknown_server_returns_error_result(self) -> None:
        async with McpFederation([]) as fed:
            result = await fed.call_tool("ghost.tool", {})
            assert result["is_error"] is True
            assert "not registered" in result["content"]

    @pytest.mark.asyncio
    async def test_unprefixed_name_returns_error_result(self) -> None:
        async with McpFederation([]) as fed:
            result = await fed.call_tool("plain_tool_name", {})
            assert result["is_error"] is True
            assert "unknown federated tool" in result["content"]
