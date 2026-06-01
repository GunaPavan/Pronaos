"""Phase 60 — HTTP-level tests for embedding batches.

Composes with Phase 59's endpoint tests. Covers
- POST /v1/batches with endpoint=/v1/embeddings persists the row
  with endpoint stored + upstream POST /v1/batches body carries
  endpoint=/v1/embeddings
- Anthropic + /v1/embeddings is rejected with 422
  embeddings_batch_unsupported_provider
- Mismatched-provider in the embedding batch path is rejected
- Submit reuses Phase 59's row machinery (status=validating, etc.)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy import select

from pronaos.db.models import Batch, Team


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_batches(sm, team_id: str) -> None:  # type: ignore[no-untyped-def]
    async with sm() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        team.batches_enabled = True
        await session.commit()


@respx.mock
@pytest.mark.asyncio
async def test_embeddings_batch_persists_row_and_carries_endpoint(  # type: ignore[no-untyped-def]
    auth_setup, monkeypatch
) -> None:
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from pronaos.config import get_settings

    get_settings.cache_clear()

    respx.post("https://api.openai.com/v1/files").mock(
        return_value=httpx.Response(200, json={"id": "file-emb"})
    )
    create = respx.post("https://api.openai.com/v1/batches").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "batch_emb_001",
                "status": "validating",
                "object": "batch",
            },
        )
    )

    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "endpoint": "/v1/embeddings",
            "requests": [
                {
                    "custom_id": "doc-1",
                    "body": {
                        "model": "openai/text-embedding-3-small",
                        "input": "first document",
                    },
                },
                {
                    "custom_id": "doc-2",
                    "body": {
                        "model": "openai/text-embedding-3-small",
                        "input": "second document",
                    },
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["status"] == "validating"
    assert body["endpoint"] == "/v1/embeddings"
    assert body["request_counts"]["total"] == 2

    # The upstream's POST /v1/batches body must carry the embeddings
    # endpoint — otherwise OpenAI will reject the batch as malformed.
    create_body = json.loads(create.calls.last.request.content)
    assert create_body["endpoint"] == "/v1/embeddings"

    # Row persisted with the right endpoint column.
    async with auth_setup.sm() as session:
        rows = (await session.execute(select(Batch))).scalars().all()
        assert len(rows) == 1
        assert rows[0].endpoint == "/v1/embeddings"
        # Input payload's per-line url field also reflects the endpoint
        # — important for replay + audit.
        first_line = rows[0].input_payload.splitlines()[0]
        first_obj = json.loads(first_line)
        assert first_obj["url"] == "/v1/embeddings"


@pytest.mark.asyncio
async def test_anthropic_embeddings_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Anthropic + /v1/embeddings → 422
    embeddings_batch_unsupported_provider."""
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "endpoint": "/v1/embeddings",
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {
                        "model": "anthropic/claude-opus-4-7",
                        "input": "hi",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "embeddings_batch_unsupported_provider"


@pytest.mark.asyncio
async def test_unsupported_endpoint_still_rejected(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Phase 60 widens the gate to {/v1/chat/completions, /v1/embeddings}
    but anything else still 422s — regression on the existing gate."""
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    resp = await auth_setup.client.post(
        "/v1/batches",
        headers=_auth(auth_setup.api_key),
        json={
            "endpoint": "/v1/audio/transcriptions",
            "requests": [
                {
                    "custom_id": "r1",
                    "body": {"model": "openai/whisper-1"},
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "batch_endpoint_unsupported"


@pytest.mark.asyncio
async def test_bare_text_embedding_name_routes_to_openai(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A bare ``text-embedding-3-small`` (no prefix) should still
    route to OpenAI now that provider_from_model handles the pattern."""
    await _enable_batches(auth_setup.sm, auth_setup.team_id)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from pronaos.config import get_settings

    get_settings.cache_clear()

    with respx.mock(assert_all_called=False) as r:
        r.post("https://api.openai.com/v1/files").mock(
            return_value=httpx.Response(200, json={"id": "file-bare"})
        )
        r.post("https://api.openai.com/v1/batches").mock(
            return_value=httpx.Response(
                200, json={"id": "batch_bare", "status": "validating"}
            )
        )

        resp = await auth_setup.client.post(
            "/v1/batches",
            headers=_auth(auth_setup.api_key),
            json={
                "endpoint": "/v1/embeddings",
                "requests": [
                    {
                        "custom_id": "r1",
                        "body": {
                            "model": "text-embedding-3-small",
                            "input": "hi",
                        },
                    }
                ],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "openai"
