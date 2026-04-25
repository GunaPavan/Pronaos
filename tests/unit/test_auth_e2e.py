"""End-to-end auth integration test.

Proves the full operator path works:
  1. A fresh SQLite DB exists with no tables.
  2. Alembic upgrade runs the initial migration.
  3. We create a tenant, a team, and an API key via the SAME code paths the
     CLI uses.
  4. A request through the running FastAPI app authenticates with that key
     and hits a mocked provider.
  5. A revoked key returns 401.

Pure-Python — no subprocess, no filesystem persistence beyond a tmp path.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import create_async_engine

from pronaos.auth.api_keys import generate_api_key, hash_key
from pronaos.config import get_settings
from pronaos.core.router import Router
from pronaos.db.models import ApiKey, Team, Tenant
from pronaos.db.session import create_sessionmaker
from pronaos.main import create_app
from pronaos.providers.registry import ProviderRegistry

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_ok_body() -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@respx.mock
@pytest.mark.asyncio
async def test_full_flow_migration_to_authed_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "pronaos-e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("PRONAOS_DATABASE_URL", db_url)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings.cache_clear()

    # ---- 1) Run the real Alembic migration against the fresh DB ----------
    from alembic import command
    from alembic.config import Config as AlembicConfig

    ini = Path(__file__).resolve().parents[2] / "alembic.ini"  # noqa: ASYNC240 — one-time setup, not hot path
    cfg = AlembicConfig(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    # ``migrations/env.py`` calls ``asyncio.run(...)`` for the async driver.
    # We're inside a running event loop here (pytest-asyncio), so dispatch
    # the migration to a worker thread that gets its own clean loop.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert db_file.exists()

    # ---- 2) Issue a tenant, team, and key via the library path -----------
    engine = create_async_engine(db_url, connect_args={"check_same_thread": False})
    sm = create_sessionmaker(engine)

    full_active, prefix_active = generate_api_key("test")
    full_revoked, prefix_revoked = generate_api_key("test")

    async with sm() as session:
        tenant = Tenant(name="acme-e2e")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="eng-e2e")
        session.add(team)
        await session.flush()
        active = ApiKey(
            team_id=team.id,
            prefix=prefix_active,
            key_hash=hash_key(full_active),
            scopes="chat:write",
            label="e2e-active",
        )
        revoked = ApiKey(
            team_id=team.id,
            prefix=prefix_revoked,
            key_hash=hash_key(full_revoked),
            scopes="chat:write",
            label="e2e-revoked",
        )
        session.add_all([active, revoked])
        await session.flush()

        from datetime import datetime

        revoked.revoked_at = datetime.now(tz=UTC)
        await session.commit()

    # ---- 3) Build an app with this DB + mocked Groq upstream -------------
    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    settings = get_settings()
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)

    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_ok_body()))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- 4) Active key succeeds ----------------------------------
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {full_active}"},
            json={
                "model": "groq/llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "pong"

        # ---- 5) Revoked key fails ------------------------------------
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {full_revoked}"},
            json={
                "model": "groq/llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 401

    await registry.aclose()
    await engine.dispose()
    get_settings.cache_clear()
    # Windows tmp cleanup — remove explicitly, tmp_path sometimes sticks.
    if db_file.exists():
        with contextlib.suppress(PermissionError):
            os.remove(db_file)
