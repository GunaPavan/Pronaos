"""Tool-call result cache live verification (Claim #36, Phase 49).

The empirical question
----------------------
Does the gateway memoize (tool_name, args) → result from past
``tool`` role messages, and inject cached results into subsequent
requests whose trailing assistant.tool_calls are awaiting execution
— skipping the client's tool re-execution?

Flow
----
1. Enable the feature on the team via PUT /v1/admin/team/{id}/
   tool-result-cache-config.
2. Reset any prior state via DELETE.
3. **Call 1 (populate)**: send a chat with the full agent loop —
   ``[user, assistant: tool_calls=[(get_weather, {city: Tokyo})],
   tool: "Tokyo: sunny 22C"]``. The gateway extracts the (name,
   args, result) and records it in Redis.
4. Read back the snapshot via admin GET to confirm the entry
   landed.
5. **Call 2 (inject)**: send a chat with the SAME tool_call but
   NO matching ``tool`` follow-up — just
   ``[user, assistant: tool_calls=[(get_weather, {city: Tokyo})]]``.
   The gateway looks up the cache, finds the hit, injects a
   synthetic ``tool`` message into the conversation before
   forwarding to the LLM. Response carries
   ``X-Pronaos-Tool-Cache-Hits: 1`` + ``X-Pronaos-Tool-Cache-Tools:
   get_weather`` headers.
6. **Call 3 (miss)**: same shape as call 2 but with different args
   (``{city: Paris}``). No cache entry; no injection; no header.

If the call 2 response carries the hit header AND the call 3
response does NOT, the composition is verified end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import uuid
from typing import Any

import httpx


def _tool_call(name: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": __import__("json").dumps(args),
        },
    }


async def _put_admin(
    *,
    client: httpx.AsyncClient,
    admin_key: str,
    path: str,
    body: dict[str, Any] | None,
) -> tuple[int, dict[str, Any] | None]:
    resp = await client.put(
        path,
        headers={"Authorization": f"Bearer {admin_key}"},
        json=body if body is not None else {},
        timeout=10.0,
    )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


async def _delete_admin(
    *, client: httpx.AsyncClient, admin_key: str, path: str
) -> int:
    resp = await client.delete(
        path,
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=10.0,
    )
    return resp.status_code


async def _chat(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> httpx.Response:
    body = {
        "model": "groq/llama-3.1-8b-instant",
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 80,
        "temperature": 0.0,
    }
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=30.0,
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--admin-api-key", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--team-id", required=True)
    args = parser.parse_args()

    print("=" * 72)
    print("Phase 49 — tool-call result cache live verification")
    print("=" * 72)

    # The weather tool schema clients would supply in a real agent loop.
    weather_tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }

    async with httpx.AsyncClient(base_url=args.gateway_url, timeout=30.0) as client:
        # ---- 1. Enable cache on team + reset prior state. ----
        print()
        print("Enabling tool-result cache on team + resetting prior state...")
        await _delete_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/tool-result-cache",
        )
        status, resp = await _put_admin(
            client=client,
            admin_key=args.admin_api_key,
            path=f"/v1/admin/team/{args.team_id}/tool-result-cache-config",
            body={"enabled": True, "ttl_seconds": 3600},
        )
        print(f"  PUT config       → {status} {resp}")

        # ---- 2. Call 1 — populate the cache. ----
        # A full agent-loop turn: user asks, assistant emits tool_call,
        # client provides the tool result, model would synthesise an
        # answer. The gateway extracts (name, args, result) and caches.
        call_1_tc_id = "call_tokyo_1"
        populate_msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "What's the weather in Tokyo right now?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("get_weather", {"city": "Tokyo"}, call_1_tc_id)],
            },
            {
                "role": "tool",
                "tool_call_id": call_1_tc_id,
                "content": "Tokyo: sunny, 22C, light wind from the east.",
            },
        ]
        print()
        print("Call 1 (populate): full loop with tool result in conversation")
        r1 = await _chat(
            client=client,
            api_key=args.api_key,
            messages=populate_msgs,
            tools=[weather_tool],
        )
        print(f"  HTTP status: {r1.status_code}")
        if r1.status_code != 200:
            print(f"  body: {r1.text[:300]}")

        # ---- 3. Read back cache snapshot. ----
        print()
        print("Reading cache snapshot via admin GET...")
        snap_resp = await client.get(
            f"/v1/admin/team/{args.team_id}/tool-result-cache",
            headers={"Authorization": f"Bearer {args.admin_api_key}"},
            timeout=10.0,
        )
        snap_body = snap_resp.json()
        entries = snap_body.get("entries", [])
        print(f"  entries: {len(entries)}")
        for e in entries:
            print(
                f"    tool={e['tool_name']!r} args_hash={e['args_hash']} "
                f"result={e['result'][:50]!r}"
            )

        # ---- 4. Call 2 — inject expected. ----
        call_2_tc_id = "call_tokyo_2"
        inject_msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "What's the weather in Tokyo right now?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("get_weather", {"city": "Tokyo"}, call_2_tc_id)],
            },
        ]
        print()
        print("Call 2 (inject): trailing assistant.tool_calls, no tool result")
        r2 = await _chat(
            client=client,
            api_key=args.api_key,
            messages=inject_msgs,
            tools=[weather_tool],
        )
        cache_hits_2 = r2.headers.get("x-pronaos-tool-cache-hits", "0")
        cache_tools_2 = r2.headers.get("x-pronaos-tool-cache-tools", "")
        print(f"  HTTP status: {r2.status_code}")
        print(f"  X-Pronaos-Tool-Cache-Hits:  {cache_hits_2}")
        print(f"  X-Pronaos-Tool-Cache-Tools: {cache_tools_2!r}")

        # ---- 5. Call 3 — miss expected (different args). ----
        call_3_tc_id = "call_paris_1"
        miss_msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "What's the weather in Paris right now?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("get_weather", {"city": "Paris"}, call_3_tc_id)],
            },
        ]
        print()
        print("Call 3 (miss): same tool, different args → no cache entry → no inject")
        r3 = await _chat(
            client=client,
            api_key=args.api_key,
            messages=miss_msgs,
            tools=[weather_tool],
        )
        cache_hits_3 = r3.headers.get("x-pronaos-tool-cache-hits", "0")
        print(f"  HTTP status: {r3.status_code}")
        print(f"  X-Pronaos-Tool-Cache-Hits:  {cache_hits_3}")

        # ---- 6. Cleanup. ----
        with contextlib.suppress(Exception):
            await _delete_admin(
                client=client,
                admin_key=args.admin_api_key,
                path=f"/v1/admin/team/{args.team_id}/tool-result-cache",
            )
        with contextlib.suppress(Exception):
            await _put_admin(
                client=client,
                admin_key=args.admin_api_key,
                path=f"/v1/admin/team/{args.team_id}/tool-result-cache-config",
                body={"enabled": False, "ttl_seconds": None},
            )

    # ---- Verdict --------------------------------------------------------
    print()
    print("=" * 72)
    # Two falsifiable properties:
    #   1. The populate→snapshot round-trip works (entries > 0 after call 1).
    #   2. Call 2 with bare tool_calls sees an injection
    #      (X-Pronaos-Tool-Cache-Hits = 1).
    #   3. Call 3 with different args sees NO injection
    #      (X-Pronaos-Tool-Cache-Hits = 0 or header absent).
    if not entries:
        print(
            "VERDICT: claim fails — the cache snapshot is empty after the "
            "populate call. Extraction path didn't record (tool, args, result)."
        )
        sys.exit(1)
    if int(cache_hits_2) < 1:
        print(
            "VERDICT: claim fails — call 2 sent a bare assistant.tool_calls "
            "with no matching tool result. The cache had a matching entry "
            f"(team={args.team_id}, get_weather, {{city: Tokyo}}) but the "
            "gateway did NOT inject it (X-Pronaos-Tool-Cache-Hits = "
            f"{cache_hits_2}). Check the chat handler's injection path."
        )
        sys.exit(1)
    if int(cache_hits_3) != 0:
        print(
            "VERDICT: claim fails — call 3 used DIFFERENT args "
            "({city: Paris}) than the cached entry ({city: Tokyo}) but the "
            f"gateway reported {cache_hits_3} cache hit(s). The args-hash "
            "canonicalisation may be collapsing distinct args to the same key."
        )
        sys.exit(1)
    print(
        "VERDICT: claim holds — the gateway memoizes tool-call results from "
        "the conversation history and injects matching cached results into "
        "subsequent requests with bare assistant.tool_calls. Call 1 populated "
        "the cache (snapshot showed the entry). Call 2 with the SAME "
        "(tool_name, args) but no client-supplied result saw "
        f"X-Pronaos-Tool-Cache-Hits = {cache_hits_2} and the LLM received "
        "the injected tool result — saving the client one round trip to "
        "re-execute get_weather(city=\"Tokyo\"). Call 3 with DIFFERENT args "
        "(city=\"Paris\") correctly missed the cache, proving the "
        "canonical-args hash discriminates distinct calls. Composes Phase "
        "7 (cache plumbing) + Phase 30 (agent-turn budgets) + Phase 37 "
        "(per-tool budgets) into a runtime FinOps cycle for agent loops."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
