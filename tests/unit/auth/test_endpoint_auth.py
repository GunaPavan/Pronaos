"""HTTP-level tests that the auth gate on /v1/chat/completions works."""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.openai_compat import (
    _parse_sse_passthrough,  # noqa: F401 — import proves module wiring
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_ok_body(content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.mark.asyncio
async def test_no_authorization_header_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").startswith("Bearer")


@pytest.mark.asyncio
async def test_wrong_scheme_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Basic deadbeef"},
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_garbage_key_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-real-key"},
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.revoked_key}"},
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


@respx.mock
@pytest.mark.asyncio
async def test_valid_key_passes_through(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The auth fixture didn't set a Groq key; patch in a dummy one so the
    # registry can construct the provider for this test.
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    from pronaos.config import get_settings

    get_settings.cache_clear()
    # Rebuild the provider registry + router with the new settings so the
    # handler sees them.
    from pronaos.core.router import Router
    from pronaos.providers.registry import ProviderRegistry

    app = auth_setup.client._transport.app  # type: ignore[attr-defined]
    await app.state.provider_registry.aclose()
    registry = ProviderRegistry(get_settings())
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)

    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_ok_body("pong")))

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {auth_setup.api_key}"},
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["pronaos"]["provider"] == "groq"
    # Request id is echoed back so clients can correlate.
    assert "x-request-id" in {h.lower() for h in resp.headers}
