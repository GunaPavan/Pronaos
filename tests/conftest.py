"""Shared pytest fixtures.

Four test clients:

- ``client``: plain in-process ASGI. No lifespan runs, no DB, no registry. Use
  for fully stateless endpoints (health, readiness).
- ``client_with_registry``: installs a ``ProviderRegistry`` + ``Router`` onto
  ``app.state``. Does **not** install a DB — do not use for endpoints that
  require auth.
- ``authed_client`` / ``auth_setup``: bring up an in-memory SQLite DB, run
  migrations, seed a tenant + team + API key, and return a client plus the
  raw key so tests can send ``Authorization: Bearer <key>``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

# load_dotenv() must run BEFORE any pronaos import that triggers
# Settings() evaluation, which reads env vars at import time.
load_dotenv()

from pronaos.auth.api_keys import (  # noqa: E402
    generate_api_key,
    hash_key,
)
from pronaos.config import get_settings  # noqa: E402
from pronaos.core.quota import QuotaTracker  # noqa: E402
from pronaos.core.ratelimit import InMemoryRateLimiter  # noqa: E402
from pronaos.core.router import Router  # noqa: E402
from pronaos.db.models import ApiKey, Base, Team, Tenant  # noqa: E402
from pronaos.db.session import create_engine, create_sessionmaker  # noqa: E402
from pronaos.main import create_app  # noqa: E402
from pronaos.providers.registry import ProviderRegistry  # noqa: E402


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def client_with_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    get_settings.cache_clear()

    app = create_app()
    settings = get_settings()
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await registry.aclose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Auth-capable fixture                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthSetup:
    client: httpx.AsyncClient
    api_key: str  # full bearer token — use in Authorization header
    tenant_id: str
    team_id: str
    key_id: str
    revoked_key: str  # pre-revoked key for negative tests
    # Sessionmaker exposed so tests can mutate seeded rows (e.g. set a
    # team's allowed_models, guardrail_policy, etc.) without going
    # through the admin API. The ``streaming_setup`` fixture already
    # does this; keeping the shape consistent across fixtures.
    sm: Any


@pytest_asyncio.fixture
async def auth_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AuthSetup]:
    """Full app + in-memory SQLite DB + seeded tenant/team/active+revoked keys."""
    # Force an isolated in-memory SQLite DB for this test.
    monkeypatch.setenv("PRONAOS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-tests")
    # GROQ key is set so the cost-aware router (Phase 21) can resolve
    # model="auto" → groq/* in tests. Tests that mock Groq with respx
    # benefit from this; tests that don't touch Groq are unaffected.
    monkeypatch.setenv("GROQ_API_KEY", "test-key-for-tests")
    # Phase 42 — AWS credentials, so the registry can build the
    # ``bedrock`` provider for tests that exercise the Bedrock adapter.
    # The values are the AWS-published example creds — safe to commit
    # and deliberately useless for hitting real AWS, just enough to
    # make SigV4Auth produce a well-formed signed request.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_engine(settings)
    sm = create_sessionmaker(engine)

    # Create tables directly (Alembic would also work but is slower for unit
    # tests). The migration is tested separately in test_migration.py.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed tenant/team/keys.
    full_active, prefix_active = generate_api_key("test")
    full_revoked, prefix_revoked = generate_api_key("test")
    async with sm() as session:
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.flush()
        team = Team(tenant_id=tenant.id, name="engineering")
        session.add(team)
        await session.flush()

        active = ApiKey(
            team_id=team.id,
            prefix=prefix_active,
            key_hash=hash_key(full_active),
            scopes="chat:write",
            label="active-test",
        )
        revoked = ApiKey(
            team_id=team.id,
            prefix=prefix_revoked,
            key_hash=hash_key(full_revoked),
            scopes="chat:write",
            label="revoked-test",
        )
        session.add_all([active, revoked])
        await session.flush()

        from datetime import datetime

        revoked.revoked_at = datetime.now(tz=UTC)
        await session.commit()

        tenant_id = tenant.id
        team_id = team.id
        key_id = active.id

    # Build the app with full state.
    app = create_app()
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    registry = ProviderRegistry(settings)
    app.state.provider_registry = registry
    app.state.router = Router(registry, default_provider=None)
    # Phase 4 quota stack — in-memory limiter is correct for tests; QuotaTracker
    # is stateless so one instance is fine across all tests.
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.quota_tracker = QuotaTracker()

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield AuthSetup(
                client=c,
                api_key=full_active,
                tenant_id=tenant_id,
                team_id=team_id,
                key_id=key_id,
                revoked_key=full_revoked,
                sm=sm,
            )
    finally:
        await registry.aclose()
        await engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
