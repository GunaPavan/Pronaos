"""HTTP-level tests for /v1/embeddings (Phase 31).

Exercises the full pipeline: auth → allowlist → preflight → guardrails
→ cache → provider → cache write → audit + usage. respx mocks the
upstream provider; we assert response shape, cache behaviour, and the
gateway-stamped X-Pronaos-* headers.
"""

from __future__ import annotations

import httpx
import pytest
import respx


def _refresh_registry_with_openai(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Set OPENAI_API_KEY + rebuild the app's provider registry.

    ``auth_setup`` builds the registry once at fixture creation — at
    that point OPENAI_API_KEY is not set, so the registry skips the
    OpenAI provider. Tests that need OpenAI must set the env var,
    invalidate the settings cache, and rebuild the registry on the
    running app.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    from pronaos.config import get_settings

    get_settings.cache_clear()
    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    from pronaos.providers.registry import ProviderRegistry

    app.state.provider_registry = ProviderRegistry(get_settings())


def _openai_embedding_response(
    *vectors: list[float],
    model: str = "text-embedding-3-small",
    prompt_tokens: int = 8,
) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i} for i, vec in enumerate(vectors)
        ],
        "model": model,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_embeddings_round_trip(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Single-input call returns one vector and the OpenAI response shape."""
    _refresh_registry_with_openai(auth_setup, monkeypatch)

    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_openai_embedding_response([0.1, 0.2, 0.3]))
    )

    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "openai/text-embedding-3-small",
            "input": "Pronaos is a self-hosted LLM gateway.",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    assert body["data"][0] == {
        "object": "embedding",
        "embedding": [0.1, 0.2, 0.3],
        "index": 0,
    }
    assert body["model"] == "openai/text-embedding-3-small"
    assert body["usage"]["prompt_tokens"] == 8
    assert body["usage"]["total_tokens"] == 8


@respx.mock
@pytest.mark.asyncio
async def test_embeddings_batched_input_preserves_order(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _refresh_registry_with_openai(auth_setup, monkeypatch)

    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json=_openai_embedding_response(
                [0.0],
                [0.5],
                [1.0],
                prompt_tokens=15,
            ),
        )
    )

    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["zero", "five", "one"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [d["embedding"] for d in body["data"]] == [[0.0], [0.5], [1.0]]
    assert [d["index"] for d in body["data"]] == [0, 1, 2]


@respx.mock
@pytest.mark.asyncio
async def test_embeddings_cache_hit_on_second_identical_call(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The cache makes the second call free. The upstream must be
    called EXACTLY ONCE; the second call serves from cache.

    Uses the layered cache as installed by the lifespan-ish auth_setup
    — but the default cache is NullCache when Redis isn't configured.
    For this test we install a Redis-less in-memory cache shim onto
    app.state.cache so the cache code path runs.
    """
    _refresh_registry_with_openai(auth_setup, monkeypatch)

    # Swap in an in-memory cache that mimics the Redis-backed Cache
    # protocol: get returns a CacheLookup, put stores, both are
    # async. Keep it minimal — one dict, no TTL.
    from pronaos.cache.base import CacheLookup

    class InMemoryCache:
        def __init__(self) -> None:
            self._data: dict[str, dict] = {}

        def _key(self, *, tenant_id: str, model: str, key_payload: dict) -> str:
            import json

            return f"{tenant_id}:{model}:{json.dumps(key_payload, sort_keys=True)}"

        async def get(self, *, tenant_id, model, key_payload):  # type: ignore[no-untyped-def]
            k = self._key(tenant_id=tenant_id, model=model, key_payload=key_payload)
            hit = self._data.get(k)
            if hit is None:
                return CacheLookup(hit=False)
            return CacheLookup(hit=True, response=hit, tier="exact")

        async def put(self, *, tenant_id, model, key_payload, response) -> None:  # type: ignore[no-untyped-def]
            k = self._key(tenant_id=tenant_id, model=model, key_payload=key_payload)
            self._data[k] = response

        async def aclose(self) -> None:
            self._data.clear()

    # Attach via the running app under auth_setup.client.transport
    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    app.state.cache = InMemoryCache()

    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_openai_embedding_response([0.7, 0.8]))
    )

    common_body = {
        "model": "openai/text-embedding-3-small",
        "input": "the same text twice",
    }

    # First call: miss → upstream call → cache write
    resp1 = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json=common_body,
    )
    assert resp1.status_code == 200
    assert resp1.headers["X-Pronaos-Cache"] == "miss"

    # Second call: hit → no upstream call
    resp2 = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json=common_body,
    )
    assert resp2.status_code == 200
    assert resp2.headers["X-Pronaos-Cache"].startswith("hit:")

    # Vectors must be byte-identical across the two calls.
    assert resp1.json()["data"] == resp2.json()["data"]
    # Critical: upstream called exactly ONCE.
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Error paths                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_provider_prefix_returns_400(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "wrong/whatever",
            "input": "x",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "unknown_provider" in detail.get("type", str(body))


@pytest.mark.asyncio
async def test_chat_only_provider_rejects_embedding_request(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Groq has chat models but no embedding models in the catalog."""
    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "input": "x",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "not_an_embedding_provider" in detail.get("type", str(body))


@pytest.mark.asyncio
async def test_empty_input_returns_400(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _refresh_registry_with_openai(auth_setup, monkeypatch)
    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "openai/text-embedding-3-small",
            "input": [],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "empty_input" in detail.get("type", str(body))


@pytest.mark.asyncio
async def test_missing_provider_api_key_returns_503(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Make sure no openai key is configured. Force-clear the env var
    # that auth_setup's parent shell might have leaked in.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PRONAOS_OPENAI_API_KEY", raising=False)

    from pronaos.config import get_settings

    get_settings.cache_clear()
    # Refresh the registry on the app so it picks up the cleared key.
    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    from pronaos.providers.registry import ProviderRegistry

    app.state.provider_registry = ProviderRegistry(get_settings())

    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "openai/text-embedding-3-small",
            "input": "x",
        },
    )
    assert resp.status_code == 503
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "provider_not_configured" in detail.get("type", str(body))


# --------------------------------------------------------------------------- #
# Auth gate (shared with chat — sanity check on the embedding route)          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_bearer_token_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/embeddings",
        json={"model": "openai/text-embedding-3-small", "input": "x"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.revoked_key),
        json={"model": "openai/text-embedding-3-small", "input": "x"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Response headers                                                            #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_response_headers_carry_provider_and_cost(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _refresh_registry_with_openai(auth_setup, monkeypatch)
    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json=_openai_embedding_response(
                [0.1] * 8,
                prompt_tokens=1_000_000,  # 1M tokens → predictable cost
            ),
        )
    )

    resp = await auth_setup.client.post(
        "/v1/embeddings",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "openai/text-embedding-3-small",
            "input": "x",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Pronaos-Provider"] == "openai"
    # text-embedding-3-small at 2000 hcents per million input tokens:
    # 1_000_000 * 2000 // 1_000_000 = 2000 hcents.
    assert resp.headers["X-Pronaos-Cost-Hcents"] == "2000"
    # Preflight estimate header is also stamped.
    assert "X-Pronaos-Preflight-Estimate" in resp.headers
    # Cache miss on the first call.
    assert resp.headers["X-Pronaos-Cache"] == "miss"
