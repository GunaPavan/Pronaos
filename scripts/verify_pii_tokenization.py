"""Live verification of reversible PII tokenization (Claim #25, Phase 38).

The empirical question
----------------------
A team has ``pii_tokenization_enabled = True`` and a
``guardrail_policy.rule_actions`` entry mapping ``pii.email`` to
``"tokenize"``. The client sends a prompt containing an email and
asks the LLM to confirm receipt of that address.

Three things must hold simultaneously:

1. The upstream provider's wire body contains the TOKEN
   (``[EMAIL_a3f7c2e1b890]``), not the original email. Compliance
   perimeter preserved.
2. The client's response body contains the ORIGINAL email back.
   The gateway reversed the LLM's echo of the token.
3. The ``X-Pronaos-PII-Reversed`` response header reports at least
   one reversal, confirming this isn't accidental string match.

Method
------
1. Set team's ``pii_tokenization_enabled = True``, TTL = 600s.
2. Set team's ``guardrail_policy.rule_actions`` to
   ``{"pii.email": "tokenize"}``.
3. Fire a chat completion with a prompt asking the LLM to confirm
   it received the email + echo it back in its reply.
4. Capture upstream wire body (via gateway-side prometheus / log
   inspection isn't possible; we use a side-by-side run against a
   real upstream where the LLM's behaviour is the proxy for "the
   upstream saw the token" — the LLM mentions ``[EMAIL_...]`` in
   its reply, the gateway reverses it, the client sees the
   original. If any step fails, the assertion fires.

Honesty
-------
The verification depends on the LLM echoing the token in its reply
— which Groq's llama-3.1-8b-instant + similar models do reliably
when asked to confirm an address. We can't directly inspect the
upstream wire body from a CLI script without a debug proxy. The
chain "client sees original BACK after sending it + LLM saw and
echoed the placeholder shape" is the strongest end-to-end signal
available without running gateway-side network capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

import httpx


async def _setup_team(
    *, gateway_url: str, admin_key: str, team_id: str, ttl: int
) -> None:
    """Enable tokenization + set the per-rule policy."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        # 1. Flip the master switch.
        r1 = await client.put(
            f"/v1/admin/team/{team_id}/pii-tokenization",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"enabled": True, "ttl_seconds": ttl},
        )
        r1.raise_for_status()
        # 2. Set the per-rule action via the existing guardrail-policy
        # endpoint. Body shape: top-level keys, not wrapped in {policy}.
        r2 = await client.put(
            f"/v1/admin/team/{team_id}/guardrail-policy",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "rule_actions": {
                    "pii.email": "tokenize",
                }
            },
        )
        r2.raise_for_status()


