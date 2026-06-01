"""MCP stdio transport live verification (Claim #37, Phase 50).

The empirical question
----------------------
Phase 48 made Pronaos a real MCP server over SSE. Phase 50 closes
the loop the way Claude Code / Anthropic Desktop / Cursor / Windsurf
actually integrate: by spawning a local subprocess that speaks the
MCP JSON-RPC protocol over stdin/stdout.

This script uses the **official Anthropic-maintained MCP Python SDK
client** to spawn `pronaos-mcp-proxy` exactly the way Claude Code
would — same `StdioServerParameters` shape, same JSON-RPC framing.
If this passes, every stdio-MCP client out there (Claude Code,
Anthropic Desktop, IDE plugins) can target Pronaos.

What it asserts
---------------
1. The console script is registered and the SDK can spawn it.
2. The MCP `initialize` handshake completes; server name is
   ``pronaos``.
3. `tools/list` advertises ``pronaos.chat``, ``pronaos.embed``,
   ``pronaos.rerank`` — same shapes as the SSE transport surfaces.
4. `tools/call` for ``pronaos.chat`` reaches the running gateway
   (recorded by a ``pronaos_routing_decisions_total`` metric tick)
   AND the returned CallToolResult carries non-empty assistant
   content from Groq.

The chain under test:

    test → mcp.client.stdio (spawns subprocess)
         → pronaos-mcp-proxy (this is the new code)
         → PronaosMcpServer (Phase 48 adapter)
         → loopback HTTP /v1/chat/completions
         → full middleware chain
         → Groq
         → back through every layer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def _read_routing_metric_count(*, gateway_url: str) -> float:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=5.0) as c:
        r = await c.get("/metrics")
    total = 0.0
    for line in r.text.splitlines():
        if not line.startswith("pronaos_routing_decisions_total{"):
            continue
        _, _, value = line.rpartition(" ")
        try:
            total += float(value)
        except ValueError:
            continue
    return total


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument(
        "--proxy-command",
        default="pronaos-mcp-proxy",
        help=(
            "Console script for the stdio proxy. Default 'pronaos-mcp-proxy' "
            "(resolved via PATH). Override with a full path if the venv's bin "
            "directory isn't on PATH."
        ),
    )
    args = parser.parse_args()

    print("=" * 72)
    print("Phase 50 — MCP stdio transport live verification")
    print("=" * 72)
    print()

    # Resolve the proxy command — fail clearly if it's not installed.
    resolved = shutil.which(args.proxy_command)
    if resolved is None:
        print(
            f"FAIL: --proxy-command {args.proxy_command!r} not found on PATH. "
            f"Did you `pip install -e .` after Phase 50 landed? "
            f"(The console-script entry is registered in pyproject.toml.)"
        )
        sys.exit(2)
    print(f"Spawning stdio proxy: {resolved}")
    print(f"  → gateway: {args.gateway_url}")
    print(f"  → api-key: pn_..._{args.api_key[-6:]}")

    before_total = await _read_routing_metric_count(gateway_url=args.gateway_url)

    # Spawn the proxy as a subprocess via the official SDK — this is
    # the EXACT path Claude Code uses (it constructs the same
    # StdioServerParameters and calls stdio_client()).
    server_params = StdioServerParameters(
        command=resolved,
        args=[
            "--gateway-url",
            args.gateway_url,
            "--api-key",
            args.api_key,
        ],
    )

    tools_ok = False
    chat_ok = False
    routed_payload_summary = ""
    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        server_name = init_result.serverInfo.name
        print(f"  initialize OK — server name: {server_name!r}")

        tools_result = await session.list_tools()
        names = {t.name for t in tools_result.tools}
        print(f"  tools/list returned: {sorted(names)}")
        expected = {"pronaos.chat", "pronaos.embed", "pronaos.rerank"}
        tools_ok = expected.issubset(names)

        print()
        print("Calling pronaos.chat via stdio...")
        call_result = await session.call_tool(
            "pronaos.chat",
            arguments={
                # ``auto`` exercises the routing path so the metric we
                # assert on (pronaos_routing_decisions_total) actually
                # ticks. A concrete fqmn would skip routing entirely
                # and produce a "claim fails" on a working setup.
                "model": "auto",
                "messages": [{"role": "user", "content": "say hi in one word"}],
                "max_tokens": 8,
                "temperature": 0.0,
            },
        )
        is_error = bool(call_result.isError)
        first = call_result.content[0] if call_result.content else None
        text = getattr(first, "text", "") if first else ""
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"_raw": text[:200]}
        print(f"  CallToolResult.isError: {is_error}")
        print(f"  payload keys: {sorted(payload.keys())}")
        choices = payload.get("choices") or []
        if choices:
            choice = choices[0]
            content_field = choice.get("message", {}).get("content")
            routed_payload_summary = repr(content_field)[:80]
            print(f"  assistant content: {routed_payload_summary}")
            if content_field:
                chat_ok = True
        elif "detail" in payload:
            print(f"  gateway detail: {payload['detail']}")

    after_total = await _read_routing_metric_count(gateway_url=args.gateway_url)
    metric_delta = after_total - before_total
    print()
    print(f"pronaos_routing_decisions_total delta: +{metric_delta:.0f}")

    # ---- Verdict --------------------------------------------------------
    print()
    print("=" * 72)
    if server_name != "pronaos":
        print(
            f"VERDICT: claim fails — MCP initialize returned server name "
            f"{server_name!r}; expected 'pronaos'."
        )
        sys.exit(1)
    if not tools_ok:
        print(
            "VERDICT: claim fails — tools/list missing one of "
            "{pronaos.chat, pronaos.embed, pronaos.rerank}. "
            f"Got: {sorted(names)}."
        )
        sys.exit(1)
    if metric_delta < 1:
        print(
            "VERDICT: claim fails — pronaos.chat tools/call returned without "
            "ticking pronaos_routing_decisions_total. The stdio proxy may "
            "not be reaching the gateway."
        )
        sys.exit(1)
    if not chat_ok:
        print(
            "VERDICT: claim partial — stdio chain reached the gateway "
            f"(metric +{metric_delta:.0f}) but the upstream returned no "
            f"assistant content. Last payload: {routed_payload_summary}. "
            "Check the Groq key validity."
        )
        sys.exit(1)
    print(
        "VERDICT: claim holds — Pronaos works as an MCP server over the "
        "stdio transport. The official Anthropic-maintained MCP Python SDK "
        "client spawned `pronaos-mcp-proxy` as a subprocess (the exact "
        "shape Claude Code / Anthropic Desktop / IDE MCP clients use), "
        "completed the MCP `initialize` handshake (server name 'pronaos'), "
        "discovered the three pronaos.* tools, and a `tools/call` for "
        f"pronaos.chat reached the running gateway (routing metric +{metric_delta:.0f}) "
        f"and returned real assistant content from Groq ({routed_payload_summary}). "
        "Any stdio-MCP client can now register Pronaos with one command:\n\n"
        "    claude mcp add pronaos -- pronaos-mcp-proxy \\\n"
        "        --gateway-url http://127.0.0.1:8080 \\\n"
        "        --api-key-file ~/.config/pronaos/api-key"
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
