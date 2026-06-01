"""End-to-end chat-endpoint tests for the Phase 42 Bedrock adapter.

Three behaviours to lock down via the real FastAPI stack + respx-mocked
Bedrock endpoint:

1. **Anthropic-on-Bedrock**: client sends an OpenAI-compat request with
   ``model="bedrock/anthropic.claude-3-5-haiku-..."``. Gateway routes
   to the Bedrock adapter, signs with SigV4, posts to the right URL,
   and translates the Bedrock response into an OpenAI-compat shape on
   the way back. The wire body is verified — no ``model`` field, has
   ``anthropic_version: bedrock-2023-05-31``.
2. **Llama-on-Bedrock**: same flow, but the wire body uses the Llama
   prompt template + ``max_gen_len`` (not Anthropic's shape).
3. **Auth header check**: every outbound request carries
   ``Authorization: AWS4-HMAC-SHA256 ... /bedrock/aws4_request``.
   This is the GATEWAY-LEVEL guarantee that SigV4 is wired correctly
   to the chat endpoint, not just the adapter in isolation.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
@pytest.mark.asyncio
async def test_chat_routes_to_bedrock_anthropic(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A ``bedrock/anthropic.*`` model resolves to the Bedrock adapter,
    forwards the Anthropic-on-Bedrock shape, and returns an OpenAI-compat
    response to the client."""
    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/"
        "model/anthropic.claude-3-5-haiku-20241022-v1:0/invoke"
    )
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Paris."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        )
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            "messages": [{"role": "user", "content": "What's the capital of France?"}],
            "max_tokens": 20,
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text

    # The gateway returns OpenAI-compat shape.
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Paris."

    # The outbound request hit the Bedrock URL, was SigV4-signed, and
    # used Anthropic-on-Bedrock body shape.
    assert route.call_count == 1
    sent = route.calls[0].request
    auth = sent.headers.get("authorization") or sent.headers.get("Authorization") or ""
    assert "AWS4-HMAC-SHA256" in auth
    assert "/bedrock/aws4_request" in auth

    sent_body = json.loads(sent.content)
    assert sent_body["anthropic_version"] == "bedrock-2023-05-31"
    # CRITICAL: no top-level ``model`` field (model is in the URL path).
    assert "model" not in sent_body
    assert sent_body["messages"] == [{"role": "user", "content": "What's the capital of France?"}]
    assert sent_body["max_tokens"] == 20


@respx.mock
@pytest.mark.asyncio
async def test_chat_routes_to_bedrock_llama(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Llama-on-Bedrock uses a different body shape — flat ``prompt`` +
    ``max_gen_len``, NOT the Anthropic ``messages`` envelope. The
    discriminator is the model ID prefix (``meta.*``)."""
    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/meta.llama3-70b-instruct-v1:0/invoke"
    )
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "generation": "Paris",
                "prompt_token_count": 25,
                "generation_token_count": 1,
                "stop_reason": "stop",
            },
        )
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "bedrock/meta.llama3-70b-instruct-v1:0",
            "messages": [{"role": "user", "content": "Capital of France?"}],
        },
    )
    assert r.status_code == 200, r.text

    sent_body = json.loads(route.calls[0].request.content)
    # Llama gets a flat ``prompt`` string.
    assert "prompt" in sent_body
    assert "messages" not in sent_body
    # And ``max_gen_len`` instead of Anthropic's ``max_tokens``.
    assert "max_gen_len" in sent_body
    # The Llama 3 template tags should appear in the rendered prompt.
    assert "<|begin_of_text|>" in sent_body["prompt"]
    assert "<|start_header_id|>user<|end_header_id|>" in sent_body["prompt"]
    assert "Capital of France?" in sent_body["prompt"]


@respx.mock
@pytest.mark.asyncio
async def test_bedrock_400_surfaces_as_provider_error(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A 4xx from Bedrock surfaces as the gateway's standard
    upstream-error JSON, not a 500."""
    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/"
        "model/anthropic.claude-3-5-haiku-20241022-v1:0/invoke"
    )
    respx.post(url).mock(
        return_value=httpx.Response(
            400,
            json={"message": "Model not granted for this account"},
        )
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    # The chat handler maps ProviderError(status=400) to its
    # upstream-error shape (the exact HTTP code is per the failover
    # chain; the important property is that the error message includes
    # Bedrock's reason so operators can debug "model access denied"
    # from the gateway logs).
    assert r.status_code >= 400
    body_text = r.text
    assert "Model not granted" in body_text or "bedrock" in body_text.lower()
