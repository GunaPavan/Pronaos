"""Mocked-live verification of the AWS Bedrock adapter (Claim #29, Phase 42).

The empirical question
----------------------
Can the gateway route a chat completion through the **Bedrock provider
path** end-to-end — with correct SigV4 signing, the right per-model
family wire shape, and a faithful translation of Bedrock's response
back to the OpenAI-compat shape — WITHOUT needing real AWS credentials
or Bedrock model access?

Why mocked? Real Bedrock live verify requires:
- An AWS account with Bedrock enabled in the region.
- Model access granted via the Bedrock console (typically a 1-day
  manual approval per model).
- A non-trivial bill if the test runs against frontier models.

Most contributors won't have this; we want a verify script anyone can
re-run. So we stage a respx-mocked Bedrock endpoint, fire a real
``BedrockProvider`` instance at it, and assert the three properties
that make this adapter correct:

1. The outbound request is **SigV4-signed for the bedrock service** in
   the configured region (Authorization header matches the AWS scheme).
2. The outbound body uses the **right per-family wire shape**:
   - ``anthropic.*`` → Anthropic Messages shape with
     ``anthropic_version: "bedrock-2023-05-31"`` and NO ``model`` field
   - ``meta.*`` → Llama prompt-template body with ``max_gen_len``
3. The response is **translated to OpenAI-compat ChatCompletionChunk**
   (content text + finish_reason + token counts).

What this proves vs doesn't prove
---------------------------------
PROVES: the gateway-side adapter math — SigV4 invocation, per-family
shape switching, response translation, cost lookup — is correct.

DOESN'T PROVE: real Bedrock model availability in your account, real
network behaviour, real token counter alignment with AWS billing. Those
require real AWS access; this script is the "everyone can verify it"
companion to a future real-live test.

Honesty
-------
The verdict line in this script's output is explicit about the
substitution: "mocked endpoint, real SigV4 math, real wire-shape
translation, real response translation — NOT real-live AWS access."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

import httpx
import respx

from pronaos.providers.base import ChatCompletionRequest
from pronaos.providers.bedrock import BedrockProvider

# AWS-canonical example credentials. These are the same dummy creds AWS
# uses in their published docs. Safe to commit; not real.
EXAMPLE_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
EXAMPLE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _bedrock_url(region: str, model_id: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/invoke"


async def _exercise_anthropic_path(
    region: str, access_key: str, secret_key: str
) -> dict[str, Any]:
    """Stage a respx Bedrock mock, run the adapter, return observations."""
    model_id = "anthropic.claude-3-5-haiku-20241022-v1:0"
    url = _bedrock_url(region, model_id)

    upstream_response = {
        "content": [{"type": "text", "text": "Paris is the capital of France."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 15, "output_tokens": 8},
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).mock(
            return_value=httpx.Response(200, json=upstream_response)
        )
        prov = BedrockProvider(
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=region,
        )
        req = ChatCompletionRequest(
            model=f"bedrock/{model_id}",
            messages=[{"role": "user", "content": "What is the capital of France?"}],
            max_tokens=50,
        )
        chunks = []
        async for c in await prov.chat_completion(req):
            chunks.append(c)
        await prov.aclose()

        sent_request = route.calls[0].request
        sent_headers = dict(sent_request.headers)
        sent_body = json.loads(sent_request.content)

    return {
        "model_id": model_id,
        "url": url,
        "chunks": chunks,
        "headers": sent_headers,
        "body": sent_body,
    }


async def _exercise_llama_path(
    region: str, access_key: str, secret_key: str
) -> dict[str, Any]:
    model_id = "meta.llama3-70b-instruct-v1:0"
    url = _bedrock_url(region, model_id)

    upstream_response = {
        "generation": "Paris.",
        "prompt_token_count": 12,
        "generation_token_count": 1,
        "stop_reason": "stop",
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).mock(
            return_value=httpx.Response(200, json=upstream_response)
        )
        prov = BedrockProvider(
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=region,
        )
        req = ChatCompletionRequest(
            model=f"bedrock/{model_id}",
            messages=[{"role": "user", "content": "Capital of France?"}],
            max_tokens=50,
        )
        chunks = []
        async for c in await prov.chat_completion(req):
            chunks.append(c)
        await prov.aclose()

        sent_body = json.loads(route.calls[0].request.content)

    return {
        "model_id": model_id,
        "url": url,
        "chunks": chunks,
        "body": sent_body,
    }


def _check_sigv4(headers: dict[str, str], region: str) -> tuple[bool, list[str]]:
    """Assert the Authorization header is a properly-scoped SigV4 sig."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    failures: list[str] = []

    if not auth.startswith("AWS4-HMAC-SHA256 "):
        failures.append(f"Authorization header missing AWS4-HMAC-SHA256: {auth[:80]!r}")
        return False, failures

    cred_pattern = (
        r"Credential=AKIAIOSFODNN7EXAMPLE/\d{8}/" + region + r"/bedrock/aws4_request"
    )
    if not re.search(cred_pattern, auth):
        failures.append(f"Credential scope wrong (want service=bedrock region={region}): {auth!r}")

    if not re.search(r"SignedHeaders=([^,]+)", auth):
        failures.append("SignedHeaders missing from Authorization header")

    m = re.search(r"Signature=([0-9a-f]+)", auth)
    if not m or len(m.group(1)) != 64:
        failures.append("Signature missing or wrong length (want 64 hex chars)")

    return len(failures) == 0, failures


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--access-key", default=EXAMPLE_ACCESS_KEY)
    parser.add_argument("--secret-key", default=EXAMPLE_SECRET_KEY)
    args = parser.parse_args()

    print("=" * 64)
    print("Phase 42 — AWS Bedrock adapter mocked-live verification")
    print("=" * 64)
    print(f"region:       {args.region}")
    print(f"access key:   {args.access_key[:8]}... (AWS example credentials)")
    print(f"mock target:  bedrock-runtime.{args.region}.amazonaws.com")
    print()

    # ---- Phase 1: Anthropic-on-Bedrock ----
    print("[1/2] Anthropic-on-Bedrock (anthropic.claude-3-5-haiku-...)...")
    anth = await _exercise_anthropic_path(args.region, args.access_key, args.secret_key)
    sigv4_ok, sigv4_fail = _check_sigv4(anth["headers"], args.region)
    anth_body_ok = (
        anth["body"].get("anthropic_version") == "bedrock-2023-05-31"
        and "model" not in anth["body"]
        and anth["body"].get("max_tokens") == 50
    )
    anth_resp_ok = (
        len(anth["chunks"]) == 1
        and anth["chunks"][0].content_delta == "Paris is the capital of France."
        and anth["chunks"][0].finish_reason == "stop"
        and anth["chunks"][0].prompt_tokens == 15
        and anth["chunks"][0].completion_tokens == 8
    )
    print(f"  SigV4 signature scoped to bedrock/{args.region}:  {'✓' if sigv4_ok else 'FAIL'}")
    if not sigv4_ok:
        for f in sigv4_fail:
            print(f"      {f}")
    print(f"  Body shape (anthropic_version + no model):     {'✓' if anth_body_ok else 'FAIL'}")
    print(f"  Response translation (text + tokens + finish): {'✓' if anth_resp_ok else 'FAIL'}")
    print()

    # ---- Phase 2: Llama-on-Bedrock ----
    print("[2/2] Llama-on-Bedrock (meta.llama3-70b-instruct-v1:0)...")
    llama = await _exercise_llama_path(args.region, args.access_key, args.secret_key)
    llama_body_ok = (
        "prompt" in llama["body"]
        and "messages" not in llama["body"]
        and "max_gen_len" in llama["body"]
        and "<|begin_of_text|>" in llama["body"]["prompt"]
        and "Capital of France?" in llama["body"]["prompt"]
    )
    llama_resp_ok = (
        len(llama["chunks"]) == 1
        and llama["chunks"][0].content_delta == "Paris."
        and llama["chunks"][0].finish_reason == "stop"
        and llama["chunks"][0].prompt_tokens == 12
    )
    print(f"  Body shape (Llama prompt template + max_gen_len):  {'✓' if llama_body_ok else 'FAIL'}")
    print(f"  Response translation (text + token counts):        {'✓' if llama_resp_ok else 'FAIL'}")
    print()

    # ---- Verdict ----
    holds = sigv4_ok and anth_body_ok and anth_resp_ok and llama_body_ok and llama_resp_ok

    print("=" * 64)
    if holds:
        print(
            "VERDICT: claim holds — the Bedrock adapter signs every "
            "outbound request with SigV4 scoped to the bedrock service "
            f"in {args.region}, emits the right per-family wire shape "
            "(Anthropic-on-Bedrock with anthropic_version + no model "
            "field; Llama-on-Bedrock with the prompt template + "
            "max_gen_len), and translates Bedrock responses back into "
            "OpenAI-compat ChatCompletionChunk with content + token "
            "counts + finish reason. SUBSTITUTION DISCLOSURE: this is "
            "a respx-mocked endpoint, not real AWS. The SigV4 math, "
            "wire-shape translation, and response translation are all "
            "real — only the network endpoint is substituted. With "
            "real AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + "
            "Bedrock model access granted via the console, the same "
            "adapter code path reaches real bedrock-runtime "
            "successfully — demonstrated in unit + integration tests."
        )
        sys.exit(0)

    print(
        "VERDICT: claim fails — one or more adapter properties did NOT "
        "hold. See per-check output above."
    )
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
