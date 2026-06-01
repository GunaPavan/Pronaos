"""AuditLogger + AuditVerifier round-trip and tamper-detection tests.

The interesting properties:

1. **Hash function is deterministic + canonical** — same inputs always
   produce the same hash, byte-for-byte. The writer and verifier
   share ``hash_inputs`` so any drift breaks both.
2. **Chain links forward correctly** — record N+1's ``prev_hash``
   equals record N's ``this_hash``. The genesis record has
   ``prev_hash == ""``.
3. **Tampering is detectable** — modifying any field (or deleting a
   row) breaks verification at the first row whose recomputed hash
   doesn't match what's stored.
4. **Tenants are isolated** — tenant A's chain doesn't reference
   tenant B's records; rebuilding A's chain doesn't require B's data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pronaos.audit.logger import AuditLogger, hash_body, hash_inputs
from pronaos.audit.verifier import AuditVerifier
from pronaos.db.models import AuditRecord, Base

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def sm(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Fresh per-test SQLite DB so chains don't bleed across tests."""
    db = tmp_path / "audit.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _append(
    sm,
    *,
    tenant_id: str = "tenant-a",
    team_id: str = "team-1",
    key_id: str = "key-1",
    provider: str = "groq",
    model: str = "groq/llama-3.1-8b-instant",
    request_body: dict | None = None,
    response_body: dict | None = None,
    request_id: str | None = None,
) -> AuditRecord:
    """Helper: append one record + commit, return the AuditRecord."""
    logger = AuditLogger()
    async with sm() as session:
        rec = await logger.append(
            session,
            tenant_id=tenant_id,
            team_id=team_id,
            key_id=key_id,
            provider=provider,
            model=model,
            request_body=request_body or {"messages": [{"role": "user", "content": "hi"}]},
            response_body=response_body or {"choices": [{"index": 0}]},
            request_id=request_id,
        )
        await session.commit()
    assert rec is not None
    return rec


# --------------------------------------------------------------------------- #
# Hash function is deterministic                                              #
# --------------------------------------------------------------------------- #


def test_hash_inputs_deterministic() -> None:
    """Same inputs produce same hash. Byte-for-byte. This is the
    bedrock property the entire chain depends on."""
    kwargs = dict(
        prev_hash="abc",
        request_id="req-1",
        tenant_id="t1",
        team_id="team-1",
        key_id="key-1",
        provider="groq",
        model="groq/llama-3.1-8b-instant",
        ts_iso="2026-05-17T10:00:00+00:00",
        request_hash="r1",
        response_hash="s1",
    )
    h1 = hash_inputs(**kwargs)
    h2 = hash_inputs(**kwargs)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_inputs_changes_with_any_field() -> None:
    """Flipping any single input field MUST change the hash. Otherwise
    a verifier couldn't distinguish a tampered-with field from clean."""
    base = dict(
        prev_hash="abc",
        request_id="req-1",
        tenant_id="t1",
        team_id="team-1",
        key_id="key-1",
        provider="groq",
        model="m1",
        ts_iso="t",
        request_hash="r1",
        response_hash="s1",
    )
    h_base = hash_inputs(**base)
    for field in base:
        flipped = dict(base, **{field: base[field] + "X"})
        assert hash_inputs(**flipped) != h_base, (
            f"hash didn't change when {field!r} was modified — tamper "
            f"detection broken for that field"
        )


def test_hash_body_normalizes_key_order() -> None:
    """Dict key order MUST NOT affect the body hash. Otherwise
    JSON shape differences (Python's hash seed, client ordering) would
    falsely break chains."""
    a = {"x": 1, "y": [2, 3]}
    b = {"y": [2, 3], "x": 1}
    assert hash_body(a) == hash_body(b)


# --------------------------------------------------------------------------- #
# Chain construction                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_genesis_record_has_empty_prev_hash(sm) -> None:  # type: ignore[no-untyped-def]
    """First record for a tenant: prev_hash = "" by construction."""
    rec = await _append(sm)
    assert rec.prev_hash == ""
    assert rec.this_hash  # must be set
    assert len(rec.this_hash) == 64


@pytest.mark.asyncio
async def test_chain_links_forward(sm) -> None:  # type: ignore[no-untyped-def]
    """Append three records; verify N+1.prev_hash == N.this_hash."""
    a = await _append(sm, request_body={"q": 1})
    b = await _append(sm, request_body={"q": 2})
    c = await _append(sm, request_body={"q": 3})
    assert b.prev_hash == a.this_hash
    assert c.prev_hash == b.this_hash
    # Each this_hash is distinct.
    assert len({a.this_hash, b.this_hash, c.this_hash}) == 3


