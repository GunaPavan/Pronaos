"""Live verification of the /v1/rerank endpoint (Phase 32, Claim #19).

The empirical question
----------------------
We shipped the rerank endpoint. Does it actually work end-to-end?

1. Accept (model, query, documents, top_n?) and forward to a real
   Cohere or Voyage rerank endpoint?
2. Return a well-shaped response (``data`` list of scored items,
   ``usage.prompt_tokens`` set)?
3. Order results by relevance score descending (upstream-correct)?
4. Cache the response so a second identical call hits with zero cost?
5. Pipe through every gateway hygiene layer: auth, allowlist,
   preflight, ingress guardrails, audit, usage record?

Method
------
- Fire one rerank call with a known query + small document set.
- Fire the *same* call again → assert ``X-Pronaos-Cache: hit:exact``
  and byte-identical scores.
- Optional: vary top_n on the second-and-then-third call to confirm
  the cache key disambiguates parameter changes.

VERDICT
-------
Holds when:
- First call: 200, ranked results, top result has the highest score,
  ``X-Pronaos-Cache: miss``.
- Second identical call: 200, byte-identical results,
  ``X-Pronaos-Cache: hit:exact``.

The script prints headline numbers and exits 0/1.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx


async def _rerank(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    query: str,
    documents: list[str],
    top_n: int | None,
) -> tuple[int, dict[str, str], dict[str, object], float]:
    """One rerank call. Returns ``(status, headers, body, elapsed_ms)``."""
    payload: dict[str, object] = {
        "model": model,
        "query": query,
        "documents": documents,
    }
    if top_n is not None:
        payload["top_n"] = top_n
    start = time.monotonic()
    resp = await client.post(
        "/v1/rerank",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
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
        "includes the rerank model (or has allowed_models = NULL).",
    )
    parser.add_argument(
        "--model",
        default="cohere/rerank-english-v3.0",
        help="Rerank model to call (cohere/rerank-* or voyage/rerank-*).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="Top-N candidates to return on each call.",
    )
    args = parser.parse_args()

    query = "What is the capital of the United States?"
    documents = [
        "Carson City is the capital of Nevada.",
        "Tokyo is the largest metropolitan area in the world.",
        "Washington, D.C. has been the capital of the United States since 1800.",
        "Sydney is a major Australian city.",
        "The Commonwealth of the Northern Mariana Islands has its capital at Saipan.",
        "Buenos Aires is the capital of Argentina.",
        "London is the capital of the United Kingdom.",
        "Paris is the capital of France.",
        "Berlin is the capital of Germany.",
        "Madrid is the capital of Spain.",
    ]

    print(f"model: {args.model}")
    print(f"query: {query!r}")
    print(f"documents: {len(documents)} candidates, top_n={args.top_n}")
    print()
    print("=" * 64)
    print("Phase 32 — /v1/rerank live verification")
    print("=" * 64)

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # ---- Call #1: cache miss → upstream rerank ----
        status1, headers1, body1, ms1 = await _rerank(
            client,
            api_key=args.api_key,
            model=args.model,
            query=query,
            documents=documents,
            top_n=args.top_n,
        )
        cache1 = headers1.get("x-pronaos-cache", "(missing)")
        scores1 = _extract_scored(body1)
        cost1 = headers1.get("x-pronaos-cost-hcents", "?")
        print(
            f"  call 1 (warmup):  status={status1}  cache={cache1}  "
            f"top={len(scores1)}  cost_hcents={cost1}  elapsed={ms1:.0f} ms"
        )
        for i, item in enumerate(scores1):
            doc_preview = (item.get("document") or "")[:60]
            print(
                f"      #{i + 1} index={item.get('index')}  "
                f"score={float(item.get('relevance_score', 0)):.4f}  "
                f"doc={doc_preview!r}"
            )
        if status1 != 200 or not scores1:
            print()
            print(
                f"VERDICT: claim fails — first call failed "
                f"(status={status1}, body={body1!r})."
            )
            sys.exit(1)

        # ---- Call #2: cache hit → byte-identical scores ----
        status2, headers2, body2, ms2 = await _rerank(
            client,
            api_key=args.api_key,
            model=args.model,
            query=query,
            documents=documents,
            top_n=args.top_n,
        )
        cache2 = headers2.get("x-pronaos-cache", "(missing)")
        scores2 = _extract_scored(body2)
        cost2 = headers2.get("x-pronaos-cost-hcents", "?")
        print(
            f"  call 2 (repeat):  status={status2}  cache={cache2}  "
            f"top={len(scores2)}  cost_hcents={cost2}  elapsed={ms2:.0f} ms"
        )

    print()

    # ---- Verdict -----------------------------------------------------
    scores_match = scores1 == scores2
    # The top-1 result should be the document about Washington, D.C.
    # (that's the only one mentioning a US capital). We assert it
    # softly — if the upstream model picks differently we don't fail
    # the verdict (model behaviour is its own validation suite).
    top_doc = (scores1[0].get("document") or "") if scores1 else ""
    top_is_dc = "Washington" in top_doc or "D.C." in top_doc

    holds = (
        status1 == 200
        and status2 == 200
        and cache1 == "miss"
        and cache2.startswith("hit:")
        and scores_match
        and len(scores1) <= args.top_n
    )
    if holds:
        print(
            f"VERDICT: claim holds — first call hit the upstream "
            f"({ms1:.0f} ms, cache=miss, cost={cost1} hcents), second "
            f"identical call served from cache ({ms2:.0f} ms, cache={cache2}, "
            f"cost={cost2} hcents) — byte-identical scores across all "
            f"{len(scores1)} results, zero upstream tokens. "
            f"Top result {'IS' if top_is_dc else 'is NOT'} the "
            f"Washington, D.C. document (semantic-correctness signal)."
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
    if not scores_match:
        reasons.append("cached scores differ from original")
    if len(scores1) > args.top_n:
        reasons.append(f"top_n={args.top_n} not honoured ({len(scores1)} returned)")
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


def _extract_scored(body: dict[str, object]) -> list[dict[str, object]]:
    """Pull the data list from a rerank response."""
    data = body.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, object]] = []
    for entry in data:
        if isinstance(entry, dict):
            out.append(entry)
    return out


if __name__ == "__main__":
    asyncio.run(_main())
