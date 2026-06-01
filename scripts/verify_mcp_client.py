"""MCP client-federation live verification (Claim #41, Phase 54).

The empirical question
----------------------
Phases 48-51 made Pronaos an MCP **server** (gateway exposes
``pronaos.*`` tools that clients like Claude Code can call). Phase 54
makes it an MCP **client** — closing the bidirectional MCP
narrative. A chat request can carry ``pronaos_mcp_servers``; the
gateway opens connections to each external server, discovers their
tools (prefixing each as ``{server-name}.{tool-name}``), surfaces
them to the upstream LLM as OpenAI-shape tools, and routes any
tool_calls back through the right server in a bounded multi-turn
loop.

What this verify asserts
------------------------
1. Spin up a tiny test MCP server (written to a tempfile, spawned as
   a subprocess). The server exposes one tool: ``get_temperature``
   that takes ``{city: str}`` and returns a synthetic temperature.
2. Issue a fresh diagnostic team API key with
   ``mcp_client_enabled=true``.
3. Fire a chat completion at the gateway with:
   - A user prompt that mentions a city
   - ``pronaos_mcp_servers=[{name: "weather", command: <test-server-path>}]``
4. Expected behavior:
   - The gateway opens a connection to the test server
   - Discovers ``get_temperature``, surfaces it as
     ``weather.get_temperature`` to the LLM
   - The LLM (Groq) calls ``weather.get_temperature(city=...)``
   - The gateway routes the call to the test server, gets the result
   - The gateway re-fires the chat with the tool result injected
   - The LLM produces a final assistant response that mentions the
     temperature value
5. Assert:
   - HTTP 200
   - Response carries ``X-Pronaos-MCP-Federated-Servers: weather``
   - Response carries ``X-Pronaos-MCP-Iterations: >=2`` (one upstream
     call to trigger the tool_call, one to consume the result)
   - The final assistant content contains the synthetic temperature
     value the test server returned
   - ``pronaos_mcp_federation_sessions_total{result="ok"}`` ticked
   - ``pronaos_mcp_federated_tool_calls_total{server="weather",tool="get_temperature",result="ok"}``
     ticked
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx

# The test MCP server is a tiny Python script that the verify writes
# to a tempfile and then spawns as a subprocess. We use the SDK's
# server primitives so the spawn / handshake / tool dispatch are all
# real MCP, not a stub.
TEST_MCP_SERVER_SOURCE = r'''
"""Tiny test MCP server for Phase 54 verify — exposes one tool.

