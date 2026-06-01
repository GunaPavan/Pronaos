"""Async SQLAlchemy engine + session helpers.

One engine per process, a sessionmaker that produces ``AsyncSession`` units of
work per request (or CLI command). Both SQLite (aiosqlite) and Postgres
(asyncpg) backends are supported transparently; we only special-case SQLite's
need for ``check_same_thread=False`` at the sync level.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from pronaos.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for this process."""
    url = settings.database_url
    kwargs: dict[str, object] = {
        "echo": False,
        "future": True,
    }
    if url.startswith("sqlite"):
        # aiosqlite is file-scoped — pool_pre_ping and common pool tuning are
        # meaningless.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # In-memory SQLite: each new connection gets its OWN empty
            # database. NullPool would mean every request opens a fresh
            # empty schema. StaticPool keeps ONE connection shared across
            # all sessions — the test-suite pattern.
            kwargs["poolclass"] = StaticPool
        else:
            # File-backed SQLite: use NullPool to open a fresh aiosqlite
            # connection per checkout and close it on release. Without this,
            # SQLAlchemy's default AsyncAdaptedQueuePool keeps connections
            # alive across requests. SQLite's transaction snapshot model
            # means a pooled connection that previously did a SELECT can
            # return STALE rows on subsequent SELECTs (the deferred
            # transaction's snapshot lags behind committed writes from
            # other connections). Reproducer: PUT a row → immediate GET on
            # the same client returns the pre-PUT value, then a SECOND GET
            # returns the post-PUT value (the pool handed out a different
            # connection on the third call). NullPool eliminates that
            # staleness window at the cost of one fresh connection per
            # request — fine for file-backed SQLite (no real connection
            # overhead) and the only safe default given the snapshot
            # semantics. Postgres/asyncpg doesn't have this issue
            # (MVCC + proper isolation), so it keeps the default pool below.
            kwargs["poolclass"] = NullPool
    else:
        # Postgres (or other) — conservative pool defaults; overridable by env
        # in later phases.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20

    return create_async_engine(url, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def get_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on clean exit and rolling back on error."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
