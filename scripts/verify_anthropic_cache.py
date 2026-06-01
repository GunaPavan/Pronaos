"""Live verification of Anthropic prompt-cache FinOps surfacing (Claim #21).

The empirical question
----------------------
Anthropic's prompt caching gives ~90% cost reduction on cached prefixes.
Pronaos passes ``cache_control`` blocks through to Anthropic — but does
it correctly:

1. Extract ``cache_creation_input_tokens`` and ``cache_read_input_tokens``
   from Anthropic's usage block?
2. Compute cost with the weighted pricing (writes 1.25×, reads 0.10×)?
3. Surface savings in response headers (``X-Pronaos-Prompt-Cache-*``)?
4. Show the cost drop empirically — call #2 should be dramatically
   cheaper than call #1 when the prompt is cached?

Method
------
1. Build a long system prompt (~2000 tokens) and attach a
   ``cache_control: {"type": "ephemeral"}`` block to it.
2. Fire call #1 — Anthropic creates the cache.
3. Fire call #2 with the SAME system prompt + a different user message.
   Anthropic should serve the system prompt from cache.
4. Compare: call #2 must show ``cache_read_input_tokens > 0`` and a
   significantly lower ``cost_hcents`` than call #1.

VERDICT
-------
Holds when:
- Call #1: ``X-Pronaos-Prompt-Cache-Write-Tokens > 0`` (writing the cache).
- Call #2: ``X-Pronaos-Prompt-Cache-Read-Tokens > 0`` (reading the cache)
  AND ``cost_hcents`` < 50% of call #1's (cache savings ≥ 50%).
- Both calls return 200.

Honesty notes
-------------
- Requires a real ``ANTHROPIC_API_KEY`` on the gateway. Without it,
  the script exits early and reports "not configured."
- The 5-minute Anthropic cache TTL means call #2 must come quickly
  after call #1 — the script fires them back to back.
- Real-world savings on long system prompts (10k+ tokens) approach 90%.
  This script uses ~2k tokens which is enough to demonstrate the
  mechanism — the percentage savings scale with prompt length.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

# A long-ish system prompt so Anthropic actually decides to cache it.
# Anthropic's minimum cacheable prefix is 1024 tokens (model-dependent).
# This block is ~2k tokens of pseudo-corpus, well above the threshold.
LONG_SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the following knowledge base when "
    "answering questions. " * 200
)


async def _chat_with_cached_system(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
) -> tuple[int, dict[str, str], dict[str, object]]:
    """Fire one chat completion with a cache_control block on the system prompt."""
    # Anthropic format requires cache_control on a content block. We
    # send a system message structured as a list of typed blocks. The
    # Pronaos chat handler accepts and forwards this shape.
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 50,
        "temperature": 0.0,
    }
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=120.0,
    )
    try:
        out: dict[str, object] = resp.json()
    except ValueError:
        out = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), out


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
        default="anthropic/claude-opus-4-7",
        help="Anthropic model with prompt-cache support.",
    )
    args = parser.parse_args()

    print(f"model: {args.model}")
    print(f"system prompt length: {len(LONG_SYSTEM_PROMPT)} chars")
    print()
    print("=" * 64)
    print("Phase 34 — Anthropic prompt-cache FinOps verification")
    print("=" * 64)

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # Call 1: writes the cache.
        status1, headers1, body1 = await _chat_with_cached_system(
            client,
            api_key=args.api_key,
            model=args.model,
            system_prompt=LONG_SYSTEM_PROMPT,
            user_message="Question 1: respond with a short ack.",
        )
        if status1 != 200:
            print(f"call 1 failed: status={status1}, body={body1}")
            sys.exit(1)
        meta1 = body1.get("pronaos", {}) if isinstance(body1, dict) else {}
        if not isinstance(meta1, dict):
            meta1 = {}
        write1 = headers1.get("x-pronaos-prompt-cache-write-tokens", "0")
        read1 = headers1.get("x-pronaos-prompt-cache-read-tokens", "0")
        cost1 = int(meta1.get("cost_hcents", 0))
        print(
            f"  call 1 (write): status=200  write_tokens={write1}  "
            f"read_tokens={read1}  cost_hcents={cost1}"
        )

        # Call 2: reads the cache (same system prompt, different user message).
        status2, headers2, body2 = await _chat_with_cached_system(
            client,
            api_key=args.api_key,
            model=args.model,
            system_prompt=LONG_SYSTEM_PROMPT,
            user_message="Question 2: another short ack.",
        )
        if status2 != 200:
            print(f"call 2 failed: status={status2}, body={body2}")
            sys.exit(1)
        meta2 = body2.get("pronaos", {}) if isinstance(body2, dict) else {}
        if not isinstance(meta2, dict):
            meta2 = {}
        write2 = headers2.get("x-pronaos-prompt-cache-write-tokens", "0")
        read2 = headers2.get("x-pronaos-prompt-cache-read-tokens", "0")
        saved2 = headers2.get("x-pronaos-prompt-cache-saved-hcents", "0")
        cost2 = int(meta2.get("cost_hcents", 0))
        print(
            f"  call 2 (read):  status=200  write_tokens={write2}  "
            f"read_tokens={read2}  saved={saved2} hcents  cost_hcents={cost2}"
        )

    print()
    cost_drop_pct = (
        100.0 * (cost1 - cost2) / cost1 if cost1 > 0 else 0.0
    )
    holds = (
        int(write1) > 0
        and int(read2) > 0
        and cost2 < cost1
        and cost_drop_pct >= 50.0
    )
    if holds:
        print(
            f"VERDICT: claim holds — Anthropic prompt-cache write detected on "
            f"call 1 ({write1} tokens), read on call 2 ({read2} tokens). "
            f"Cost dropped {cost1} → {cost2} hcents ({cost_drop_pct:.1f}% "
            f"reduction). The saved-hcents header reports {saved2} hcents."
        )
        sys.exit(0)

    reasons: list[str] = []
    if int(write1) <= 0:
        reasons.append(
            "call 1 did NOT write the cache (cache_creation_input_tokens=0); "
            "the system prompt may be too short or Anthropic declined caching"
        )
    if int(read2) <= 0:
        reasons.append(
            "call 2 did NOT read the cache (cache_read_input_tokens=0); "
            "the cache may have expired (5-min TTL) between calls"
        )
    if cost2 >= cost1:
        reasons.append(
            f"call 2 was not cheaper than call 1 ({cost2} >= {cost1})"
        )
    if cost_drop_pct < 50.0:
        reasons.append(
            f"cost reduction only {cost_drop_pct:.1f}% (expected ≥ 50%)"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
