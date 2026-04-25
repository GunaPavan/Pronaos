"""HTTP-level tests for /v1/chat/completions.

Exercises the full stack: auth gate → FastAPI route → router → provider
registry → adapter → httpx (mocked by respx). Complements the provider-level
tests (test_anthropic.py, test_openai_compat.py) with an end-to-end check
through the real FastAPI stack.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.anthropic import ANTHROPIC_API_URL


def _anthropic_response(text: str = "hi there") -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
@pytest.mark.asyncio
async def test_chat_completion_openai_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("hi there"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hi there"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    assert body["pronaos"]["provider"] == "anthropic"
    assert body["pronaos"]["cost_hcents"] >= 0


@respx.mock
@pytest.mark.asyncio
async def test_streaming_emits_openai_sse(auth_setup) -> None:  # type: ignore[no-untyped-def]
    sse = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    )
    respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(200, text=sse))

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    body = resp.text
    events = [line for line in body.split("\n\n") if line.startswith("data: ")]
    assert any('"role":"assistant"' in e for e in events)
    assert any('"content":"hi"' in e for e in events)
    assert any('"finish_reason":"stop"' in e for e in events)
    assert events[-1].strip() == "data: [DONE]"


@respx.mock
@pytest.mark.asyncio
async def test_upstream_auth_error_surfaces_as_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "bad key"},
            },
        )
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "AuthError"
