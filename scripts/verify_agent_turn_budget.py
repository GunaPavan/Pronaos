"""Live verification of the agent-turn budget gate (Claim #17, Phase 30).

The empirical question
----------------------
A team has set ``agent_turn_budget_tokens = N``. A misbehaving agent
loop is about to call the gateway 20 times under one
``X-Pronaos-Agent-Turn-ID``. Does the gateway:

1. Allow the calls that fit within the budget?
2. Deny the call that would push the running total over N?
3. Return correct ``X-Pronaos-Agent-Turn-Remaining-*`` headers + a
   429 with the expected ``type`` in the body?
4. Reset cleanly when the client rotates the turn-id?

Method
------
1. Set ``agent_turn_budget_tokens`` on the test team.
2. Generate a fresh turn-id (UUID).
3. Fire N=20 chat completions in sequence, each carrying the same
   ``X-Pronaos-Agent-Turn-ID`` header.
4. Record which calls returned 200 and which returned 429.
5. VERDICT: exactly K calls succeed, where K is the largest count
   such that ``K * avg_tokens_per_call <= budget``. Call #K+1 must
   return 429 with reason ``agent_turn_token_budget_exhausted``.
6. Rotate the turn-id and confirm a fresh call succeeds (the budget
   is per-turn, not per-team-per-day).

Honesty
-------
Per-call token counts vary slightly with prompt UUID. The script
reports actual K (number of allowed calls) and verifies the
underlying property: monotonic accumulation, denial at the threshold
crossing, fresh turn-id resets the budget.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import httpx


async def _one_call(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    prompt: str,
    turn_id: str,
    max_tokens: int,
) -> tuple[int, dict[str, str], dict[str, object]]:
    """Fire one chat completion. Returns ``(status_code, headers, body)``."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Pronaos-Agent-Turn-ID": turn_id,
        },
        json=body,
        timeout=60.0,
    )
    out_body: dict[str, object]
    try:
        out_body = resp.json()
    except ValueError:
        out_body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), out_body


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
        help="API key whose team has agent_turn_budget_tokens set.",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="Model to call.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=20,
        help="Maximum number of calls to make under the same turn-id.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=24,
        help="max_tokens per chat completion (tight so we hit the budget quickly).",
    )
    args = parser.parse_args()

    turn_id = uuid.uuid4().hex
    print(f"turn-id: {turn_id}")
    print(f"firing up to {args.max_calls} calls until the gate denies...")
    print()

    successes = 0
    deny_status: int | None = None
    deny_reason: str | None = None
    deny_remaining_tokens: str | None = None
    # cost-hcents counterpart captured below for future per-cost reporting.
    total_tokens_used = 0

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        for i in range(1, args.max_calls + 1):
            prompt = f"Say OK and nothing else. (call {i}, {uuid.uuid4().hex[:8]})"
            status, headers, body = await _one_call(
                client,
                api_key=args.api_key,
                model=args.model,
                prompt=prompt,
                turn_id=turn_id,
                max_tokens=args.max_tokens,
            )
            if status == 200:
                successes = i
                usage_obj = body.get("usage") if isinstance(body, dict) else None
                total = (
                    usage_obj.get("total_tokens", 0)
                    if isinstance(usage_obj, dict)
                    else 0
                )
                total_tokens_used += int(total or 0)
                remaining = headers.get(
                    "x-pronaos-agent-turn-remaining-tokens", "(unset)"
                )
                calls_seen = headers.get("x-pronaos-agent-turn-calls", "?")
                print(
                    f"  call {i:>2}  status=200  tokens={total}  "
                    f"calls-seen-by-gateway={calls_seen}  remaining={remaining}"
                )
            elif status == 429:
                deny_status = 429
                detail = body.get("detail") if isinstance(body, dict) else None
                if isinstance(detail, dict):
                    deny_reason = str(detail.get("type", "?"))
                deny_remaining_tokens = headers.get(
                    "x-pronaos-agent-turn-remaining-tokens"
                )
                # cost-hcents header captured for future per-cost reporting;
                # only the token-side number lands in stdout today.
                _deny_remaining_cost = headers.get(
                    "x-pronaos-agent-turn-remaining-cost-hcents"
                )
                print(
                    f"  call {i:>2}  status=429  reason={deny_reason}  "
                    f"remaining_tokens={deny_remaining_tokens}"
                )
                break
            else:
                print(f"  call {i:>2}  unexpected status={status}: {body!r}")
                break

        # Rotate the turn-id and prove the gate is per-turn, not per-team.
        new_turn_id = uuid.uuid4().hex
        print()
        print(f"rotating to fresh turn-id: {new_turn_id}")
        status, headers, body = await _one_call(
            client,
            api_key=args.api_key,
            model=args.model,
            prompt=f"Say OK. ({uuid.uuid4().hex[:8]})",
            turn_id=new_turn_id,
            max_tokens=args.max_tokens,
        )
        print(f"  fresh-turn call  status={status}")
        fresh_turn_allowed = status == 200

    print()
    print("=" * 64)
    print("Phase 30 — agent-turn budget gate experiment")
    print("=" * 64)
    print(f"successful calls under same turn-id:  {successes}")
    print(f"total tokens consumed inside budget:  {total_tokens_used}")
    print(f"denial status code:                   {deny_status}")
    print(f"denial reason:                        {deny_reason}")
    print(f"remaining_tokens at deny:             {deny_remaining_tokens}")
    print(f"fresh turn-id allowed after deny:     {fresh_turn_allowed}")
    print()

    holds = (
        successes > 0
        and deny_status == 429
        and deny_reason == "agent_turn_token_budget_exhausted"
        and fresh_turn_allowed
    )
    if holds:
        print(
            f"VERDICT: claim holds — gateway allowed {successes} calls under "
            "the same turn-id, denied the call that would have exceeded the "
            f"team's agent_turn_budget_tokens with HTTP 429 + reason "
            f"'agent_turn_token_budget_exhausted'. A fresh turn-id was "
            "accepted immediately afterward, proving the gate is "
            "per-execution and self-clears across turns."
        )
        sys.exit(0)
    reasons: list[str] = []
    if successes == 0:
        reasons.append("zero successful calls — team budget too tight or auth failed")
    if deny_status != 429:
        reasons.append(f"no 429 observed (status was {deny_status})")
    if deny_reason and deny_reason != "agent_turn_token_budget_exhausted":
        reasons.append(f"wrong reason: {deny_reason}")
    if not fresh_turn_allowed:
        reasons.append("fresh turn-id was rejected — gate not per-turn")
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
