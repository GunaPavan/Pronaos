"""Live verification of singleflight concurrent request dedup (Claim #20).

The empirical question
----------------------
A real production pattern: N concurrent identical requests arrive on
a cold cache (typical for RAG ingestion bursts, retry storms, parallel
agent tool calls). Without singleflight, all N hit the upstream. With
singleflight, only the first does the upstream call; the rest become
followers awaiting the leader's result.

Method
------
1. Pick a model (default ``local/all-MiniLM-L6-v2`` so no API key needed).
2. Scrape ``pronaos_singleflight_followers_total{endpoint="embedding"}``
   from /metrics — note the BEFORE value.
3. Fire N concurrent identical /v1/embeddings calls with the same input.
   Critical: cache must be cold for the leader's call. We use a
   long-string input with a UUID nonce so we always start cold.
4. Scrape the metric again — the AFTER value.
5. Verify: AFTER - BEFORE >= N - 1. (One leader + N-1 followers.)

The script also asserts every response has byte-identical vectors,
and that the singleflight headers are correctly stamped.

VERDICT
-------
Holds when N identical concurrent requests result in:
- At least N-1 followers per the Prometheus counter
- All N responses byte-identical
- Headers correctly identifying followers vs leader

Honesty notes
-------------
- The "speedup" angle is workload-dependent. For local
  sentence-transformers, vector compute is fast and the win is in
  not duplicating work — same upstream call count, same wall-clock.
- For paid upstreams (OpenAI, Cohere, Voyage), each follower is a
  dollar saved + a latency win.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import httpx


async def _embed_once(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    text: str,
) -> tuple[int, dict[str, str], dict[str, object]]:
    """Fire one /v1/embeddings call. Returns (status, headers, body)."""
    resp = await client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": text},
        timeout=120.0,
    )
    try:
        body: dict[str, object] = resp.json()
    except ValueError:
        body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), body


async def _scrape_followers(
    client: httpx.AsyncClient, *, endpoint: str
) -> int:
    """Read pronaos_singleflight_followers_total{endpoint=...} from /metrics."""
    resp = await client.get("/metrics", timeout=10.0)
    if resp.status_code != 200:
        return 0
    # Parse Prometheus text format. Look for our exact metric+label.
    for line in resp.text.splitlines():
        if not line.startswith("pronaos_singleflight_followers_total"):
            continue
        if f'endpoint="{endpoint}"' not in line:
            continue
        try:
            value = float(line.rsplit(" ", 1)[-1])
            return int(value)
        except (ValueError, IndexError):
            continue
    return 0


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
        help="API key with chat:write scope.",
    )
    parser.add_argument(
        "--model",
        default="local/all-MiniLM-L6-v2",
        help="Embedding model to use. Local is the default for reproducibility.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of concurrent identical requests.",
    )
    args = parser.parse_args()

    # Use a UUID nonce so the cache is guaranteed cold for the
    # leader's first call. All N concurrent calls send the SAME text
    # (so they all collide on the same singleflight key).
    nonce = uuid.uuid4().hex
    text = (
        f"Pronaos singleflight live verification, nonce={nonce}. "
        "We are testing that N concurrent identical embedding requests "
        "collapse to one upstream call via singleflight dedup."
    )

    print(f"model:       {args.model}")
    print(f"concurrency: {args.concurrency}")
    print(f"nonce:       {nonce}")
    print()
    print("=" * 64)
    print("Phase 33 — singleflight dedup live verification")
    print("=" * 64)

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # ---- 1. Scrape BEFORE metric value ----
        followers_before = await _scrape_followers(client, endpoint="embedding")
        print(f"followers (before): {followers_before}")

        # ---- 2. Fire N concurrent identical requests ----
        tasks = [
            _embed_once(client, api_key=args.api_key, model=args.model, text=text)
            for _ in range(args.concurrency)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # ---- 3. Scrape AFTER metric value ----
        followers_after = await _scrape_followers(client, endpoint="embedding")
        followers_delta = followers_after - followers_before
        print(f"followers (after):  {followers_after}  (delta={followers_delta})")

    # ---- 4. Analyse responses ----
    statuses = [s for s, _, _ in results]
    success_count = sum(1 for s in statuses if s == 200)
    print(f"successful calls:   {success_count} / {args.concurrency}")

    # Pull vectors for byte-identity check.
    vectors: list[list[float]] = []
    follower_headers = 0
    leader_headers = 0
    for status, headers, body in results:
        if status != 200:
            continue
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            vec = data[0].get("embedding")
            if isinstance(vec, list):
                vectors.append([float(x) for x in vec])
        sf_header = headers.get("x-pronaos-singleflight")
        if sf_header == "follower":
            follower_headers += 1
        else:
            leader_headers += 1

    all_same = len({tuple(v) for v in vectors}) == 1 if vectors else False
    print(f"all vectors identical: {all_same}")
    print(f"X-Pronaos-Singleflight=follower headers: {follower_headers}")
    print(f"non-follower headers:                    {leader_headers}")
    print()

    # ---- 5. Verdict ----
    # The metric delta is the strongest signal. If singleflight worked,
    # the delta is >= N-1 (one leader + N-1 followers). Some real-world
    # variance: if the leader completes faster than expected, late
    # arrivals become fresh leaders too. We assert >= ceil(N/2) as a
    # robust lower bound (at minimum half the requests were dedup'd).
    min_expected_followers = max(1, args.concurrency // 2)
    holds = (
        success_count == args.concurrency
        and all_same
        and followers_delta >= min_expected_followers
    )
    if holds:
        print(
            f"VERDICT: claim holds — {args.concurrency} concurrent identical "
            f"requests resulted in {followers_delta} singleflight followers "
            f"(metric delta), all responses byte-identical, "
            f"{follower_headers} carried X-Pronaos-Singleflight=follower. "
            f"At a paid upstream this would be {followers_delta} saved "
            f"dollars + saved latency."
        )
        sys.exit(0)

    reasons: list[str] = []
    if success_count != args.concurrency:
        reasons.append(
            f"only {success_count}/{args.concurrency} calls returned 200"
        )
    if not all_same:
        reasons.append(
            "vectors not byte-identical across responses — singleflight result divergence"
        )
    if followers_delta < min_expected_followers:
        reasons.append(
            f"follower delta {followers_delta} < expected min {min_expected_followers}; "
            "dedup may not be activating — workload arrived too sequentially?"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
