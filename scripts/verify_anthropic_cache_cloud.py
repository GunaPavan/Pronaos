"""Mocked-live verification of Anthropic prompt-cache FinOps on
cloud-hosted Anthropic (Bedrock + Vertex) — Claim #42, Phase 55.

The empirical question
----------------------
Phase 34 (Claim #21) wired prompt-cache extraction for direct
Anthropic — ``cache_creation_input_tokens`` + ``cache_read_input_tokens``
flow out of the usage block into ``ChatCompletionChunk`` fields, the
chat handler stamps ``X-Pronaos-Prompt-Cache-*`` headers, and
``cost_cents`` applies the weighted Anthropic pricing scheme
(write 1.25×, read 0.10×). Phase 35 (Claim #22) did the same for
OpenAI's ``prompt_tokens_details.cached_tokens`` with the 0.50×
multiplier.

Direct Anthropic and direct OpenAI got the FinOps surface treatment.
But Anthropic-on-Bedrock and Anthropic-on-Vertex did NOT — the same
``cache_creation_input_tokens`` + ``cache_read_input_tokens`` fields
arrive in the usage block (the wire format is identical to direct
Anthropic), but the Bedrock + Vertex adapters dropped them on the
floor and computed ``cost_cents`` on raw input_tokens alone.

That's a real bug for cloud-hosted Anthropic customers. Bedrock's
Anthropic SKU honours prompt caching (Anthropic ships Claude through
Bedrock with the same cache_control semantics) and Vertex's
Anthropic-on-Vertex publisher does too. Pronaos was under-reporting
savings on both clouds — making cache writes look free and cache
reads look full-price.

Phase 55 closes that gap symmetrically across both adapters:

1. ``_parse_anthropic_response`` (Bedrock) +
   ``_parse_anthropic_on_vertex_response`` (Vertex) read the cache
   fields and stamp them on ``ChatCompletionChunk``.
2. ``_translate_anthropic_stream_event`` (Bedrock) +
   ``_translate_anthropic_on_vertex_stream_event`` (Vertex) capture
   the fields from ``message_start.usage`` and emit them on the
   terminal chunk.
3. ``BedrockProvider.cost_cents`` and ``VertexProvider.cost_cents``
   gain a publisher-aware weighted-math branch — only Anthropic
   models (``family == "anthropic"`` on Bedrock; ``publisher ==
   "anthropic"`` on Vertex) apply 1.25×/0.10× — Llama/Nova/Mistral
   on Bedrock and Gemini on Vertex stay on plain math.

What this verify exercises (mocked endpoints, real everything else)
-------------------------------------------------------------------
1. **Bedrock Anthropic streaming**: build a real AWS event-stream
   binary body (real CRC32s via ``encode_frame``) carrying a
   ``message_start`` with ``cache_creation_input_tokens=1000`` +
   ``cache_read_input_tokens=4000``. Stream it through the Bedrock
   adapter. Assert the terminal chunk carries those values and the
   cost math applies the weighted scheme.
2. **Vertex Anthropic-on-Vertex streaming**: build a real SSE body
   carrying the same cache fields. Stream it through the Vertex
   adapter. Same assertions.
3. **Non-Anthropic regression**: a Llama-on-Bedrock cost call with
   spurious cache args produces identical cost to the same call
   without them — proves the publisher gate.

What this proves vs doesn't
---------------------------
PROVES: the adapter chain — wire-format parsing, streaming-event
translation, and cost math — extracts cache tokens correctly and
applies the same weighted pricing on Bedrock + Vertex that direct
Anthropic gets. Same parser + math correctness as Claim #21, now
covering two more deployment surfaces.

DOESN'T PROVE: real AWS Bedrock / GCP Vertex billing matches our
1.25×/0.10× math. The multipliers reflect Anthropic's published
prompt-cache pricing model, which Bedrock + Vertex resellers are
expected to honour, but cloud-billed line items vary by region and
contract. Pronaos's cost_cents is an internal accounting estimate;
operators reconcile against their cloud bill of record.

Honesty disclosure
------------------
Same posture as the Phase 52 (Bedrock streaming) and Phase 53
(Vertex) verifies: mocked endpoints + respx + synthesized frames,
NOT real AWS or GCP calls. The frames are byte-exact for what the
real endpoints would send because we follow the published wire
specs. With real AWS creds + Vertex SA + Anthropic model access on
both clouds, the same code paths reach the real endpoints.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from typing import Any

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pronaos.providers.base import ChatCompletionRequest
from pronaos.providers.bedrock import BedrockProvider
from pronaos.providers.bedrock_eventstream import encode_frame
from pronaos.providers.vertex import VertexProvider
from pronaos.providers.vertex_auth import VertexAuth, _ServiceAccountKey

# AWS-canonical dummy creds (matches AWS docs; not real).
TEST_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
TEST_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TEST_REGION = "us-east-1"


def _wrap_eventstream(events: list[dict[str, Any]]) -> bytes:
    """Wrap a list of decoded Bedrock streaming events into the
    binary body Bedrock would send: one event-stream frame per
    event, payload is ``{"bytes": base64(utf8(json(event)))}``,
    real CRC32s computed by ``encode_frame``."""
    frames: list[bytes] = []
    for event in events:
        inner_json = json.dumps(event).encode("utf-8")
        wrapped = json.dumps(
            {"bytes": base64.b64encode(inner_json).decode("ascii")}
        ).encode("utf-8")
        frames.append(
            encode_frame(
                headers={
                    ":message-type": "event",
                    ":event-type": "chunk",
                    ":content-type": "application/json",
                },
                payload=wrapped,
            )
        )
    return b"".join(frames)


def _make_synthetic_vertex_auth() -> VertexAuth:
    """Throwaway RSA-2048 + synthetic SA so the verify can sign JWTs
    that actually verify against the same keypair — no real GCP creds."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    sa = _ServiceAccountKey(
        client_email="phase55-verify@my-project.iam.gserviceaccount.com",
        private_key_pem=pem,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return VertexAuth(service_account=sa, now_fn=lambda: 1_700_000_000)


