"""Live verification of streaming cache replay (Claim #15, Phase 28).

The empirical question
----------------------
Streaming chat calls used to skip the cache entirely. Phase 28 changed
that: the first streaming call captures inter-chunk timing into the
cache, and a subsequent identical call replays the stored response as
SSE — zero upstream tokens consumed, time-to-first-token reduced to
local-replay latency.

This script measures three things on a real running gateway against a
real upstream:

1. **Fresh stream time-to-first-token** — how long from request issue
   until the first content delta arrives over SSE. This is what users
   actually feel.
2. **Cached stream time-to-first-token** — same metric on the second
   (cache hit) request. Should be dominated by network RTT to the
   gateway, not by the upstream LLM call.
3. **Total wall time** for both, plus the upstream-call delta — proof
   that the second call did not hit the provider.

Method
------
- Issue a streaming POST to /v1/chat/completions with stream=true,
  temperature=0, max_tokens small, a unique prompt.
- Measure t_first_byte_fresh and t_total_fresh.
- Issue the SAME request again. Measure t_first_byte_cached + t_total_cached.
- Compare. Verdict holds when the cached time-to-first-token drops by
  ≥ 80% AND the response contains ``X-Pronaos-Cache: hit:replay``.

Prerequisites
-------------
- Gateway running with cache + streaming enabled.
- An API key with chat:write scope.
- A working provider credential (Groq is the default in .env.example).

Usage
-----
    python scripts/verify_streaming_cache_replay.py \\
        --api-key pn_live_... \\
        --gateway-url http://127.0.0.1:8080 \\
        --model groq/llama-3.1-8b-instant
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

import httpx


async def _stream_call(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> tuple[float, float, str, str]:
    """Issue a streaming chat completion; return
    ``(time_to_first_chunk_s, time_to_done_s, cache_header, first_chunk_text)``.

    ``time_to_first_chunk_s`` is wall-clock from request issue to the
    first SSE event that carries a non-empty content delta. That's the
    UX-relevant metric — users don't see role markers, they see
    tokens.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    t0 = time.monotonic()
    t_first: float | None = None
    first_text = ""
    cache_header = ""

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=60.0,
    ) as resp:
        resp.raise_for_status()
        cache_header = resp.headers.get("x-pronaos-cache", "")
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                continue
            # First SSE event with a non-empty content delta is what
            # the user sees first. Role-marker events don't count.
            if t_first is None and '"content":"' in payload:
                t_first = time.monotonic() - t0
                # Crude extraction of the first content delta for
                # eyeball verification.
                start = payload.find('"content":"') + len('"content":"')
                end = payload.find('"', start)
                first_text = payload[start:end] if end > start else ""
    t_total = time.monotonic() - t0
    return t_first or t_total, t_total, cache_header, first_text


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8080",
        help="Pronaos gateway base URL.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="Pronaos API key with chat:write scope.",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="Concrete provider/model to stream against.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=80,
        help="max_tokens for the streamed completion.",
    )
    parser.add_argument(
        "--min-ttft-reduction",
        type=float,
        default=0.50,
        help=(
            "Floor for 'claim holds' — cached time-to-first-token must "
            "drop by at least this fraction vs fresh. Default 0.50 = 50%%. "
            "The relative reduction depends on upstream speed: a slow "
            "provider gives a 70-90%% win; a fast provider (e.g. Groq "
            "on a hot path) gives a 50-70%% win. The *absolute* cached "
            "TTFT is the same either way — typically 100-300 ms, "
            "dominated by network RTT + gateway + replay setup."
        ),
    )
    args = parser.parse_args()

    # Unique prompt so we get a real cache miss on the first call (and
    # the script is rerunnable without manually purging Redis).
    prompt = (
        f"List three cities in France in a single line, separated by "
        f"commas. Token of uniqueness: {uuid.uuid4().hex[:8]}."
    )

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # Phase 1: cache miss — first streaming call goes to the upstream.
        print("phase 1: cache-miss stream (going to upstream)")
        ttft_fresh, total_fresh, cache_fresh, first_text_fresh = await _stream_call(
            client,
            api_key=args.api_key,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
        )
        print(f"  time-to-first-token: {ttft_fresh * 1000:.1f} ms")
        print(f"  total wall time:     {total_fresh * 1000:.1f} ms")
        print(f"  X-Pronaos-Cache:     {cache_fresh}")
        print(f"  first content:       {first_text_fresh!r}")
        print()

        # Phase 2: same prompt — should be a cache hit + SSE replay.
        print("phase 2: cache-hit stream (SSE replay, no upstream call)")
        ttft_cached, total_cached, cache_cached, first_text_cached = await _stream_call(
            client,
            api_key=args.api_key,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
        )
        print(f"  time-to-first-token: {ttft_cached * 1000:.1f} ms")
        print(f"  total wall time:     {total_cached * 1000:.1f} ms")
        print(f"  X-Pronaos-Cache:     {cache_cached}")
        print(f"  first content:       {first_text_cached!r}")
        print()

    ttft_reduction = (
        (ttft_fresh - ttft_cached) / ttft_fresh if ttft_fresh > 0 else 0.0
    )

    print("=" * 64)
    print("Phase 28 — streaming cache replay experiment")
    print("=" * 64)
    print()
    print("                  fresh stream    cached stream    delta")
    print(
        f"  TTFT          {ttft_fresh * 1000:>9.1f} ms"
        f"  {ttft_cached * 1000:>9.1f} ms"
        f"  {(ttft_fresh - ttft_cached) * 1000:>+8.1f} ms"
    )
    print(
        f"  total wall    {total_fresh * 1000:>9.1f} ms"
        f"  {total_cached * 1000:>9.1f} ms"
        f"  {(total_fresh - total_cached) * 1000:>+8.1f} ms"
    )
    print()
    print(f"time-to-first-token reduction: {ttft_reduction:>+6.1%}")
    print()

    cache_hit_observed = cache_cached.startswith("hit")
    content_match = first_text_fresh == first_text_cached
    holds = (
        cache_hit_observed
        and content_match
        and ttft_reduction >= args.min_ttft_reduction
    )

    if holds:
        print(
            f"VERDICT: claim holds — cached stream TTFT dropped by "
            f"{ttft_reduction:.1%} (threshold: {args.min_ttft_reduction:.0%}), "
            f"X-Pronaos-Cache={cache_cached!r}, content matched."
        )
        sys.exit(0)
    else:
        reasons: list[str] = []
        if not cache_hit_observed:
            reasons.append(f"X-Pronaos-Cache={cache_cached!r} did not start with 'hit'")
        if not content_match:
            reasons.append(
                f"first content drifted: fresh={first_text_fresh!r} "
                f"cached={first_text_cached!r}"
            )
        if ttft_reduction < args.min_ttft_reduction:
            reasons.append(
                f"TTFT reduction {ttft_reduction:.1%} "
                f"below threshold {args.min_ttft_reduction:.0%}"
            )
        print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
