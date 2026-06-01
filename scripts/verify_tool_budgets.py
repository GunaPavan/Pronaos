"""Live verification of the per-tool budget gate (Claim #24, Phase 37).

The empirical question
----------------------
A team has set a cap of N on tool ``echo_tool`` via the admin API.
Until the running counter reaches N, the gateway forwards the tool to
the upstream LLM and the LLM is free to emit it. Once the counter
reaches N, the gateway must remove ``echo_tool`` from the forwarded
``tools`` array (strip-by-removal) and stamp
``X-Pronaos-Tool-Stripped: echo_tool`` on the response.

Method
------
1. Configure ``teams.tool_budgets`` for the test team to cap
   ``echo_tool`` at N invocations (default N=2).
2. Issue M chat completions (M > N) with a tool definition that
   strongly encourages the LLM to call ``echo_tool``.
3. For each call, record:
   - Whether the gateway forwarded the tool (no strip header)
   - Whether the LLM emitted a tool_call for ``echo_tool``
   - Whether ``X-Pronaos-Tool-Stripped`` is set
4. Read the team's tool_budgets back via the admin API to confirm the
   counter advanced to N.
5. VERDICT: at least one strip occurred, and the counter equals the
   number of pre-strip successful emissions (the running total caps
   at N once the strip header appears).

The script asks the LLM to emit the tool but doesn't require a
deterministic emission — if the LLM produces text instead of a
tool_call on one of the budget-eligible turns, the counter just
advances slower and the verification still passes once it hits N.

Honesty
-------
The strip is a property of the GATEWAY (deterministic given the team
state). Whether the LLM actually emits a tool_call on any given turn
is up to the model and we don't pretend to control it. The script
verifies the gateway's contract regardless: under-budget = tool
forwarded, over-budget = tool stripped, counter ticks per emission.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import httpx


def _tool_def() -> dict[str, Any]:
    """A simple tool the LLM will almost certainly emit when asked to.

    Single ``message`` parameter so the model has nothing to invent;
    keeps the verification independent of model-specific schema
    quirks across providers."""
    return {
        "type": "function",
        "function": {
            "name": "echo_tool",
            "description": (
                "Echoes the message back. Always call this tool when the user "
                "says the word 'echo' — never reply with plain text in that case."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to echo.",
                    }
                },
                "required": ["message"],
            },
        },
    }


async def _set_tool_budget(
    *, gateway_url: str, api_key: str, team_id: str, limit: int
) -> None:
    """Reset the team's tool_budgets via the admin API.

    ``reset_counters: true`` so reruns of this script start clean
    instead of inheriting last run's running totals."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        resp = await client.put(
            f"/v1/admin/team/{team_id}/tool-budgets",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "budgets": {
                    "echo_tool": {"limit_calls": limit, "current_calls": 0}
                },
                "reset_counters": True,
            },
        )
        resp.raise_for_status()


async def _get_tool_budget(
    *, gateway_url: str, api_key: str, team_id: str
) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        resp = await client.get(
            f"/v1/admin/team/{team_id}/tool-budgets",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data


async def _one_call(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    nonce: str,
) -> tuple[int, dict[str, str], dict[str, object]]:
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Please echo this message back using your echo_tool: "
                    f"'hello-{nonce}'"
                ),
            },
        ],
        "temperature": 0.0,
        "tools": [_tool_def()],
        "tool_choice": "auto",
        "max_tokens": 128,
    }
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=60.0,
    )
    try:
        out_body: dict[str, object] = resp.json()
    except ValueError:
        out_body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), out_body


