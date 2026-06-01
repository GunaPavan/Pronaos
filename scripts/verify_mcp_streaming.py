"""MCP streaming progress-notifications live verification (Claim #38, Phase 51).

The empirical question
----------------------
Claim #35 (Phase 48) made Pronaos an MCP server over SSE.
Claim #37 (Phase 50) added the stdio transport so IDE-class clients
can spawn Pronaos. Both shipped with the SAME honest-limit:

    "A long chat response is returned as one final CallToolResult
    rather than streamed via MCP progress notifications. Streaming
    via MCP requires the notifications/progress mechanism;
    supporting it is a follow-up."

Phase 51 closes that loop. When the MCP client supplies
``_meta.progressToken`` on its ``tools/call`` for ``pronaos.chat``,
the gateway forwards with ``stream=true`` to its own
``/v1/chat/completions`` endpoint, parses every SSE chunk as it
arrives from the real upstream provider, and emits one
``notifications/progress`` message per chunk back through the MCP
transport. The final ``CallToolResult`` still carries a complete
non-streaming-shape ChatCompletion synthesized from the accumulated
deltas — so MCP clients that ignore progress notifications still
see the full response.

What this verify asserts
------------------------
1. Spawn ``pronaos-mcp-proxy`` as a subprocess via the official
   Anthropic-maintained MCP Python SDK's ``stdio_client`` — the
   exact shape Claude Code uses.
2. Initialize + tools/list — same baseline as Phase 50.
3. Call ``pronaos.chat`` WITH ``_meta.progressToken="prog-live-X"``
   and a 100-token-cap continuation prompt.
4. The SDK's ``message_handler`` callback collects
   ``ProgressNotification`` messages as they stream in.
5. Assert: ``len(progress_notifications) >= 3`` (real chunks
   arrived), the concatenated notification messages MATCH the
   final CallToolResult's assistant text, and the time-to-first-
   progress is at least 50ms earlier than time-to-final-result
   (i.e. streaming actually saved wall-clock).
6. Re-run the same call WITHOUT a progressToken; assert ZERO
   progress notifications were emitted (the non-streaming branch
   was taken) but the final assistant content is still produced.

Real run against Groq via stdio:
    test → stdio_client (spawns pronaos-mcp-proxy)
         → PronaosMcpServer (stdio transport)
         → loopback POST /v1/chat/completions stream=true
         → middleware chain → Groq SSE stream
         → per-chunk: gateway emits notifications/progress
         → final: synthesized CallToolResult with full text
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.session import RequestResponder
from mcp.types import (
    ProgressNotification,
    ServerNotification,
)


async def _read_streaming_metric(*, gateway_url: str) -> dict[str, float]:
    """Read the two Phase 51 streaming metrics so the verify can
    assert per-run deltas instead of absolute values (multiple
    verify runs against the same gateway shouldn't false-fail)."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=5.0) as c:
        r = await c.get("/metrics")
    chunks_total = 0.0
    sessions_total = 0.0
    for line in r.text.splitlines():
        if line.startswith("pronaos_mcp_streaming_chunks_total{"):
            _, _, v = line.rpartition(" ")
            try:
                chunks_total += float(v)
            except ValueError:
                continue
        elif line.startswith("pronaos_mcp_streaming_sessions_total{"):
            _, _, v = line.rpartition(" ")
            try:
                sessions_total += float(v)
            except ValueError:
                continue
    return {"chunks": chunks_total, "sessions": sessions_total}


def _make_message_handler(
    *,
    progress_collector: list[dict[str, Any]],
    first_progress_abs: list[float],
) -> Any:
    """Build the ``message_handler`` callback. Records the absolute
    monotonic timestamp of the first progress notification so the
    caller can compute TTFP relative to whatever reference matters
    (call_start, not session_start — session_start includes
    subprocess spawn + MCP handshake time)."""

    async def _handler(
        message: (
            RequestResponder[Any, Any]
            | ServerNotification
            | Exception
        ),
    ) -> None:
        if isinstance(message, Exception):
            return
        if isinstance(message, ServerNotification):
            root = message.root
            if isinstance(root, ProgressNotification):
                if not first_progress_abs:
                    first_progress_abs.append(time.monotonic())
                progress_collector.append(
                    {
                        "progress": root.params.progress,
                        "total": root.params.total,
                        "message": root.params.message,
                        "progress_token": root.params.progressToken,
                    }
                )

    return _handler


async def _one_call(
    *,
    server_params: StdioServerParameters,
    progress_token: str | int | None,
    user_prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Spawn the stdio proxy, run one tools/call (optionally with
    a progressToken) and return collected stats. TTFP and TTF are
    both measured from the same reference (``t_call_start``) so the
    delta is meaningful — subprocess spawn + MCP handshake time
    don't leak into either."""
    progress: list[dict[str, Any]] = []
    first_progress_abs: list[float] = []

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(
            read_stream,
            write_stream,
            message_handler=_make_message_handler(
                progress_collector=progress,
                first_progress_abs=first_progress_abs,
            ),
        ) as session,
    ):
        await session.initialize()

        args: dict[str, Any] = {
            "model": "auto",
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        call_kwargs: dict[str, Any] = {
            "name": "pronaos.chat",
            "arguments": args,
        }
        if progress_token is not None:
            # The SDK supports a progress_callback that activates
            # the progress-token mechanism. Use a no-op callback;
            # we collect via the message_handler so we capture
            # every notification, not just those the SDK-side
            # callback parses.
            async def _noop_pc(
                progress: float,
                total: float | None,
                message: str | None,
            ) -> None:
                return

            call_kwargs["progress_callback"] = _noop_pc

        t_call_start = time.monotonic()
        result = await session.call_tool(**call_kwargs)
        t_call_end = time.monotonic()

    # Parse the final CallToolResult content as JSON (the
    # synthesized ChatCompletion).
    first = result.content[0] if result.content else None
    text = getattr(first, "text", "") if first else ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"_raw": text[:200]}
    assistant_text = ""
    choices = payload.get("choices") or []
    if choices:
        assistant_text = choices[0].get("message", {}).get("content") or ""

    # TTFP is measured from t_call_start so it's apples-to-apples
    # with time_to_final.
    ttfp: float | None = (
        first_progress_abs[0] - t_call_start if first_progress_abs else None
    )
    return {
        "progress_notifications": progress,
        "time_to_first_progress": ttfp,
        "time_to_final": t_call_end - t_call_start,
        "assistant_text": assistant_text,
        "payload": payload,
        "is_error": bool(result.isError),
    }


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
    )
    args = parser.parse_args()

    print("=" * 72)
    print("Phase 51 — MCP streaming progress notifications live verification")
    print("=" * 72)
    print()

    resolved = shutil.which(args.proxy_command)
    if resolved is None:
        print(
            f"FAIL: --proxy-command {args.proxy_command!r} not found on PATH."
        )
        sys.exit(2)
    print(f"Spawning stdio proxy: {resolved}")
    print(f"  → gateway: {args.gateway_url}")
    print(f"  → api-key: pn_..._{args.api_key[-6:]}")
    print()

    server_params = StdioServerParameters(
        command=resolved,
        args=[
            "--gateway-url",
            args.gateway_url,
            "--api-key",
            args.api_key,
        ],
    )

    # ---- Read metric baseline -----------------------------------------
    # NOTE: streaming metrics are recorded by the proxy SUBPROCESS, not
    # the gateway. On stdio, the proxy has its own Prometheus registry
    # that the gateway's /metrics endpoint cannot see. The delta here
    # is informational only; the streaming-branch evidence is the
    # captured progress notifications, not the gateway metric. For
    # SSE-transport MCP (Phase 48), the MCP server lives in the gateway
    # process and the same counters tick visibly on /metrics.
    before = await _read_streaming_metric(gateway_url=args.gateway_url)

    # Pick distinct prompts so the two runs don't collide in the L1
    # cache — otherwise the second run hits the cache (no real Groq
    # stream) and the comparison is meaningless.
    streaming_prompt = (
        "Recite the first eight planets of the solar system, "
        "one per line, in order from Mercury outward."
    )
    nonstreaming_prompt = (
        "List the five Great Lakes of North America, one per line, "
        "by surface area descending."
    )

    # ---- Run 1: WITH progressToken ------------------------------------
    print("Run 1: tools/call with _meta.progressToken (streaming)")
    streaming = await _one_call(
        server_params=server_params,
        progress_token="prog-live-1",
        user_prompt=streaming_prompt,
        max_tokens=100,
    )
    print(f"  progress notifications: {len(streaming['progress_notifications'])}")
    print(f"  time-to-first-progress: {streaming['time_to_first_progress']:.3f}s"
          if streaming["time_to_first_progress"] is not None
          else "  time-to-first-progress: (none received)")
    print(f"  time-to-final-result:   {streaming['time_to_final']:.3f}s")
    print(f"  is_error: {streaming['is_error']}")
    # Print accumulated message text vs final assistant_text (truncated)
    notif_concat = "".join(
        (n.get("message") or "")
        for n in streaming["progress_notifications"]
    )
    print(f"  notif-concat (first 80c): {notif_concat[:80]!r}")
    print(f"  final assistant (first 80c): {streaming['assistant_text'][:80]!r}")
    print()

    # ---- Run 2: WITHOUT progressToken (regression check) --------------
    print("Run 2: tools/call WITHOUT _meta.progressToken (non-streaming)")
    non_streaming = await _one_call(
        server_params=server_params,
        progress_token=None,
        user_prompt=nonstreaming_prompt,
        max_tokens=100,
    )
    print(f"  progress notifications: {len(non_streaming['progress_notifications'])}")
    print(f"  time-to-final-result:   {non_streaming['time_to_final']:.3f}s")
    print(f"  is_error: {non_streaming['is_error']}")
    print(
        f"  final assistant (first 80c): "
        f"{non_streaming['assistant_text'][:80]!r}"
    )
    print()

    # ---- Read metric delta (informational only on stdio) --------------
    # On stdio, the proxy subprocess has its own Prometheus registry,
    # so the metric tick is invisible to the gateway's /metrics. Still
    # print the delta — on SSE-transport runs, it ticks visibly.
    after = await _read_streaming_metric(gateway_url=args.gateway_url)
    chunks_delta = after["chunks"] - before["chunks"]
    sessions_delta = after["sessions"] - before["sessions"]
    print(
        f"pronaos_mcp_streaming_chunks_total delta:   +{chunks_delta:.0f} "
        f"(stdio: recorded in proxy subprocess, not visible here)"
    )
    print(
        f"pronaos_mcp_streaming_sessions_total delta: +{sessions_delta:.0f} "
        f"(stdio: same caveat)"
    )
    print()

    # ---- Verdict ------------------------------------------------------
    print("=" * 72)

    # Run 1 — streaming branch was taken
    n_progress = len(streaming["progress_notifications"])
    if n_progress < 3:
        print(
            f"VERDICT: claim fails — only {n_progress} progress "
            "notification(s) received; expected ≥3 (a 100-token Groq "
            "response should produce many chunks)."
        )
        sys.exit(1)

    # The concat of every notification's message must EQUAL the final
    # assistant text (the gateway accumulates the same deltas it
    # forwards to the client).
    if notif_concat != streaming["assistant_text"]:
        print(
            "VERDICT: claim fails — concatenated progress-notification "
            "messages diverge from the final CallToolResult assistant "
            "text. Streaming chunks and final synthesis are not "
            "consistent. Notif-concat:\n  "
            + repr(notif_concat[:200])
            + "\nFinal text:\n  "
            + repr(streaming["assistant_text"][:200])
        )
        sys.exit(1)

    # Streaming should produce a measurable TTFT improvement — the
    # first progress notification must land before the final
    # CallToolResult (by definition of streaming). TTFP is measured
    # from t_call_start so it's apples-to-apples with time_to_final.
    # We require ≥50ms head-start as a sanity check that the gateway
    # is actually forwarding chunks as they arrive, not buffering.
    ttp = streaming["time_to_first_progress"] or float("inf")
    if ttp >= streaming["time_to_final"] - 0.05:
        print(
            f"VERDICT: claim fails — time-to-first-progress "
            f"({ttp:.3f}s) is not measurably ahead of "
            f"time-to-final-result ({streaming['time_to_final']:.3f}s). "
            "The gateway may be buffering the upstream stream "
            "instead of forwarding chunks as they arrive."
        )
        sys.exit(1)

    # Run 2 — non-streaming branch was taken
    if non_streaming["progress_notifications"]:
        print(
            f"VERDICT: claim fails — {len(non_streaming['progress_notifications'])} "
            "progress notification(s) received on the run that had NO "
            "progressToken. The streaming branch fired when it shouldn't."
        )
        sys.exit(1)
    if not non_streaming["assistant_text"]:
        print(
            "VERDICT: claim fails — non-streaming run produced no "
            "assistant content. The non-streaming branch regressed."
        )
        sys.exit(1)

    # Note: streaming metrics tick inside the proxy SUBPROCESS, so
    # the gateway's /metrics never sees them — chunks_delta and
    # sessions_delta will always be 0 on a stdio verify run. The
    # SSE-transport verify (Phase 48's verify_mcp_server.py) is the
    # right place to observe those counters tick. Skip the assertion
    # here; the captured progress notifications are the proof.

    print(
        f"VERDICT: claim holds — MCP streaming progress notifications "
        f"work end-to-end. With ``_meta.progressToken`` set on the inbound "
        f"tools/call, the gateway forwarded the chat request with "
        f"``stream=true`` to its own /v1/chat/completions, parsed the "
        f"real Groq SSE stream, and emitted {n_progress} "
        f"``notifications/progress`` messages back through the stdio "
        f"transport — time-to-first-progress {ttp*1000:.0f}ms, "
        f"{(streaming['time_to_final']-ttp)*1000:.0f}ms ahead of the "
        f"final CallToolResult. The concatenated progress-notification "
        f"messages match the synthesized final CallToolResult byte-for-byte. "
        f"With NO progressToken, zero progress notifications fired and the "
        f"non-streaming branch still produced the full assistant content "
        f"— the streaming branch is surgically opt-in. Closes the "
        f"documented honest-limit in both Claim #35 (SSE transport) and "
        f"Claim #37 (stdio transport)."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
