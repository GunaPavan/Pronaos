"""Tool-use-aware routing live verification (Claim #33, Phase 46).

The empirical question
----------------------
Phase 45 produces per-model tool-use accuracy scores. Phase 46 wires
them into the router: when the request carries tools AND the team's
strategy is ``tool-use-aware-cheapest``, the router filters models
by stored tool-use accuracy BEFORE picking the cheapest survivor.

This script proves the composition works end-to-end against a real
gateway:

1. Seed ``team.tool_use_scores`` from Phase 45's live data
   (70B=1.0, 8B=0.917, Scout=0.833).
2. Set ``team.tool_use_threshold = 0.95``.
3. Switch routing strategy to ``tool-use-aware-cheapest``.
4. Fire request A: ``model="auto"`` + tools → assert the gateway
   routes to **70B** (only model above 0.95).
5. Fire request B: ``model="auto"`` + NO tools → assert the gateway
   routes to **8B** (cheapest; tool-use filter is bypassed).

If both routings match the predictions, the platform-composing
claim holds — the gateway uses its own eval data to inform routing
decisions, and the filter applies surgically (only when relevant).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx


async def _put_admin(
    *,
    client: httpx.AsyncClient,
    admin_key: str,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.put(
        path,
        headers={"Authorization": f"Bearer {admin_key}"},
        json=body,
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


async def _chat_call(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    with_tools: bool,
) -> tuple[int, str | None, dict[str, Any]]:
    body: dict[str, Any] = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "What's the weather in Tokyo right now?"}
        ],
        "max_tokens": 60,
        "temperature": 0.0,
    }
    if with_tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        body["tool_choice"] = "auto"
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=60.0,
    )
    routed = resp.headers.get("x-pronaos-routed-model")
    try:
        out: dict[str, Any] = resp.json()
    except ValueError:
        out = {"_raw": resp.text}
    return resp.status_code, routed, out


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--admin-api-key", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help="Tool-use accuracy threshold (default 0.95).",
    )
    args = parser.parse_args()

    # Phase 45 captured per-model accuracies on the curated 12-case set:
    seed_scores = {
        "groq/llama-3.3-70b-versatile": {
            "score": 1.0,
            "n_samples": 12,
            "source_eval_id": "tool_use_basic-phase45-live",
            "ts": "2026-05-21T17:02:00Z",
        },
        "groq/llama-3.1-8b-instant": {
            "score": 0.917,
            "n_samples": 12,
            "source_eval_id": "tool_use_basic-phase45-live",
            "ts": "2026-05-21T17:02:00Z",
        },
        "groq/meta-llama/llama-4-scout-17b-16e-instruct": {
            "score": 0.833,
            "n_samples": 12,
            "source_eval_id": "tool_use_basic-phase45-live",
            "ts": "2026-05-21T17:02:00Z",
        },
    }

    print("=" * 72)
    print("Phase 46 — tool-use-aware-cheapest routing live verification")
    print("=" * 72)
    print()
    print(
        f"Seeding tool_use_scores from Phase 45 (threshold = {args.threshold})..."
    )
    print("  70B   = 1.000")
    print("  8B    = 0.917")
    print("  Scout = 0.833")
    print()

    async with httpx.AsyncClient(base_url=args.gateway_url, timeout=60.0) as client:
        await _put_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/tool-use-scores",
            body={"scores": seed_scores, "threshold": args.threshold},
        )
        await _put_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/routing-strategy",
            body={"strategy": "tool-use-aware-cheapest"},
        )

        # ---- Request A: model="auto" + tools ----------------------------
        print("Request A: model='auto' + tools → expect routing to 70B")
        status_a, routed_a, body_a = await _chat_call(
            client=client, api_key=args.api_key, with_tools=True
        )
        print(f"  HTTP status:           {status_a}")
        print(f"  X-Pronaos-Routed-Model: {routed_a}")
        if status_a == 200 and isinstance(body_a, dict):
            choices = body_a.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message", {})
                tcs = msg.get("tool_calls") or []
                if tcs:
                    fn = (tcs[0].get("function") or {}).get("name", "")
                    print(f"  model emitted tool:    {fn!r}")
        print()

        # ---- Request B: model="auto" + NO tools -------------------------
        print("Request B: model='auto' + NO tools → expect routing to 8B (cheapest)")
        status_b, routed_b, _body_b = await _chat_call(
            client=client, api_key=args.api_key, with_tools=False
        )
        print(f"  HTTP status:           {status_b}")
        print(f"  X-Pronaos-Routed-Model: {routed_b}")
        print()

        # ---- Cleanup ----------------------------------------------------
        # Don't leave the team in a state that breaks subsequent demos.
        await _put_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/tool-use-scores",
            body={"scores": None, "threshold": None},
        )
        await _put_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/routing-strategy",
            body={"strategy": None},
        )

    # ---- Verdict --------------------------------------------------------
    expected_a = "groq/llama-3.3-70b-versatile"
    expected_b = "groq/llama-3.1-8b-instant"
    a_ok = status_a == 200 and routed_a == expected_a
    b_ok = status_b == 200 and routed_b == expected_b

    print("=" * 72)
    if a_ok and b_ok:
        print(
            "VERDICT: claim holds — the gateway composed Phase 45 (per-model "
            "tool-use accuracy) into Phase 24's quality-aware router as a "
            "new ``tool-use-aware-cheapest`` strategy. With "
            f"threshold={args.threshold}: a tool-bearing request routed "
            f"to {routed_a} (the only model above {args.threshold}); a "
            f"tool-less request bypassed the filter and routed to "
            f"{routed_b} (cheapest in the eligible pool). The filter "
            f"applies surgically — when tool quality matters, and never "
            f"when it doesn't."
        )
        sys.exit(0)

    reasons: list[str] = []
    if not a_ok:
        reasons.append(
            f"Request A: expected routed={expected_a!r}, got {routed_a!r} "
            f"(status={status_a})"
        )
    if not b_ok:
        reasons.append(
            f"Request B: expected routed={expected_b!r}, got {routed_b!r} "
            f"(status={status_b})"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