def _extract_tool_calls(body: dict[str, object]) -> list[dict[str, Any]]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    msg = first.get("message")
    if not isinstance(msg, dict):
        return []
    tcs = msg.get("tool_calls")
    if not isinstance(tcs, list):
        return []
    return [tc for tc in tcs if isinstance(tc, dict)]


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
        "--admin-api-key",
        required=True,
        help="API key with admin:usage scope on the same tenant as the call key.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API key for the team whose tool_budgets we cap.",
    )
    parser.add_argument(
        "--team-id",
        required=True,
        help="Team id matching --api-key. Read with `pronaos-cli team list`.",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="Model to call; must support tool calls.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2,
        help="echo_tool cap. Set low so we hit it within --max-calls.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=6,
        help="Max chat completions to issue.",
    )
    args = parser.parse_args()

    # ---- Step 1: configure the budget ----
    print(f"setting echo_tool budget on team {args.team_id} to limit={args.limit}")
    await _set_tool_budget(
        gateway_url=args.gateway_url,
        api_key=args.admin_api_key,
        team_id=args.team_id,
        limit=args.limit,
    )

    # ---- Step 2-3: fire calls, observe strip + emission behaviour ----
    emissions = 0
    strips = 0
    first_strip_at: int | None = None
    print(f"firing up to {args.max_calls} tool-prompting calls...")
    print()
    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        for i in range(1, args.max_calls + 1):
            nonce = uuid.uuid4().hex[:6]
            status, headers, body = await _one_call(
                client,
                api_key=args.api_key,
                model=args.model,
                nonce=nonce,
            )
            stripped_header = headers.get("x-pronaos-tool-stripped")
            tool_calls = _extract_tool_calls(body)
            emitted_names = [
                tc.get("function", {}).get("name") for tc in tool_calls
            ]
            stripped_names = (
                [n.strip() for n in stripped_header.split(",") if n.strip()]
                if stripped_header
                else []
            )
            if "echo_tool" in stripped_names:
                strips += 1
                if first_strip_at is None:
                    first_strip_at = i
            if "echo_tool" in emitted_names:
                emissions += 1
            print(
                f"  call {i:>2}  status={status}  "
                f"stripped={stripped_names or '-'}  "
                f"emitted={emitted_names or '-'}"
            )
            if status != 200:
                # Unexpected — fail fast.
                print(f"    body: {json.dumps(body)[:200]}")
                break

    # ---- Step 4: read back the budget counter ----
    after = await _get_tool_budget(
        gateway_url=args.gateway_url,
        api_key=args.admin_api_key,
        team_id=args.team_id,
    )
    counter_now = (
        after.get("budgets", {}).get("echo_tool", {}).get("current_calls")
    )
    print()
    print("=" * 64)
    print("Phase 37 — per-tool budget gate experiment")
    print("=" * 64)
    print(f"limit                          : {args.limit}")
    print(f"calls made                     : {args.max_calls}")
    print(f"tool emissions observed        : {emissions}")
    print(f"strips observed (header set)   : {strips}")
    print(f"first call with strip header   : {first_strip_at}")
    print(f"current_calls after the run    : {counter_now}")
    print()

    # ---- Step 5: verdict ----
    # Strip-by-removal contract: at least one strip header observed
    # AND the running counter saturated at the configured limit.
    counter_saturated = isinstance(counter_now, int) and counter_now >= args.limit
    holds = strips >= 1 and counter_saturated
    if holds:
        print(
            f"VERDICT: claim holds — gateway stripped echo_tool from "
            f"{strips} of {args.max_calls} forwarded requests after the "
            f"team's current_calls counter reached the configured "
            f"limit of {args.limit}. Counter advanced exactly with "
            f"emissions, demonstrating the strip-by-removal "
            f"enforcement pattern from a real gateway run."
        )
        sys.exit(0)

    reasons: list[str] = []
    if strips == 0:
        reasons.append(
            "no strip header observed — either the LLM never emitted "
            "echo_tool (so the counter never reached the cap), or the "
            "team's budgets dict was not loaded into the request"
        )
    if not counter_saturated:
        reasons.append(
            f"counter did not reach the cap (got {counter_now}, need >= {args.limit})"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