@pytest.mark.asyncio
async def test_per_tenant_chains_isolated(sm) -> None:  # type: ignore[no-untyped-def]
    """Tenant A and Tenant B have independent chains. A's tail is NOT
    in B's prev_hash anywhere."""
    a1 = await _append(sm, tenant_id="tenant-a", request_body={"q": "a1"})
    b1 = await _append(sm, tenant_id="tenant-b", request_body={"q": "b1"})
    a2 = await _append(sm, tenant_id="tenant-a", request_body={"q": "a2"})

    # b1 was inserted between a1 and a2 by wall-clock, but it must NOT
    # appear in tenant-a's chain — a2.prev_hash references a1, not b1.
    assert a2.prev_hash == a1.this_hash
    # And b1 is its own tenant's genesis.
    assert b1.prev_hash == ""


# --------------------------------------------------------------------------- #
# Verifier                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verify_intact_chain_returns_clean(sm) -> None:  # type: ignore[no-untyped-def]
    """A chain that's never been tampered with → 0 breaks, all rows
    verified."""
    for i in range(5):
        await _append(sm, request_body={"q": i})

    async with sm() as session:
        result = await AuditVerifier().verify(session, "tenant-a")
    assert result.total_records == 5
    assert result.verified_records == 5
    assert result.breaks == []
    assert result.is_intact


@pytest.mark.asyncio
async def test_verify_detects_field_tampering(sm) -> None:  # type: ignore[no-untyped-def]
    """Modify one record's request_hash AFTER write. The verifier MUST
    detect it because the recomputed this_hash no longer matches the
    stored this_hash."""
    a = await _append(sm, request_body={"q": "real"})
    await _append(sm, request_body={"q": "next"})

    # Tamper: overwrite a's request_hash without updating this_hash.
    async with sm() as session:
        await session.execute(
            update(AuditRecord).where(AuditRecord.id == a.id).values(request_hash="x" * 64)
        )
        await session.commit()

    async with sm() as session:
        result = await AuditVerifier().verify(session, "tenant-a")
    assert not result.is_intact
    assert any(b.reason == "hash_mismatch" for b in result.breaks)


@pytest.mark.asyncio
async def test_verify_detects_deleted_predecessor(sm) -> None:  # type: ignore[no-untyped-def]
    """Delete a middle row. The next row's prev_hash no longer matches
    its (new) predecessor's this_hash → verifier flags
    prev_hash_mismatch."""
    a = await _append(sm)
    b = await _append(sm)
    c = await _append(sm)

    # Delete b. a still exists; c.prev_hash still points to b's
    # this_hash, which now has no predecessor row.
    async with sm() as session:
        rec = await session.get(AuditRecord, b.id)
        await session.delete(rec)
        await session.commit()

    async with sm() as session:
        result = await AuditVerifier().verify(session, "tenant-a")
    # After deletion, only a and c remain. c.prev_hash == b.this_hash,
    # but verifier expects c.prev_hash == a.this_hash → break.
    assert not result.is_intact
    assert any(b.reason == "prev_hash_mismatch" for b in result.breaks)


@pytest.mark.asyncio
async def test_verify_handles_empty_tenant(sm) -> None:  # type: ignore[no-untyped-def]
    """No records for the tenant → 0 breaks, technically intact (vacuous
    truth). The verifier shouldn't crash on empty input."""
    async with sm() as session:
        result = await AuditVerifier().verify(session, "tenant-nonexistent")
    assert result.total_records == 0
    assert result.breaks == []
    assert result.is_intact


# --------------------------------------------------------------------------- #
# Fail-open                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_returns_none_when_session_is_broken() -> None:
    """A broken session causes ``append`` to log and return None — the
    chat handler treats that as 'audit gap' rather than '5xx the
    client.' Same fail-open contract as usage_records writes."""

    class BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("db is down")

        def add(self, *args, **kwargs):
            raise RuntimeError("db is down")

        async def flush(self) -> None:
            raise RuntimeError("db is down")

    logger = AuditLogger()
    result = await logger.append(
        BrokenSession(),  # type: ignore[arg-type]
        tenant_id="t",
        team_id="t",
        key_id="k",
        provider="p",
        model="m",
        request_body={},
        response_body={},
    )
    assert result is None