async def _chat(
    *,
    gateway_url: str,
    api_key: str,
    model: str,
    email: str,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Send a prompt that asks the LLM to repeat the email back."""
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "I need you to echo back the placeholder string in the next "
                    "sentence EXACTLY as written, character for character, with "
                    "no commentary. Here is the placeholder you must repeat: "
                    f"{email} . Respond with one short sentence that contains "
                    "the placeholder verbatim."
                ),
            }
        ],
        "temperature": 0.0,
        "max_tokens": 60,
    }
    async with httpx.AsyncClient(base_url=gateway_url, timeout=60.0) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    try:
        out_body: dict[str, Any] = resp.json()
    except ValueError:
        out_body = {"_raw": resp.text}
    return resp.status_code, dict(resp.headers), out_body


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--admin-api-key",
        required=True,
        help="API key with admin:usage scope on this tenant.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API key with chat:write for the team under test.",
    )
    parser.add_argument(
        "--team-id",
        required=True,
        help="Team id matching --api-key.",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help="Model to call; any tool-capable model works.",
    )
    parser.add_argument(
        "--email",
        default="alice@pronaos.example",
        help="The email to embed in the prompt.",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=600,
        help="Token TTL in seconds (default 600).",
    )
    args = parser.parse_args()

    print(
        f"setting team {args.team_id}: pii_tokenization_enabled=True, "
        f"rule_actions.pii.email=tokenize, ttl={args.ttl}s"
    )
    await _setup_team(
        gateway_url=args.gateway_url,
        admin_key=args.admin_api_key,
        team_id=args.team_id,
        ttl=args.ttl,
    )

    print(f"firing chat completion with email {args.email!r} in the prompt...")
    print()
    status, headers, body = await _chat(
        gateway_url=args.gateway_url,
        api_key=args.api_key,
        model=args.model,
        email=args.email,
    )
    if status != 200:
        print(f"unexpected status {status}: {json.dumps(body)[:300]}")
        sys.exit(1)

    content = ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = msg.get("content") or ""

    guardrails_header = headers.get("x-pronaos-guardrails", "")
    reversed_count = headers.get("x-pronaos-pii-reversed", "0")
    orphaned_count = headers.get("x-pronaos-pii-orphaned", "0")
    # Detect any leftover token shape in the client response — must be 0.
    leftover_tokens = re.findall(r"\[EMAIL_[a-f0-9]{12}\]", content)

    print("Response content:")
    print(f"  {content!r}")
    print()
    print("Headers:")
    print(f"  X-Pronaos-Guardrails:  {guardrails_header!r}")
    print(f"  X-Pronaos-PII-Reversed: {reversed_count}")
    print(f"  X-Pronaos-PII-Orphaned: {orphaned_count}")
    print(f"  leftover [EMAIL_xxx] tokens in body: {len(leftover_tokens)}")

    print()
    print("=" * 64)
    print("Phase 38 — reversible PII tokenization experiment")
    print("=" * 64)
    print(f"prompt contained email:           {args.email}")
    print(f"client response contains email:   {args.email in content}")
    print(f"client response has leftover token: {len(leftover_tokens) > 0}")
    print(f"X-Pronaos-Guardrails marker:      {guardrails_header}")
    print(f"X-Pronaos-PII-Reversed:           {reversed_count}")
    print()

    # ---- Verdict ----
    holds = (
        args.email in content
        and len(leftover_tokens) == 0
        and "tokenized:" in guardrails_header
        and int(reversed_count) >= 1
    )
    if holds:
        print(
            f"VERDICT: claim holds — gateway tokenized {args.email!r} on the "
            "ingress path (X-Pronaos-Guardrails header carries the "
            "'tokenized:' marker proving the engine took the tokenize "
            "branch, not the redact branch). The upstream LLM saw only "
            "the deterministic placeholder; its reply mentioned the "
            "placeholder back; the gateway reversed the placeholder so "
            "the client sees the original email in the final response. "
            "Information flow preserved end-to-end while compliance "
            "perimeter held."
        )
        sys.exit(0)

    reasons: list[str] = []
    if args.email not in content:
        reasons.append(
            "client response does NOT contain the original email — "
            "tokenization may have succeeded but reversal failed, "
            "OR the LLM didn't echo the placeholder (try a more "
            "instructive prompt with --email)"
        )
    if leftover_tokens:
        reasons.append(
            f"client response still contains {len(leftover_tokens)} "
            "raw [EMAIL_xxx] token(s) — reversal didn't run or Redis "
            "had no mapping (check the TTL)"
        )
    if "tokenized:" not in guardrails_header:
        reasons.append(
            "X-Pronaos-Guardrails header does not carry 'tokenized:' "
            "— the engine fell back to redact (team flag off or "
            "policy missing the rule_actions entry)"
        )
    if int(reversed_count) < 1:
        reasons.append(
            "X-Pronaos-PII-Reversed is 0 — egress detokenizer didn't "
            "match any tokens, suggesting the LLM didn't echo the "
            "placeholder shape in its reply"
        )
    print(f"VERDICT: claim fails — {'; '.join(reasons)}.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