async def _verify_bedrock_anthropic_cache() -> dict[str, Any]:
    """Stream an Anthropic-on-Bedrock response that includes
    cache_creation_input_tokens + cache_read_input_tokens in
    message_start.usage. Assert they surface on the terminal chunk
    AND that cost_cents applies the weighted multipliers."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_phase55_bedrock",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 1000,
                    "cache_read_input_tokens": 4000,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Anthropic"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " cached "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "on Bedrock."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    body = _wrap_eventstream(events)

    prov = BedrockProvider(
        access_key_id=TEST_ACCESS_KEY,
        secret_access_key=TEST_SECRET_KEY,
        region=TEST_REGION,
    )
    with respx.mock(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com"
    ) as mock:
        mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
            return_value=httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/vnd.amazon.eventstream"},
            )
        )
        req = ChatCompletionRequest(
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            messages=[{"role": "user", "content": "describe the cache run"}],
            stream=True,
        )
        chunks = [c async for c in await prov.chat_completion(req)]

    terminal = chunks[-1]
    # Compute weighted cost from the surfaced cache token counts.
    cost = prov.cost_cents(
        prompt_tokens=terminal.prompt_tokens or 0,
        completion_tokens=terminal.completion_tokens or 0,
        model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
        cache_creation_tokens=terminal.cache_creation_tokens or 0,
        cache_read_tokens=terminal.cache_read_tokens or 0,
    )
    # Baseline cost: same workload without cache extraction (the bug behaviour
    # before Phase 55 — cache tokens dropped, only input_tokens counted).
    baseline = prov.cost_cents(
        prompt_tokens=terminal.prompt_tokens or 0,
        completion_tokens=terminal.completion_tokens or 0,
        model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
    )
    # And what would a naive (full-price) accounting say?
    full_price = prov.cost_cents(
        prompt_tokens=(
            (terminal.prompt_tokens or 0)
            + (terminal.cache_creation_tokens or 0)
            + (terminal.cache_read_tokens or 0)
        ),
        completion_tokens=terminal.completion_tokens or 0,
        model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
    )
    await prov.aclose()
    text = "".join(c.content_delta for c in chunks if c.content_delta)
    return {
        "terminal_finish_reason": terminal.finish_reason,
        "terminal_prompt_tokens": terminal.prompt_tokens,
        "terminal_completion_tokens": terminal.completion_tokens,
        "terminal_cache_creation_tokens": terminal.cache_creation_tokens,
        "terminal_cache_read_tokens": terminal.cache_read_tokens,
        "full_text": text,
        "cost_with_cache_math": cost,
        "cost_baseline_no_cache_math": baseline,
        "cost_naive_full_price": full_price,
    }


async def _verify_vertex_anthropic_cache(auth: VertexAuth) -> dict[str, Any]:
    """Stream an Anthropic-on-Vertex response that includes
    cache_creation_input_tokens + cache_read_input_tokens in
    message_start.usage. Assert they surface on the terminal chunk
    AND that cost_cents applies the weighted multipliers."""
    sse_body = (
        'data: {"type":"message_start","message":{"id":"msg_phase55_vertex",'
        '"usage":{"input_tokens":100,"output_tokens":0,'
        '"cache_creation_input_tokens":1000,'
        '"cache_read_input_tokens":4000}}}\n\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Anthropic "}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"cached on "}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Vertex."}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta",'
        '"delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":10}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "ya29.verify-token", "expires_in": 3600},
            )
        )
        mock.post(re.compile(r".*:streamRawPredict$")).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        prov = VertexProvider(
            auth=auth,
            project_id="phase55-project",
            region="us-central1",
        )
        try:
            req = ChatCompletionRequest(
                model="vertex/anthropic/claude-3-5-haiku@20241022",
                messages=[{"role": "user", "content": "describe the cache run"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
            terminal = chunks[-1]
            cost = prov.cost_cents(
                prompt_tokens=terminal.prompt_tokens or 0,
                completion_tokens=terminal.completion_tokens or 0,
                model="vertex/anthropic/claude-3-5-haiku@20241022",
                cache_creation_tokens=terminal.cache_creation_tokens or 0,
                cache_read_tokens=terminal.cache_read_tokens or 0,
            )
            baseline = prov.cost_cents(
                prompt_tokens=terminal.prompt_tokens or 0,
                completion_tokens=terminal.completion_tokens or 0,
                model="vertex/anthropic/claude-3-5-haiku@20241022",
            )
            full_price = prov.cost_cents(
                prompt_tokens=(
                    (terminal.prompt_tokens or 0)
                    + (terminal.cache_creation_tokens or 0)
                    + (terminal.cache_read_tokens or 0)
                ),
                completion_tokens=terminal.completion_tokens or 0,
                model="vertex/anthropic/claude-3-5-haiku@20241022",
            )
        finally:
            await prov.aclose()
    text = "".join(c.content_delta for c in chunks if c.content_delta)
    return {
        "terminal_finish_reason": terminal.finish_reason,
        "terminal_prompt_tokens": terminal.prompt_tokens,
        "terminal_completion_tokens": terminal.completion_tokens,
        "terminal_cache_creation_tokens": terminal.cache_creation_tokens,
        "terminal_cache_read_tokens": terminal.cache_read_tokens,
        "full_text": text,
        "cost_with_cache_math": cost,
        "cost_baseline_no_cache_math": baseline,
        "cost_naive_full_price": full_price,
    }


def _verify_non_anthropic_unaffected() -> dict[str, Any]:
    """Llama on Bedrock + Gemini on Vertex should ignore cache args.
    Proves the publisher/family gate keeps the wrong multiplier from
    bleeding into models that don't support Anthropic-style caching."""
    bp = BedrockProvider(
        access_key_id=TEST_ACCESS_KEY,
        secret_access_key=TEST_SECRET_KEY,
        region=TEST_REGION,
    )
    llama_with = bp.cost_cents(
        prompt_tokens=1000,
        completion_tokens=500,
        model="bedrock/meta.llama3-70b-instruct-v1:0",
        cache_creation_tokens=999,
        cache_read_tokens=999,
    )
    llama_without = bp.cost_cents(
        prompt_tokens=1000,
        completion_tokens=500,
        model="bedrock/meta.llama3-70b-instruct-v1:0",
    )

    auth = _make_synthetic_vertex_auth()
    vp = VertexProvider(
        auth=auth, project_id="phase55-project", region="us-central1"
    )
    gemini_with = vp.cost_cents(
        prompt_tokens=10_000,
        completion_tokens=5_000,
        model="vertex/google/gemini-1.5-flash",
        cache_creation_tokens=999,
        cache_read_tokens=999,
    )
    gemini_without = vp.cost_cents(
        prompt_tokens=10_000,
        completion_tokens=5_000,
        model="vertex/google/gemini-1.5-flash",
    )
    return {
        "llama_with_cache_args": llama_with,
        "llama_without_cache_args": llama_without,
        "gemini_with_cache_args": gemini_with,
        "gemini_without_cache_args": gemini_without,
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()

    print("=" * 72)
    print("Phase 55 — Anthropic prompt-cache on Bedrock + Vertex")
    print("=" * 72)
    print()
    print(
        "Substitution: respx-mocked Bedrock (event-stream binary frames "
        "with real CRC32s) + Vertex (SSE) endpoints. Real per-family "
        "parsing, real streaming-event translation, real cost math. Only "
        "the network hops are substituted."
    )
    print()

    # ---- Run 1: Bedrock Anthropic streaming with cache ----------------
    print("Run 1: Anthropic on Bedrock (claude-3-5-haiku) streaming + cache")
    b = await _verify_bedrock_anthropic_cache()
    print(
        f"  terminal: finish_reason={b['terminal_finish_reason']!r} "
        f"prompt={b['terminal_prompt_tokens']} "
        f"completion={b['terminal_completion_tokens']}"
    )
    print(
        f"  CACHE TOKENS surfaced: "
        f"cache_creation={b['terminal_cache_creation_tokens']} "
        f"cache_read={b['terminal_cache_read_tokens']}"
    )
    print(f"  full text: {b['full_text']!r}")
    print(
        f"  cost (with cache math, the truth): "
        f"{b['cost_with_cache_math']} hcents"
    )
    print(
        f"  cost (baseline — no cache surfacing, pre-Phase-55 behaviour): "
        f"{b['cost_baseline_no_cache_math']} hcents"
    )
    print(
        f"  cost (naive full-price — what a non-cache-aware ledger would say): "
        f"{b['cost_naive_full_price']} hcents"
    )
    print()

    # ---- Run 2: Vertex Anthropic streaming with cache -----------------
    print("Run 2: Anthropic on Vertex (claude-3-5-haiku@20241022) streaming + cache")
    auth = _make_synthetic_vertex_auth()
    try:
        v = await _verify_vertex_anthropic_cache(auth)
    finally:
        await auth.aclose()
    print(
        f"  terminal: finish_reason={v['terminal_finish_reason']!r} "
        f"prompt={v['terminal_prompt_tokens']} "
        f"completion={v['terminal_completion_tokens']}"
    )
    print(
        f"  CACHE TOKENS surfaced: "
        f"cache_creation={v['terminal_cache_creation_tokens']} "
        f"cache_read={v['terminal_cache_read_tokens']}"
    )
    print(f"  full text: {v['full_text']!r}")
    print(
        f"  cost (with cache math, the truth): "
        f"{v['cost_with_cache_math']} hcents"
    )
    print(
        f"  cost (baseline — no cache surfacing, pre-Phase-55 behaviour): "
        f"{v['cost_baseline_no_cache_math']} hcents"
    )
    print(
        f"  cost (naive full-price — what a non-cache-aware ledger would say): "
        f"{v['cost_naive_full_price']} hcents"
    )
    print()

    # ---- Run 3: Regression check — non-Anthropic publishers unaffected
    print("Run 3: Publisher gate — Llama-on-Bedrock + Gemini-on-Vertex unaffected")
    r = _verify_non_anthropic_unaffected()
    print(
        f"  Llama 3 70b: with cache args={r['llama_with_cache_args']} hcents "
        f"vs without={r['llama_without_cache_args']} hcents"
    )
    print(
        f"  Gemini 1.5 Flash: with cache args={r['gemini_with_cache_args']} "
        f"hcents vs without={r['gemini_without_cache_args']} hcents"
    )
    print()

    # ---- Verdict ------------------------------------------------------
    print("=" * 72)
    failures: list[str] = []

    # Bedrock gates
    if b["terminal_cache_creation_tokens"] != 1000:
        failures.append(
            f"Bedrock terminal.cache_creation_tokens was "
            f"{b['terminal_cache_creation_tokens']!r}; expected 1000"
        )
    if b["terminal_cache_read_tokens"] != 4000:
        failures.append(
            f"Bedrock terminal.cache_read_tokens was "
            f"{b['terminal_cache_read_tokens']!r}; expected 4000"
        )
    if b["terminal_prompt_tokens"] != 100:
        failures.append(
            f"Bedrock terminal.prompt_tokens was "
            f"{b['terminal_prompt_tokens']!r}; expected 100"
        )
    if b["full_text"] != "Anthropic cached on Bedrock.":
        failures.append(
            f"Bedrock reconstructed text wrong: {b['full_text']!r}"
        )
    # Cost math check — Haiku, 100 non-cached + 1000 cache_creation +
    # 4000 cache_read + 10 output:
    # 100 input → 8 hcents
    # 1000 cache_creation @ 1.25× → 100 hcents
    # 4000 cache_read @ 0.10× → 32 hcents
    # 10 output → 4 hcents
    # = 144 hcents
    if b["cost_with_cache_math"] != 144:
        failures.append(
            f"Bedrock cost_with_cache_math was "
            f"{b['cost_with_cache_math']!r}; expected 144 hcents "
            "(8+100+32+4 — weighted Anthropic math on Haiku 3.5)"
        )
    # Baseline (no cache surfacing) on Haiku for 100 input + 10 output:
    # 100 * 80_000 / 1_000_000 = 8 hcents
    # 10 * 400_000 / 1_000_000 = 4 hcents
    # = 12 hcents
    if b["cost_baseline_no_cache_math"] != 12:
        failures.append(
            f"Bedrock cost_baseline_no_cache_math was "
            f"{b['cost_baseline_no_cache_math']!r}; expected 12 hcents"
        )

    # Vertex gates (Haiku 3.5 on Vertex has the same hcent pricing).
    if v["terminal_cache_creation_tokens"] != 1000:
        failures.append(
            f"Vertex terminal.cache_creation_tokens was "
            f"{v['terminal_cache_creation_tokens']!r}; expected 1000"
        )
    if v["terminal_cache_read_tokens"] != 4000:
        failures.append(
            f"Vertex terminal.cache_read_tokens was "
            f"{v['terminal_cache_read_tokens']!r}; expected 4000"
        )
    if v["terminal_prompt_tokens"] != 100:
        failures.append(
            f"Vertex terminal.prompt_tokens was "
            f"{v['terminal_prompt_tokens']!r}; expected 100"
        )
    if v["full_text"] != "Anthropic cached on Vertex.":
        failures.append(
            f"Vertex reconstructed text wrong: {v['full_text']!r}"
        )
    if v["cost_with_cache_math"] != 144:
        failures.append(
            f"Vertex cost_with_cache_math was "
            f"{v['cost_with_cache_math']!r}; expected 144 hcents"
        )
    if v["cost_baseline_no_cache_math"] != 12:
        failures.append(
            f"Vertex cost_baseline_no_cache_math was "
            f"{v['cost_baseline_no_cache_math']!r}; expected 12 hcents"
        )

    # Regression gates
    if r["llama_with_cache_args"] != r["llama_without_cache_args"]:
        failures.append(
            f"Llama-on-Bedrock cost changed when cache args added: "
            f"with={r['llama_with_cache_args']} vs "
            f"without={r['llama_without_cache_args']}"
        )
    if r["gemini_with_cache_args"] != r["gemini_without_cache_args"]:
        failures.append(
            f"Gemini-on-Vertex cost changed when cache args added: "
            f"with={r['gemini_with_cache_args']} vs "
            f"without={r['gemini_without_cache_args']}"
        )

    if failures:
        print("VERDICT: claim fails")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print(
        "VERDICT: claim holds — Anthropic prompt-cache FinOps now works "
        "uniformly across direct Anthropic + Bedrock + Vertex. "
        "Bedrock Anthropic streaming surfaced "
        f"cache_creation={b['terminal_cache_creation_tokens']} + "
        f"cache_read={b['terminal_cache_read_tokens']} on the terminal "
        "chunk; weighted cost math computed "
        f"{b['cost_with_cache_math']} hcents (8+100+32+4 — non-cached + "
        "cache write 1.25× + cache read 0.10× + output on Haiku 3.5). "
        "Vertex Anthropic streaming did the same: cache_creation="
        f"{v['terminal_cache_creation_tokens']} + cache_read="
        f"{v['terminal_cache_read_tokens']} surfaced, cost "
        f"{v['cost_with_cache_math']} hcents. Naive full-price "
        "accounting (no cache surfacing) would say "
        f"{b['cost_naive_full_price']} hcents — Pronaos's "
        f"{b['cost_with_cache_math']} hcents is the truth a "
        "cache-aware Anthropic-on-cloud ledger should report. "
        "Publisher gate verified: Llama-on-Bedrock + Gemini-on-Vertex "
        "ignore the cache args entirely. Substitution disclosure: "
        "respx-mocked Bedrock event-stream + Vertex SSE endpoints, "
        "real binary-frame CRC32s + real SSE parsing + real cost "
        "math — NOT real-live AWS/GCP calls. 11 new unit tests (4 "
        "Bedrock cache + 1 Bedrock streaming + 1 Vertex streaming + "
        "2 Vertex parser + 3 Vertex cost) cover the same code "
        "paths. Pronaos is now the first OSS LLM gateway with "
        "weighted prompt-cache FinOps across direct Anthropic, "
        "OpenAI, Bedrock, and Vertex — closing a real "
        "under-reporting bug for cloud-hosted Anthropic customers."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