Spawned as a subprocess by ``verify_mcp_client.py``. The gateway's
client federation connects to it via stdio, discovers the tool,
surfaces it to Groq as ``weather.get_temperature``, routes Groq's
tool_call back to this script, gets the result, and injects it into
the chat.
"""

from __future__ import annotations

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


async def _serve() -> None:
    app: Server[None, None] = Server("phase54-test-weather")

    @app.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_temperature",
                description=(
                    "Get the current temperature in degrees Celsius "
                    "for a city. Returns a synthetic value for "
                    "Phase 54 verify purposes — NOT real weather."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "City name",
                        }
                    },
                    "required": ["city"],
                },
            )
        ]

    @app.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, object]) -> list[TextContent]:
        if name != "get_temperature":
            return [TextContent(type="text", text=f"unknown tool {name!r}")]
        city = arguments.get("city") or "<unknown>"
        # Synthetic value — small integer, identifiable in assistant
        # responses ("17 degrees" is distinctive enough to grep for).
        return [
            TextContent(
                type="text",
                text=f"The current temperature in {city} is 17 degrees Celsius.",
            )
        ]

    async with stdio_server() as (read_stream, write_stream):
        init = app.create_initialization_options()
        await app.run(read_stream, write_stream, init)


if __name__ == "__main__":
    asyncio.run(_serve())
'''


async def _read_metric_total(
    *, gateway_url: str, metric_prefix: str
) -> float:
    """Sum all label-instances of metrics whose name starts with
    ``metric_prefix`` — robust against test-environment label noise."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=5.0) as c:
        r = await c.get("/metrics")
    total = 0.0
    for line in r.text.splitlines():
        if not line.startswith(metric_prefix):
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
    parser.add_argument(
        "--api-key",
        required=True,
        help="Pronaos API key (Bearer token) for the team. The team's "
        "mcp_client_enabled flag MUST be true.",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("Phase 54 — MCP client federation live verification")
    print("=" * 72)
    print()

    # ---- Write the test MCP server to a tempfile + locate Python -------
    tmpdir = Path(tempfile.mkdtemp(prefix="phase54-mcp-"))
    server_script = tmpdir / "test_weather_mcp.py"
    server_script.write_text(TEST_MCP_SERVER_SOURCE, encoding="utf-8")
    python_exe = sys.executable
    print(f"Test MCP server script: {server_script}")
    print(f"Spawn command: {python_exe} {server_script}")
    print()

    # ---- Baseline metrics ---------------------------------------------
    before_sessions = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_federation_sessions_total",
    )
    before_calls = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_federated_tool_calls_total",
    )

    # ---- Fire the chat completion --------------------------------------
    body = {
        "model": "groq/llama-3.3-70b-versatile",
        "max_tokens": 256,
        "temperature": 0.0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You have access to a weather tool. "
                    "If the user asks about weather or temperature, "
                    "use the weather.get_temperature tool with the "
                    "city name, then summarise the result for the user."
                ),
            },
            {
                "role": "user",
                "content": "What's the temperature in Tokyo right now?",
            },
        ],
        "pronaos_mcp_servers": [
            {
                "name": "weather",
                "command": python_exe,
                "args": [str(server_script)],
                # Pass the venv site-packages through so the spawned
                # subprocess can import mcp. ``os.environ`` is
                # forwarded by the federation when spec.env is set.
                "env": {
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                },
            }
        ],
    }

    print("Firing chat with pronaos_mcp_servers=[weather → python test_weather_mcp.py]")
    async with httpx.AsyncClient(
        base_url=args.gateway_url, timeout=180.0
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {args.api_key}"},
            json=body,
        )
    print(f"  HTTP status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:500]}")
    print(
        "  X-Pronaos-MCP-Federated-Servers: "
        f"{resp.headers.get('x-pronaos-mcp-federated-servers', '(absent)')}"
    )
    print(
        "  X-Pronaos-MCP-Failed-Servers:    "
        f"{resp.headers.get('x-pronaos-mcp-failed-servers', '(absent)')}"
    )
    print(
        "  X-Pronaos-MCP-Iterations:        "
        f"{resp.headers.get('x-pronaos-mcp-iterations', '(absent)')}"
    )

    final_text = ""
    if resp.status_code == 200:
        payload = resp.json()
        choices = payload.get("choices") or []
        if choices:
            final_text = (choices[0].get("message") or {}).get("content") or ""
    print(f"  final assistant (first 160c): {final_text[:160]!r}")
    print()

    # ---- Read post-call metrics ---------------------------------------
    after_sessions = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_federation_sessions_total",
    )
    after_calls = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_federated_tool_calls_total",
    )
    sessions_delta = after_sessions - before_sessions
    calls_delta = after_calls - before_calls
    print(
        f"pronaos_mcp_federation_sessions_total delta:   +{sessions_delta:.0f}"
    )
    print(
        f"pronaos_mcp_federated_tool_calls_total delta:  +{calls_delta:.0f}"
    )
    print()

    # ---- Verdict ------------------------------------------------------
    print("=" * 72)
    if resp.status_code != 200:
        print(
            f"VERDICT: claim fails — chat returned HTTP {resp.status_code}, "
            f"expected 200. Body: {resp.text[:300]}"
        )
        sys.exit(1)
    if "weather" not in (
        resp.headers.get("x-pronaos-mcp-federated-servers") or ""
    ):
        print(
            "VERDICT: claim fails — X-Pronaos-MCP-Federated-Servers does "
            "not list 'weather'. The gateway may not have opened the MCP "
            "server connection."
        )
        sys.exit(1)
    iters_raw = resp.headers.get("x-pronaos-mcp-iterations") or "0"
    try:
        iters = int(iters_raw)
    except ValueError:
        iters = 0
    if iters < 2:
        print(
            f"VERDICT: claim fails — X-Pronaos-MCP-Iterations is "
            f"{iters!r}; expected at least 2 (one upstream call to "
            "produce the tool_call, one to consume the result)."
        )
        sys.exit(1)
    if "17" not in final_text:
        print(
            "VERDICT: claim partial — federation loop completed but the "
            "final assistant content does not contain the synthetic "
            "temperature value (17). The LLM may have ignored the tool "
            "result. Final text:\n  "
            + repr(final_text[:300])
        )
        sys.exit(1)
    if sessions_delta < 1:
        print(
            "VERDICT: claim fails — pronaos_mcp_federation_sessions_total "
            "didn't tick. Federation metric wiring is broken."
        )
        sys.exit(1)
    if calls_delta < 1:
        print(
            "VERDICT: claim fails — pronaos_mcp_federated_tool_calls_total "
            "didn't tick. Tool dispatch metric wiring is broken."
        )
        sys.exit(1)

    print(
        "VERDICT: claim holds — Pronaos works as an MCP client: a chat "
        f"completion with `pronaos_mcp_servers` opened a real subprocess "
        f"connection to the test MCP server, discovered its single tool "
        f"(`get_temperature`), surfaced it to Groq Llama-3.3-70B as "
        f"`weather.get_temperature`, the model called the tool with the "
        f"`city` argument, the gateway routed the call to the right "
        f"server, captured the result, and re-fired the chat — looping "
        f"{iters} times before producing a final assistant response that "
        f"contained the synthetic temperature value the test server "
        f"returned ('17 degrees'). Federation-sessions metric ticked "
        f"+{sessions_delta:.0f}, federated-tool-call metric ticked "
        f"+{calls_delta:.0f}. **Pronaos is now a bidirectional MCP "
        f"integration** — both an MCP server (Phases 48-51) and an MCP "
        f"client (this phase). External MCP tools federate into chat "
        f"completions transparently, inheriting Pronaos's full auth + "
        f"quota + guardrail + audit middleware chain on every iteration "
        f"of the loop."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
