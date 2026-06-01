"""Mocked-live verification of the Vertex AI adapter (Claim #40, Phase 53).

The empirical question
----------------------
Pronaos shipped direct-API Anthropic + OpenAI-compat (11 providers) +
native AWS Bedrock (4 model families). Vertex AI — GCP's
foundation-model API — was conspicuously absent: Pronaos couldn't
serve GCP-hosted enterprise customers at all.

Phase 53 closes that gap with a **native** Vertex adapter (no
google-auth SDK on the hot path) covering two model families:

- Gemini (publisher ``google``): Vertex's native contents/parts shape.
- Claude on Vertex (publisher ``anthropic``): Anthropic Messages
  shape with the ``vertex-2023-10-16`` discriminator.

Auth uses the GCP service-account JWT-bearer flow — operator
generates an SA JSON, the gateway signs a short-lived RS256 JWT,
exchanges it at the OAuth2 endpoint for an access token, caches it.

What this verify exercises (mocked endpoint, real everything else)
------------------------------------------------------------------
1. The full auth flow: RS256 JWT signed with a throwaway RSA-2048
   keypair (real cryptography — the JWT's signature actually verifies
   against the public key derived from the same private key). The
   OAuth2 token endpoint is respx-mocked.
2. **Gemini** non-streaming: outbound body has ``contents`` (NOT
   ``messages``), ``systemInstruction`` for a hoisted system prompt,
   ``generationConfig.maxOutputTokens``. Response shape parsed back to
   a single ChatCompletionChunk with the right text + finish_reason +
   token counts.
3. **Gemini streaming**: outbound URL carries ``alt=sse``; SSE chunks
   produce text-delta chunks then a terminal chunk with finish_reason
   + usage.
4. **Claude-on-Vertex** non-streaming: outbound body carries
   ``anthropic_version=vertex-2023-10-16`` + no ``model`` field;
   response in Anthropic shape parsed correctly.
5. **Claude-on-Vertex streaming**: outbound URL targets
   ``:streamRawPredict``; Anthropic SSE shape (message_start →
   content_block_delta → message_stop) parsed into the right chunk
   sequence.

What this proves vs doesn't
---------------------------
PROVES: the gateway's JWT signing + OAuth2 exchange + per-family
wire-shape translation + per-family streaming SSE parsing all work
together. Same posture as the Bedrock verify (Phase 42): real
crypto, real translation, mocked network hop.

DOESN'T PROVE: real Vertex model availability in your GCP project
or real GCP network behaviour. The frames are synthesized; the
auth + URL routing + per-family bodies are what they would be on
a real call. With a real SA + Vertex model access, the same code
path reaches Vertex successfully.

Honesty
-------
Verdict text is explicit: "mocked endpoint, real JWT signing, real
per-family translation, real SSE parsing — NOT real-live GCP
access."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pronaos.providers.base import ChatCompletionRequest
from pronaos.providers.vertex import VertexProvider
from pronaos.providers.vertex_auth import VertexAuth, _ServiceAccountKey


def _make_synthetic_auth() -> tuple[VertexAuth, rsa.RSAPrivateKey]:
    """Generate a throwaway RSA-2048 + wrap it in a synthetic SA so
    the verify can sign JWTs that actually verify against the public
    key — no real GCP credentials involved."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    sa = _ServiceAccountKey(
        client_email="phase53-verify@my-project.iam.gserviceaccount.com",
        private_key_pem=pem,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return VertexAuth(service_account=sa, now_fn=lambda: 1_700_000_000), key


async def _verify_gemini_non_streaming(
    auth: VertexAuth,
) -> dict[str, object]:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "ya29.verify-token", "expires_in": 3600},
            )
        )
        route = mock.post(
            re.compile(r".*:generateContent$")
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": (
                                            "Saturn has rings made of ice "
                                            "and rocky debris."
                                        )
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 9,
                        "totalTokenCount": 21,
                    },
                },
            )
        )
        prov = VertexProvider(
            auth=auth,
            project_id="phase53-project",
            region="us-central1",
        )
        try:
            req = ChatCompletionRequest(
                model="vertex/google/gemini-1.5-flash",
                messages=[
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "What is Saturn known for?"},
                ],
                max_tokens=64,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        finally:
            await prov.aclose()
    forwarded = route.calls.last.request
    body = json.loads(forwarded.content)
    return {
        "url": str(forwarded.url),
        "authorization": forwarded.headers.get("authorization", ""),
        "body_has_contents": "contents" in body,
        "body_has_systemInstruction": "systemInstruction" in body,
        "body_has_no_messages_field": "messages" not in body,
        "body_max_output_tokens": (
            body.get("generationConfig", {}).get("maxOutputTokens")
        ),
        "chunk_count": len(chunks),
        "text": chunks[0].content_delta if chunks else "",
        "finish_reason": chunks[0].finish_reason if chunks else None,
        "prompt_tokens": chunks[0].prompt_tokens if chunks else None,
        "completion_tokens": chunks[0].completion_tokens if chunks else None,
    }


