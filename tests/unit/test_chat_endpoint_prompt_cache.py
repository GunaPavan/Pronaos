"""End-to-end tests for /v1/chat/completions Anthropic prompt-cache (Phase 34).

Exercises the full stack: the chat handler reads cache_creation /
cache_read tokens from the chunk, computes weighted cost via the
provider, surfaces them in the response body's ``pronaos`` block,
and stamps X-Pronaos-Prompt-Cache-* response headers.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.anthropic import ANTHROPIC_API_URL


def _anthropic_response_with_cache(
    *,
    text: str = "ack",
    input_tokens: int = 5,
    output_tokens: int = 2,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> dict:
    usage: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_creation:
        usage["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": usage,
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
@pytest.mark.asyncio
async def test_chat_with_cache_read_stamps_headers(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A response with cache_read_input_tokens > 0 stamps headers + body."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response_with_cache(
                input_tokens=10,
                output_tokens=5,
                cache_creation=0,
                cache_read=500,
            ),
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
    assert resp.status_code == 200, resp.text

    # Headers stamped.
    assert resp.headers.get("X-Pronaos-Prompt-Cache-Read-Tokens") == "500"
    assert resp.headers.get("X-Pronaos-Prompt-Cache-Write-Tokens") == "0"
    # Savings header must be present and non-negative.
    saved = int(resp.headers.get("X-Pronaos-Prompt-Cache-Saved-Hcents", "-1"))
    assert saved >= 0

    # Body's pronaos block carries the same numbers.
    body = resp.json()
    meta = body["pronaos"]
    assert meta["cache_read_tokens"] == 500
    assert meta["cache_creation_tokens"] == 0
    assert "cache_saved_hcents" in meta
    # Cost still positive (we paid for input+output+cache_read).
    assert meta["cost_hcents"] > 0


@respx.mock
@pytest.mark.asyncio
async def test_chat_without_cache_no_headers_no_meta(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """No cache_control on the request → no cache headers, no cache meta in body."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response_with_cache(
                input_tokens=10,
                output_tokens=5,
                # Anthropic omits the cache fields when no cache_control is used.
            ),
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
    assert resp.status_code == 200

    # No cache headers stamped (clean response for non-cached calls).
    assert "X-Pronaos-Prompt-Cache-Read-Tokens" not in resp.headers
    assert "X-Pronaos-Prompt-Cache-Write-Tokens" not in resp.headers
    assert "X-Pronaos-Prompt-Cache-Saved-Hcents" not in resp.headers

    # Body's pronaos block has provider + cost, no cache fields.
    meta = resp.json()["pronaos"]
    assert "cache_read_tokens" not in meta
    assert "cache_creation_tokens" not in meta
    assert "cache_saved_hcents" not in meta


@respx.mock
@pytest.mark.asyncio
async def test_first_call_writes_cache_second_call_reads_it(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Simulate the canonical flow: call 1 creates the cache (write_tokens > 0),
    call 2 reads it (read_tokens > 0, lower cost).
    """
    # Use a route side-effect so each call returns a different payload.
    responses = iter(
        [
            httpx.Response(
                200,
                json=_anthropic_response_with_cache(
                    input_tokens=50,
                    output_tokens=10,
                    cache_creation=2000,  # writing the cache
                    cache_read=0,
                ),
            ),
            httpx.Response(
                200,
                json=_anthropic_response_with_cache(
                    input_tokens=50,
                    output_tokens=10,
                    cache_creation=0,
                    cache_read=2000,  # reading the cache on call 2
                ),
            ),
        ]
    )
    respx.post(ANTHROPIC_API_URL).mock(side_effect=lambda req: next(responses))

    body = {
        "model": "anthropic/claude-opus-4-7",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.5,  # bypass cache (we want each call to hit upstream)
    }

    r1 = await auth_setup.client.post(
        "/v1/chat/completions", headers=_auth(auth_setup.api_key), json=body
    )
    assert r1.status_code == 200
    m1 = r1.json()["pronaos"]
    assert m1["cache_creation_tokens"] == 2000
    assert m1["cache_read_tokens"] == 0

    r2 = await auth_setup.client.post(
        "/v1/chat/completions", headers=_auth(auth_setup.api_key), json=body
    )
    assert r2.status_code == 200
    m2 = r2.json()["pronaos"]
    assert m2["cache_creation_tokens"] == 0
    assert m2["cache_read_tokens"] == 2000

    # Call 2 cost MUST be lower than call 1 (cache read at 10% vs cache
    # creation at 125%, with same number of additional input + output).
    assert m2["cost_hcents"] < m1["cost_hcents"]
    # Call 2 reports non-zero savings.
    assert m2["cache_saved_hcents"] > 0
