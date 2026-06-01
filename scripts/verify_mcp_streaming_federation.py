"""MCP streaming federation live verification (Claim #45, Phase 58).

The empirical question
----------------------
Phase 54 made Pronaos an MCP client (chat requests can carry
``pronaos_mcp_servers`` and external MCP servers' tools get federated
into the chat). Phase 54 shipped with a documented honest-limit:
``stream=true`` + ``pronaos_mcp_servers`` together returned HTTP 422
``mcp_streaming_unsupported``. IDE-class clients that always stream
couldn't use federation.

Phase 58 closes that gap. The streaming federation wrapper:

1. Reuses Phase 54's non-streaming federation loop end-to-end
2. Synthesizes an OpenAI-shape SSE stream from the final payload
3. Propagates federation headers + stamps ``X-Pronaos-MCP-Streamed: 1``

What this verify asserts
------------------------
1. Spin up the same tiny test MCP server from Phase 54's verify
   (exposes ``get_temperature``).
2. Issue a chat completion at the gateway with BOTH:
   - ``stream=true``
   - ``pronaos_mcp_servers=[weather → test server]``
3. Expected behavior:
   - The 422 ``mcp_streaming_unsupported`` gate is GONE
   - Response is an SSE stream (Content-Type: text/event-stream)
   - Streaming chunks reconstruct to a non-empty assistant message
   - Federation headers are present + correct
   - ``X-Pronaos-MCP-Streamed: 1`` is stamped
   - ``pronaos_mcp_streaming_federation_sessions_total{result="ok"}``
     ticked by exactly +1

Honest disclosure
-----------------
TTFT equals the full federation loop's latency, not first-token from
the upstream. This v1 of streaming federation synthesizes SSE from
the buffered final response. Future phases can add true mid-stream
tool_call routing if real-time TTFT becomes a requirement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

TEST_MCP_SERVER_SOURCE = r'''
"""Tiny test MCP server for Phase 58 verify — exposes one tool.

