"""HTTP-level tests for /v1/rerank (Phase 32).

Exercises the full pipeline: auth → allowlist → preflight → guardrails
→ cache → provider → cache write → audit + usage. respx mocks the
upstream provider; we assert response shape, cache behaviour, and the
gateway-stamped X-Pronaos-* headers.
"""

from __future__ import annotations

import httpx
import pytest
import respx


def _refresh_registry_with_cohere(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Set COHERE_API_KEY + rebuild the app's provider registry."""
    monkeypatch.setenv("COHERE_API_KEY", "test-cohere-key")
    from pronaos.config import get_settings

    get_settings.cache_clear()
    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    from pronaos.providers.registry import ProviderRegistry

    app.state.provider_registry = ProviderRegistry(get_settings())


def _cohere_rerank_response(
    *items: tuple[int, float, str],
    search_units: int = 1,
) -> dict:
    return {
        "id": "abc",
        "results": [
            {"index": i, "relevance_score": s, "document": {"text": t}} for (i, s, t) in items
        ],
        "meta": {
            "api_version": {"version": "2"},
            "billed_units": {"search_units": search_units},
        },
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_rerank_round_trip(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Single rerank call returns scored items in upstream order."""
    _refresh_registry_with_cohere(auth_setup, monkeypatch)

    respx.post("https://api.cohere.com/v2/rerank").mock(
        return_value=httpx.Response(
            200,
            json=_cohere_rerank_response(
                (2, 0.99, "Washington, D.C. is the capital."),
                (0, 0.07, "Carson City is the capital of Nevada."),
            ),
        )
    )

    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "cohere/rerank-english-v3.0",
            "query": "What is the capital of the United States?",
            "documents": [
                "Carson City is the capital of Nevada.",
                "Tokyo is the capital of Japan.",
                "Washington, D.C. is the capital.",
            ],
            "top_n": 2,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    assert body["data"][0]["index"] == 2
    assert body["data"][0]["relevance_score"] == pytest.approx(0.99)
    assert body["data"][0]["document"] == "Washington, D.C. is the capital."
    assert body["data"][1]["index"] == 0
    assert body["model"] == "cohere/rerank-english-v3.0"


@respx.mock
@pytest.mark.asyncio
async def test_rerank_cache_hit_on_repeat(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Identical query + documents → second call hits cache + zero upstream."""
    _refresh_registry_with_cohere(auth_setup, monkeypatch)

    # Install an in-memory cache (NullCache by default).
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

    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    app.state.cache = InMemoryCache()

    route = respx.post("https://api.cohere.com/v2/rerank").mock(
        return_value=httpx.Response(
            200,
            json=_cohere_rerank_response(
                (1, 0.8, "doc B"),
                (0, 0.2, "doc A"),
            ),
        )
    )

    common_body = {
        "model": "cohere/rerank-english-v3.0",
        "query": "find the relevant doc",
        "documents": ["doc A", "doc B"],
        "top_n": 2,
    }

    resp1 = await auth_setup.client.post(
        "/v1/rerank", headers=_auth(auth_setup.api_key), json=common_body
    )
    assert resp1.status_code == 200
    assert resp1.headers["X-Pronaos-Cache"] == "miss"

    resp2 = await auth_setup.client.post(
        "/v1/rerank", headers=_auth(auth_setup.api_key), json=common_body
    )
    assert resp2.status_code == 200
    assert resp2.headers["X-Pronaos-Cache"].startswith("hit:")
    # Byte-identical response.
    assert resp1.json()["data"] == resp2.json()["data"]
    # Critical: upstream called once.
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_rerank_omits_top_n_when_absent(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Public top_n=None must NOT show up in the upstream body."""
    _refresh_registry_with_cohere(auth_setup, monkeypatch)

    route = respx.post("https://api.cohere.com/v2/rerank").mock(
        return_value=httpx.Response(200, json=_cohere_rerank_response((0, 0.5, "the doc")))
    )
    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "cohere/rerank-english-v3.0",
            "query": "q",
            "documents": ["the doc"],
        },
    )
    assert resp.status_code == 200
    upstream_body = route.calls[0].request.content
    assert b'"top_n"' not in upstream_body


@respx.mock
@pytest.mark.asyncio
async def test_response_headers(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _refresh_registry_with_cohere(auth_setup, monkeypatch)

    respx.post("https://api.cohere.com/v2/rerank").mock(
        return_value=httpx.Response(200, json=_cohere_rerank_response((0, 0.5, "d")))
    )

    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "cohere/rerank-english-v3.0",
            "query": "q",
            "documents": ["d"],
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Pronaos-Provider"] == "cohere"
    # Per-call cost: 1 search_unit × 20 hcents/call = 20.
    assert resp.headers["X-Pronaos-Cost-Hcents"] == "20"
    assert "X-Pronaos-Preflight-Estimate" in resp.headers
    assert resp.headers["X-Pronaos-Cache"] == "miss"


# --------------------------------------------------------------------------- #
# Error paths                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_provider_prefix_returns_400(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={"model": "wrong/whatever", "query": "q", "documents": ["d"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "unknown_provider" in detail.get("type", str(body))


@pytest.mark.asyncio
async def test_non_rerank_provider_returns_400(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """OpenAI is in the catalog but has no rerank_pricing."""
    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={"model": "openai/text-embedding-3-small", "query": "q", "documents": ["d"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "not_a_rerank_provider" in detail.get("type", str(body))


@pytest.mark.asyncio
async def test_empty_documents_returns_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Pydantic min_length=1 rejects empty document arrays."""
    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "cohere/rerank-english-v3.0",
            "query": "q",
            "documents": [],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_provider_api_key_returns_503(auth_setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    monkeypatch.delenv("PRONAOS_COHERE_API_KEY", raising=False)
    from pronaos.config import get_settings

    get_settings.cache_clear()
    transport = auth_setup.client._transport
    app = transport.app  # type: ignore[attr-defined]
    from pronaos.providers.registry import ProviderRegistry

    app.state.provider_registry = ProviderRegistry(get_settings())

    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "cohere/rerank-english-v3.0",
            "query": "q",
            "documents": ["d"],
        },
    )
    assert resp.status_code == 503
    body = resp.json()
    detail = body.get("detail") or body.get("error") or {}
    assert "provider_not_configured" in detail.get("type", str(body))


# --------------------------------------------------------------------------- #
# Auth gate                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_bearer_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/rerank",
        json={"model": "cohere/rerank-english-v3.0", "query": "q", "documents": ["d"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_returns_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    resp = await auth_setup.client.post(
        "/v1/rerank",
        headers=_auth(auth_setup.revoked_key),
        json={"model": "cohere/rerank-english-v3.0", "query": "q", "documents": ["d"]},
    )
    assert resp.status_code == 401
