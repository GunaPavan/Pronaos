"""Native MCP (Model Context Protocol) server adapter — Phase 48.

Exposes Pronaos's gateway functionality as MCP tools so MCP-speaking
clients (Claude Code, IDE integrations, Anthropic's own apps) target
the gateway directly with all of Pronaos's auth/quota/audit/routing/
caching applied automatically.

Authentication uses the same bearer-token API key mechanism as the REST
endpoints. The bearer token validated at SSE-handshake time is stashed
into a ContextVar that each tool-call handler reads when forwarding
the call through the gateway's existing chat/embeddings/rerank
pipeline.

This module is import-light by design: clients of Pronaos that don't
opt into MCP (PRONAOS_MCP_ENABLED=false) never touch this module's
code at runtime; the SDK import sits behind the lifespan flag.
"""

from __future__ import annotations

from pronaos.mcp.server import PronaosMcpServer, current_bearer_token

__all__ = ["PronaosMcpServer", "current_bearer_token"]


def _stdio_main() -> None:
    """Console-script entry point: ``pronaos-mcp-proxy``.

    Resolved at call-time so the SDK import (only needed for the
    stdio proxy) doesn't run on every ``import pronaos.mcp``.
    """
    from pronaos.mcp.stdio_proxy import main

    main()