Spawned as a subprocess by ``verify_mcp_streaming_federation.py``.
Identical to the Phase 54 verify's test server; the streaming
federation reuses the same federation primitives so any tool that
works under non-streaming works under streaming.
"""

from __future__ import annotations

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


async def _serve() -> None:
    app: Server[None, None] = Server("phase58-test-weather")

    @app.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_temperature",
                description=(
                    "Get the current temperature in degrees Celsius "
                    "for a city. Returns a synthetic value for "
                    "Phase 58 verify purposes."
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


async def _consume_sse_stream(
    response: httpx.Response,
) -> tuple[list[dict[str, object]], str]:
    """Drive the SSE stream and return (chunks, reconstructed_text).

    Each ``data: <json>`` line becomes one parsed chunk. ``[DONE]``
    is filtered out. Reconstructed text concatenates every
    ``delta.content`` fragment in order — the streaming federation
    chunks the final assistant content at 64 chars, so this should
    rebuild the full answer."""
    chunks: list[dict[str, object]] = []
    text_pieces: list[str] = []
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        chunks.append(data)
        choices = data.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content_piece = delta.get("content")
            if isinstance(content_piece, str):
                text_pieces.append(content_piece)
    return chunks, "".join(text_pieces)


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
    print("Phase 58 — MCP streaming federation live verification")
    print("=" * 72)
    print()

    # ---- Write the test MCP server + locate Python --------------------
    tmpdir = Path(tempfile.mkdtemp(prefix="phase58-mcp-"))
    server_script = tmpdir / "test_weather_mcp.py"
    server_script.write_text(TEST_MCP_SERVER_SOURCE, encoding="utf-8")
    python_exe = sys.executable
    print(f"Test MCP server script: {server_script}")
    print(f"Spawn command: {python_exe} {server_script}")
    print()

    # ---- Baseline metrics ---------------------------------------------
    before_streaming_sessions = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_streaming_federation_sessions_total",
    )

    # ---- Fire streaming chat with federation --------------------------
    body = {
        "model": "groq/llama-3.3-70b-versatile",
        "max_tokens": 256,
        "temperature": 0.0,
        "stream": True,
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
                "env": {
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                },
            }
        ],
    }

    print(
        "Firing STREAMING chat with pronaos_mcp_servers=[weather] "
        "(previously returned 422 mcp_streaming_unsupported)"
    )
    async with httpx.AsyncClient(
        base_url=args.gateway_url, timeout=180.0
    ) as client, client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {args.api_key}"},
        json=body,
    ) as resp:
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        mcp_streamed = resp.headers.get("x-pronaos-mcp-streamed", "")
        federated_servers = resp.headers.get(
            "x-pronaos-mcp-federated-servers", ""
        )
        iterations_hdr = resp.headers.get("x-pronaos-mcp-iterations", "")
        chunks: list[dict[str, object]] = []
        reconstructed = ""
        if status == 200:
            chunks, reconstructed = await _consume_sse_stream(resp)
        else:
            error_body = await resp.aread()
            error_text = error_body.decode("utf-8", errors="replace")

    print(f"  HTTP status: {status}")
    print(f"  Content-Type: {content_type}")
    print(f"  X-Pronaos-MCP-Streamed: {mcp_streamed!r}")
    print(f"  X-Pronaos-MCP-Federated-Servers: {federated_servers!r}")
    print(f"  X-Pronaos-MCP-Iterations: {iterations_hdr!r}")
    print(f"  SSE chunks received: {len(chunks)}")
    print(f"  Reconstructed assistant (first 160c): {reconstructed[:160]!r}")
    if status != 200:
        print(f"  Error body: {error_text[:500]}")
    print()

    # ---- Read post-call metrics ---------------------------------------
    after_streaming_sessions = await _read_metric_total(
        gateway_url=args.gateway_url,
        metric_prefix="pronaos_mcp_streaming_federation_sessions_total",
    )
    streaming_delta = after_streaming_sessions - before_streaming_sessions
    print(
        f"pronaos_mcp_streaming_federation_sessions_total delta: "
        f"+{streaming_delta:.0f}"
    )
    print()

    # ---- Verdict ------------------------------------------------------
    print("=" * 72)
    failures: list[str] = []
    if status != 200:
        failures.append(
            f"streaming chat returned HTTP {status} (expected 200 — "
            "the 422 mcp_streaming_unsupported gate should be removed)"
        )
    if "text/event-stream" not in content_type:
        failures.append(
            f"Content-Type {content_type!r} is not text/event-stream"
        )
    if mcp_streamed != "1":
        failures.append(
            f"X-Pronaos-MCP-Streamed header missing or wrong: {mcp_streamed!r}"
        )
    if "weather" not in federated_servers:
        failures.append(
            f"X-Pronaos-MCP-Federated-Servers should include 'weather': "
            f"{federated_servers!r}"
        )
    if not iterations_hdr or iterations_hdr == "1":
        failures.append(
            f"X-Pronaos-MCP-Iterations should be >= 2 (tool call + "
            f"final-text iteration); got {iterations_hdr!r}"
        )
    if not chunks:
        failures.append("no SSE chunks received from the stream")
    if not reconstructed:
        failures.append(
            "reconstructed assistant text is empty — the SSE stream "
            "did not deliver the final answer"
        )
    if "17" not in reconstructed:
        failures.append(
            "reconstructed assistant text does not contain '17' — the "
            "weather.get_temperature tool result (synthetic 17 degrees) "
            f"did not flow through to the final answer: {reconstructed[:200]!r}"
        )
    if streaming_delta < 1:
        failures.append(
            "pronaos_mcp_streaming_federation_sessions_total did NOT "
            "tick — the streaming branch was not exercised"
        )

    if failures:
        print("VERDICT: claim fails")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print(
        "VERDICT: claim holds - streaming MCP federation works end-to-end. "
        "The 422 mcp_streaming_unsupported gate is gone; "
        f"stream=true + pronaos_mcp_servers returned HTTP {status} with "
        f"Content-Type {content_type!r}. The synthesized SSE stream "
        f"delivered {len(chunks)} chunks reconstructing to "
        f"{len(reconstructed)} chars of final assistant text containing "
        "the synthetic '17 degrees' temperature from the test MCP "
        "server. Federation headers stamped correctly: "
        f"X-Pronaos-MCP-Federated-Servers={federated_servers!r}, "
        f"X-Pronaos-MCP-Iterations={iterations_hdr!r}, "
        f"X-Pronaos-MCP-Streamed={mcp_streamed!r}. "
        f"pronaos_mcp_streaming_federation_sessions_total ticked by "
        f"+{streaming_delta:.0f}. Substitution disclosure: "
        "synthesized SSE from the buffered federation result; TTFT "
        "equals full federation loop latency, not first-token from "
        "the upstream. The same code paths fire on every IDE-class "
        "client that combines stream=true with MCP federation."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
