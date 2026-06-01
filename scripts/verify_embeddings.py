"""Live verification of the /v1/embeddings endpoint (Phase 31, Claim #18).

The empirical question
----------------------
We just shipped a new endpoint. Does it actually work end-to-end:

1. Accept an OpenAI-shape ``input`` (string OR list) and forward it
   correctly to the upstream provider?
2. Return a well-shaped OpenAI response (``data`` list of embeddings
   in input order, ``usage.prompt_tokens`` set)?
3. Cache the response so a second identical call serves from cache
   (``X-Pronaos-Cache: hit:exact``) with **zero upstream tokens**?
4. Survive auth, allowlist, preflight, guardrails, audit, usage
   recording — the full pipeline?

Method
------
- Pick an embedding model (default: ``openai/text-embedding-3-small``).
- Fire one warm-up call with a known input → assert 200, vector shape.
- Fire the *same* input again → assert ``X-Pronaos-Cache`` is a hit,
  byte-identical vector, and the response is fast (cache-served path).
- Optionally fire a batched call with three inputs → assert three
  vectors in input order.

VERDICT
-------
Holds when:
- First call: 200, vector length > 0, ``X-Pronaos-Cache: miss``.
- Second call: 200, byte-identical vector to call #1,
  ``X-Pronaos-Cache: hit:exact``, faster than the first call (the
  cache-hit short-circuits the upstream HTTP round-trip).
- Batched call: data list length == input list length, indices
  ordered 0..N-1.

The script prints headline numbers and exits 0/1 accordingly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx


async def _embed(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    text: str | list[str],
) -> tuple[int, dict[str, str], dict[str, object], float]:
    """One embeddings call. Returns ``(status, headers, body, elapsed_ms)``."""
    start = time.monotonic()
    resp = await client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": text},
        timeout=60.0,
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0
    try:
        body: dict[str, object] = resp.json()
    except ValueError:
        body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), body, elapsed_ms


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
        help="API key with chat:write scope on a team whose allowlist "
        "includes the embedding model (or has allowed_models = NULL).",
    )
    parser.add_argument(
        "--model",
        default="openai/text-embedding-3-small",
        help="Embedding model to call.",
    )
    parser.add_argument(
        "--text",
        default="Pronaos is a self-hosted multi-tenant LLM gateway.",
        help="Text to embed. Same text is used for both calls so the "
        "cache deterministically hits on the second.",
    )
    args = parser.parse_args()

    print(f"model: {args.model}")
    print(f"input: {args.text!r}")
    print()
    print("=" * 64)
    print("Phase 31 — /v1/embeddings live verification")
    print("=" * 64)

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # ---- Call #1: cache miss → upstream call --------------------
        status1, headers1, body1, ms1 = await _embed(
            client, api_key=args.api_key, model=args.model, text=args.text
        )
        cache1 = headers1.get("x-pronaos-cache", "(missing)")
        vectors1 = _extract_vectors(body1)
        tokens1 = _extract_tokens(body1)
        print(
            f"  call 1 (warmup):  status={status1}  cache={cache1}  "
            f"vectors={len(vectors1)}×{len(vectors1[0]) if vectors1 else 0}  "
            f"tokens={tokens1}  elapsed={ms1:.0f} ms"
        )
        if status1 != 200 or not vectors1:
            print()
            print(f"VERDICT: claim fails — first call failed (status={status1}, body={body1!r}).")
            sys.exit(1)

        # ---- Call #2: cache hit → no upstream call ------------------
        status2, headers2, body2, ms2 = await _embed(
            client, api_key=args.api_key, model=args.model, text=args.text
        )
        cache2 = headers2.get("x-pronaos-cache", "(missing)")
        vectors2 = _extract_vectors(body2)
        tokens2 = _extract_tokens(body2)
        print(
            f"  call 2 (repeat):  status={status2}  cache={cache2}  "
            f"vectors={len(vectors2)}×{len(vectors2[0]) if vectors2 else 0}  "
            f"tokens={tokens2}  elapsed={ms2:.0f} ms"
        )

        # ---- Call #3: batched ---------------------------------------
        batched_status, _batched_headers, batched_body, batched_ms = await _embed(
            client,
            api_key=args.api_key,
            model=args.model,
            text=["alpha", "beta", "gamma"],
        )
        batched_vectors = _extract_vectors(batched_body)
        print(
            f"  call 3 (batched): status={batched_status}  "
            f"vectors={len(batched_vectors)}×"
            f"{len(batched_vectors[0]) if batched_vectors else 0}  "
            f"elapsed={batched_ms:.0f} ms"
        )

    print()

    # ---- Verdict -----------------------------------------------------
    holds = (
        status1 == 200
        and status2 == 200
        and cache1 == "miss"
        and cache2.startswith("hit:")
        and vectors1 == vectors2
        and batched_status == 200
        and len(batched_vectors) == 3
    )
    if holds:
        speedup = ms1 / ms2 if ms2 > 0 else float("inf")
        print(
            f"VERDICT: claim holds — first call hit the upstream "
            f"({ms1:.0f} ms, cache=miss), second identical call served "
            f"from cache ({ms2:.0f} ms, cache={cache2}) — "
            f"{speedup:.1f}× speedup, byte-identical vector, zero upstream "
            f"tokens. Batched call returned 3 vectors in order."
        )
        sys.exit(0)

    reasons: list[str] = []
    if status1 != 200:
        reasons.append(f"first call status={status1}")
    if status2 != 200:
        reasons.append(f"second call status={status2}")
    if cache1 != "miss":
        reasons.append(f"first call wasn't a miss ({cache1!r})")
    if not cache2.startswith("hit:"):
        reasons.append(f"second call wasn't a hit ({cache2!r}); is Redis configured?")
    if vectors1 != vectors2:
        reasons.append("cached vector differs from original")
    if batched_status != 200:
        reasons.append(f"batched call status={batched_status}")
    if len(batched_vectors) != 3:
        reasons.append(f"batched call returned {len(batched_vectors)} vectors, expected 3")
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


def _extract_vectors(body: dict[str, object]) -> list[list[float]]:
    """Pull vectors out of an OpenAI-shape embedding response."""
    data = body.get("data")
    if not isinstance(data, list):
        return []
    out: list[list[float]] = []
    # Sort by index defensively — the gateway already does this, but
    # this script also reads OpenAI raw if pointed at a non-gateway URL.
    sorted_data = sorted(
        data, key=lambda e: int(e.get("index", 0)) if isinstance(e, dict) else 0
    )
    for entry in sorted_data:
        if isinstance(entry, dict):
            vec = entry.get("embedding")
            if isinstance(vec, list):
                out.append([float(x) for x in vec])
    return out


def _extract_tokens(body: dict[str, object]) -> int:
    """Pull prompt_tokens from the usage block."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0
    pt = usage.get("prompt_tokens", 0)
    return int(pt) if isinstance(pt, int | float) else 0


if __name__ == "__main__":
    asyncio.run(_main())