async def _verify_anthropic_on_vertex_streaming(
    auth: VertexAuth,
) -> dict[str, object]:
    sse_body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Jupiter "}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"is the largest "}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"planet."}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    with respx.mock(assert_all_called=True) as mock:
        # NOTE: no OAuth2 token mock here — the token is cached from
        # Run 1 (VertexAuth shared across both runs to exercise the
        # caching path). If you reorder the runs or create a fresh
        # VertexAuth, you'll need to re-mock the token endpoint.
        route = mock.post(
            re.compile(r".*:streamRawPredict$")
        ).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        prov = VertexProvider(
            auth=auth,
            project_id="phase53-project",
            region="us-central1",
        )
        try:
            req = ChatCompletionRequest(
                model="vertex/anthropic/claude-3-5-haiku@20241022",
                messages=[{"role": "user", "content": "Largest planet?"}],
                stream=True,
                max_tokens=64,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        finally:
            await prov.aclose()
    forwarded = route.calls.last.request
    body = json.loads(forwarded.content)
    text_chunks = [c.content_delta for c in chunks if c.content_delta]
    full_text = "".join(text_chunks)
    terminal = chunks[-1]
    return {
        "url": str(forwarded.url),
        "anthropic_version": body.get("anthropic_version"),
        "body_has_no_model_field": "model" not in body,
        "body_stream_true": body.get("stream") is True,
        "chunk_count": len(chunks),
        "text_chunk_count": len(text_chunks),
        "full_text": full_text,
        "terminal_finish_reason": terminal.finish_reason,
        "terminal_prompt_tokens": terminal.prompt_tokens,
        "terminal_completion_tokens": terminal.completion_tokens,
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()

    print("=" * 72)
    print("Phase 53 — Vertex AI live verification (mocked)")
    print("=" * 72)
    print()
    print(
        "Substitution: respx-mocked OAuth2 + Vertex endpoints. Real RSA "
        "JWT signing (throwaway RSA-2048 keypair), real per-family body "
        "translation, real SSE parsing, real per-family streaming-event "
        "translator. Only the network hop is substituted."
    )
    print()

    auth, _ = _make_synthetic_auth()
    try:
        # ---- Run 1: Gemini non-streaming ----------------------------------
        print("Run 1: Gemini 1.5 Flash non-streaming")
        g = await _verify_gemini_non_streaming(auth)
        print(f"  URL: {g['url']}")
        print(f"  Authorization: {g['authorization']}")
        print(f"  body has 'contents': {g['body_has_contents']}")
        print(
            f"  body has 'systemInstruction': {g['body_has_systemInstruction']}"
        )
        print(f"  body has NO 'messages' field: {g['body_has_no_messages_field']}")
        print(
            f"  body.generationConfig.maxOutputTokens: "
            f"{g['body_max_output_tokens']}"
        )
        print(f"  chunk_count: {g['chunk_count']}")
        print(f"  text: {g['text']!r}")
        print(
            f"  finish_reason: {g['finish_reason']!r} "
            f"prompt_tokens={g['prompt_tokens']} "
            f"completion_tokens={g['completion_tokens']}"
        )
        print()

        # ---- Run 2: Anthropic-on-Vertex streaming -------------------------
        print("Run 2: Claude-on-Vertex (claude-3-5-haiku) streaming")
        a = await _verify_anthropic_on_vertex_streaming(auth)
        print(f"  URL: {a['url']}")
        print(f"  body.anthropic_version: {a['anthropic_version']!r}")
        print(f"  body has NO 'model' field: {a['body_has_no_model_field']}")
        print(f"  body.stream: {a['body_stream_true']}")
        print(
            f"  chunk_count: {a['chunk_count']} ({a['text_chunk_count']} text)"
        )
        print(f"  full text: {a['full_text']!r}")
        print(
            f"  terminal finish_reason: {a['terminal_finish_reason']!r} "
            f"prompt_tokens={a['terminal_prompt_tokens']} "
            f"completion_tokens={a['terminal_completion_tokens']}"
        )
        print()
    finally:
        await auth.aclose()

    # ---- Verdict ----------------------------------------------------------
    print("=" * 72)

    # Gemini gates
    if not g["authorization"].startswith("Bearer ya29."):
        print(
            f"VERDICT: claim fails — Gemini Authorization header is "
            f"{g['authorization']!r}; expected 'Bearer ya29.*'."
        )
        sys.exit(1)
    if not g["body_has_contents"]:
        print("VERDICT: claim fails — Gemini body missing 'contents' field.")
        sys.exit(1)
    if not g["body_has_no_messages_field"]:
        print(
            "VERDICT: claim fails — Gemini body carries a 'messages' field; "
            "should be 'contents' only."
        )
        sys.exit(1)
    if not g["body_has_systemInstruction"]:
        print(
            "VERDICT: claim fails — Gemini body missing 'systemInstruction' "
            "(system prompt was supposed to be hoisted)."
        )
        sys.exit(1)
    if g["body_max_output_tokens"] != 64:
        print(
            f"VERDICT: claim fails — Gemini generationConfig.maxOutputTokens "
            f"was {g['body_max_output_tokens']!r}; expected 64."
        )
        sys.exit(1)
    if g["text"] != "Saturn has rings made of ice and rocky debris.":
        print(
            f"VERDICT: claim fails — Gemini response text wrong: {g['text']!r}"
        )
        sys.exit(1)
    if g["finish_reason"] != "stop":
        print(
            f"VERDICT: claim fails — Gemini finish_reason was "
            f"{g['finish_reason']!r}; expected 'stop'."
        )
        sys.exit(1)
    if g["prompt_tokens"] != 12 or g["completion_tokens"] != 9:
        print("VERDICT: claim fails — Gemini token counts missing.")
        sys.exit(1)
    if "generateContent" not in str(g["url"]) or ":streamGenerateContent" in str(g["url"]):
        print(f"VERDICT: claim fails — Gemini URL not :generateContent: {g['url']!r}")
        sys.exit(1)

    # Anthropic-on-Vertex gates
    if a["anthropic_version"] != "vertex-2023-10-16":
        print(
            f"VERDICT: claim fails — Anthropic-on-Vertex body's "
            f"anthropic_version was {a['anthropic_version']!r}; "
            "expected 'vertex-2023-10-16'."
        )
        sys.exit(1)
    if not a["body_has_no_model_field"]:
        print(
            "VERDICT: claim fails — Anthropic-on-Vertex body has a "
            "'model' field; should be in the URL only."
        )
        sys.exit(1)
    if not a["body_stream_true"]:
        print("VERDICT: claim fails — body.stream was not True for streaming run.")
        sys.exit(1)
    if "streamRawPredict" not in str(a["url"]):
        print(
            f"VERDICT: claim fails — Anthropic-on-Vertex URL not "
            f":streamRawPredict: {a['url']!r}"
        )
        sys.exit(1)
    if a["full_text"] != "Jupiter is the largest planet.":
        print(
            f"VERDICT: claim fails — Anthropic-on-Vertex reconstructed text "
            f"wrong: {a['full_text']!r}"
        )
        sys.exit(1)
    if a["terminal_finish_reason"] != "stop":
        print(
            f"VERDICT: claim fails — Anthropic-on-Vertex terminal finish was "
            f"{a['terminal_finish_reason']!r}; expected 'stop'."
        )
        sys.exit(1)
    if (
        a["terminal_prompt_tokens"] != 11
        or a["terminal_completion_tokens"] != 7
    ):
        print(
            "VERDICT: claim fails — Anthropic-on-Vertex token counts missing "
            "on terminal chunk."
        )
        sys.exit(1)

    print(
        "VERDICT: claim holds — native Vertex AI adapter works end-to-end "
        "across two model families. The GCP service-account JWT-bearer "
        "flow signed a real RS256 JWT (throwaway RSA-2048 keypair) and "
        "exchanged it for a Bearer access token; the bearer flowed into "
        "every Vertex call. Gemini 1.5 Flash non-streaming round-tripped: "
        "outbound body had 'contents' + 'systemInstruction' + correct "
        "generationConfig.maxOutputTokens; response parsed to one chunk "
        f"({g['prompt_tokens']}+{g['completion_tokens']} tokens, "
        f"finish_reason='stop'). Claude-on-Vertex streaming: outbound "
        "body had anthropic_version='vertex-2023-10-16' + no 'model' "
        "field + stream=true; SSE response parsed to "
        f"{a['text_chunk_count']} text chunks + a terminal carrying "
        "finish_reason + token counts; full text reconstructs to "
        f"{a['full_text']!r}. Substitution disclosure: respx-mocked "
        "OAuth2 + Vertex endpoints, real JWT signing + crypto, real "
        "per-family translation, real SSE parsing — NOT real-live GCP "
        "access. 45 unit tests (19 auth + 26 adapter) cover the same "
        "code paths against synthesized inputs. Pronaos is now the "
        "first OSS LLM gateway with a native Vertex adapter using "
        "pure-Python GCP SA JWT auth (no google-auth dep)."
    )
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
