"""End-to-end chat-endpoint tests for Phase 41 multi-modal input.

Four behaviours to lock down via the real FastAPI stack + respx-mocked
upstream:

1. **Multi-modal request reaches the upstream verbatim** (OpenAI-compat
   path). A request with a text part + image_url part is forwarded
   to Groq with the same shape; the `tools` array isn't synthesized.
2. **Anthropic translation** — the same request through the Anthropic
   adapter rewrites image_url to image-with-source.
3. **Size cap enforced pre-flight**. A request whose total base64
   image bytes exceeds the team's `max_image_bytes` is rejected
   with 422 BEFORE the upstream call is made (zero httpx calls).
4. **Image-tokens header stamped** on successful responses.
"""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest
import respx
from sqlalchemy import update

from pronaos.db.models import Team
from pronaos.providers.anthropic import ANTHROPIC_API_URL

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _synth_png(width: int, height: int) -> str:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr = b"IHDR"
    dims = struct.pack(">II", width, height)
    rest = b"\x08\x02\x00\x00\x00"
    crc = b"\x00" * 4
    raw = sig + ihdr_len + ihdr + dims + rest + crc
    return f"data:image/png;base64,{base64.b64encode(raw).decode()}"


def _groq_text_response(text: str = "I see a cat.") -> dict[str, object]:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "llama-3.2-90b-vision-preview",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 300, "completion_tokens": 5},
    }


def _anthropic_text_response(text: str = "I see a cat.") -> dict[str, object]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 300, "output_tokens": 5},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Pass-through (OpenAI-compat path)                                           #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_image_url_passes_through_groq(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """The wire body to Groq must contain the exact image_url shape
    the client sent — OpenAI-compat upstreams expect it verbatim."""
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("got it"))
    )
    img_url = _synth_png(256, 256)
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what's in this image?"},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text
    sent = json.loads(route.calls[0].request.content)
    sent_content = sent["messages"][0]["content"]
    assert isinstance(sent_content, list)
    # text part preserved + image_url part preserved.
    assert sent_content[0] == {"type": "text", "text": "what's in this image?"}
    assert sent_content[1]["type"] == "image_url"
    assert sent_content[1]["image_url"]["url"] == img_url

    # Image-tokens header stamped.
    assert "x-pronaos-image-tokens" in r.headers
    assert int(r.headers["x-pronaos-image-tokens"]) > 0
    assert r.headers["x-pronaos-image-count"] == "1"


# --------------------------------------------------------------------------- #
# Anthropic translation                                                       #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_image_url_translated_for_anthropic(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Same shape through the Anthropic adapter — the wire body uses
    Anthropic's image-block shape with source.type=base64."""
    route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_text_response("ok"))
    )
    img_url = _synth_png(256, 256)
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what's in this image?"},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200, r.text
    sent = json.loads(route.calls[0].request.content)
    sent_content = sent["messages"][0]["content"]
    assert isinstance(sent_content, list)
    assert sent_content[0] == {"type": "text", "text": "what's in this image?"}
    assert sent_content[1]["type"] == "image"
    assert sent_content[1]["source"]["type"] == "base64"
    assert sent_content[1]["source"]["media_type"] == "image/png"


@respx.mock
@pytest.mark.asyncio
async def test_https_url_translated_for_anthropic(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """HTTPS URL → Anthropic source.type=url. No fetching."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_text_response("ok"))
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        }
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200
    sent = json.loads(respx.calls.last.request.content)
    img = sent["messages"][0]["content"][0]
    assert img["type"] == "image"
    assert img["source"] == {"type": "url", "url": "https://example.com/cat.png"}


# --------------------------------------------------------------------------- #
# Size cap enforcement                                                        #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_oversized_image_rejected_pre_flight(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Cap = 100 bytes; image payload is ~50KB. Rejected with 422
    BEFORE any upstream call — assert respx caught zero outgoing
    requests."""
    # Tighten the cap.
    async with auth_setup.sm() as session:
        await session.execute(
            update(Team).where(Team.id == auth_setup.team_id).values(max_image_bytes=100)
        )
        await session.commit()

    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("never reached"))
    )
    # 1000x1000 PNG header → ~50KB after base64 decode pretend; the
    # synthetic header itself is tiny but we attach lots of base64 bytes
    # to push past the 100-byte cap. Use a deliberately large data URI.
    fat_data = "A" * 10000
    fat_url = f"data:image/png;base64,{fat_data}"
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": fat_url}}
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"]["type"] == "image_too_large"
    assert body["detail"]["cap"] == 100
    # CRITICAL: upstream was never called.
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_under_cap_image_passes_through(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Cap = 100,000; small image passes. The 200 path is exercised
    again for completeness."""
    async with auth_setup.sm() as session:
        await session.execute(
            update(Team)
            .where(Team.id == auth_setup.team_id)
            .values(max_image_bytes=100_000)
        )
        await session.commit()
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("ok"))
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _synth_png(64, 64)}}
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# No-cap default (existing behaviour preserved)                               #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_no_cap_team_unaffected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Team with max_image_bytes=NULL = no cap. Big image passes
    through — operators must opt in to the cap to get protection."""
    # Default team has NULL cap.
    fat_url = f"data:image/png;base64,{'A' * 100000}"
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_groq_text_response("ok"))
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": fat_url}}
                    ],
                }
            ],
            "temperature": 0.0,
        },
    )
    assert r.status_code == 200
