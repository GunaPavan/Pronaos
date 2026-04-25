"""Smoke tests for system endpoints.

These run against the full app via an in-process ASGI transport so lifespan,
middleware, and routers are all exercised — same path requests take in prod.
"""

from __future__ import annotations

import httpx
import pytest

from pronaos import __version__


@pytest.mark.asyncio
async def test_healthz_returns_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


@pytest.mark.asyncio
async def test_readyz_returns_ready(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
