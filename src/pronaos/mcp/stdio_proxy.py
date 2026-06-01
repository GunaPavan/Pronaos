"""MCP stdio proxy — bridge Claude Code / Anthropic Desktop / IDE MCP
clients to a running Pronaos gateway.

Phase 50.

Why this exists
---------------
Phase 48 ships an SSE-based MCP server mounted at /v1/mcp/sse —
perfect for remote / containerised MCP clients. But the MCP clients
that matter most for solo dev workflows — Claude Code, Anthropic
Desktop, Cursor / Windsurf / Continue / etc. — all use the **stdio
transport**: they spawn the MCP server as a local subprocess and
exchange JSON-RPC frames over stdin/stdout.

This module is that subprocess. It:

1. Parses CLI args (`--gateway-url`, `--api-key` or `--api-key-file`).
2. Constructs the same ``PronaosMcpServer`` instance the SSE handler
   uses, pointed at the gateway URL.
3. Pre-loads the bearer token into the per-task ContextVar (one
   token per stdio session — no per-request auth dance).
4. Runs the MCP server over stdio via ``mcp.server.stdio.stdio_server``.

Registration with Claude Code
-----------------------------
Once Pronaos is installed::

    claude mcp add pronaos -- pronaos-mcp-proxy \
        --gateway-url http://127.0.0.1:8080 \
        --api-key-file ~/.config/pronaos/api-key

Claude Code spawns this subprocess on demand; every tool call
the model emits inside Claude Code is forwarded by this proxy
through the gateway's REST surface, picking up auth/quotas/
guardrails/cache/routing/audit automatically.

API-key handling
----------------
``--api-key`` accepts the literal token (convenient for one-off
demos). ``--api-key-file`` reads from a path (the recommended
pattern; matches how Anthropic itself recommends sensitive secrets
be passed to MCP subprocesses, since the registration command-line
can show up in process listings).

The token is the same Pronaos API key used by REST clients —
must carry the ``chat:write`` scope.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import NoReturn

from mcp.server.stdio import stdio_server

from pronaos.mcp.server import PronaosMcpServer, reset_bearer_token, set_bearer_token


def _resolve_bearer_token(args: argparse.Namespace) -> str:
    """Return the bearer token from --api-key, --api-key-file, or env var.

    Raises ``SystemExit(2)`` with an actionable message when none is
    supplied — better than starting up and failing the first tool call.
    """
    if args.api_key:
        return str(args.api_key).strip()
    if args.api_key_file:
        try:
            token = Path(args.api_key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SystemExit(
                f"pronaos-mcp-proxy: cannot read --api-key-file {args.api_key_file!r}: {e}"
            ) from e
        if not token:
            raise SystemExit(f"pronaos-mcp-proxy: --api-key-file {args.api_key_file!r} is empty")
        return token
    raise SystemExit(
        "pronaos-mcp-proxy: no bearer token supplied. Pass --api-key <token> "
        "or --api-key-file <path-to-file-containing-token>."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pronaos-mcp-proxy",
        description=(
            "MCP stdio proxy — bridges Claude Code / IDE MCP clients to a "
            "running Pronaos gateway. Spawned by the MCP client as a "
            "subprocess; speaks the MCP JSON-RPC protocol over stdin/stdout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8080",
        help=(
            "Base URL of the Pronaos gateway. Each MCP tools/call forwards "
            "via loopback HTTP to this URL. Default: http://127.0.0.1:8080."
        ),
    )
    bearer_group = parser.add_mutually_exclusive_group()
    bearer_group.add_argument(
        "--api-key",
        help=(
            "Pronaos API key (Bearer token) to authenticate every "
            "forwarded request with. NOT recommended on the command line "
            "(visible in process listings) — prefer --api-key-file."
        ),
    )
    bearer_group.add_argument(
        "--api-key-file",
        help=(
            "Path to a file whose first line is the Pronaos API key. "
            "Recommended over --api-key for security."
        ),
    )
    return parser


async def _serve(*, gateway_url: str, bearer_token: str) -> None:
    """Run the MCP server over stdio for the lifetime of the client
    subprocess connection. Bearer token is set in the ContextVar
    once and unset on shutdown."""
    server = PronaosMcpServer(gateway_url=gateway_url, transport="stdio")
    token_reset = set_bearer_token(bearer_token)
    try:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.mcp.create_initialization_options()
            await server.mcp.run(read_stream, write_stream, init_options)
    finally:
        reset_bearer_token(token_reset)


def main(argv: list[str] | None = None) -> NoReturn:
    """Entry point invoked via the ``pronaos-mcp-proxy`` console script."""
    args = _build_parser().parse_args(argv)
    bearer_token = _resolve_bearer_token(args)
    try:
        asyncio.run(_serve(gateway_url=args.gateway_url, bearer_token=bearer_token))
    except KeyboardInterrupt:
        # Clean exit when the MCP client (Claude Code etc.) closes the
        # subprocess via SIGINT. asyncio's runner raises this on Windows
        # when the parent terminates the pipe.
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
