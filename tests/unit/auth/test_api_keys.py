"""Unit tests for API-key generation, hashing, and verification."""

from __future__ import annotations

import pytest

from pronaos.auth.api_keys import (
    KEY_PREFIX,
    _parse_key,
    generate_api_key,
    hash_key,
    verify_key,
)


class TestGenerate:
    def test_format(self) -> None:
        full, prefix = generate_api_key("live")
        parts = full.split("_")
        assert parts[0] == KEY_PREFIX
        assert parts[1] == "live"
        assert parts[2] == prefix
        assert len(parts[3]) > 0

    def test_prefix_is_12_hex_chars(self) -> None:
        _, prefix = generate_api_key("live")
        assert len(prefix) == 12
        int(prefix, 16)  # raises if non-hex

    def test_different_each_time(self) -> None:
        a, _ = generate_api_key("live")
        b, _ = generate_api_key("live")
        assert a != b

    def test_env_label_round_trips(self) -> None:
        full_live, _ = generate_api_key("live")
        full_test, _ = generate_api_key("test")
        assert full_live.split("_")[1] == "live"
        assert full_test.split("_")[1] == "test"


class TestHash:
    def test_hash_never_equals_key(self) -> None:
        full, _ = generate_api_key("test")
        h = hash_key(full)
        assert h != full
        assert h.startswith("$argon2")

    def test_same_key_produces_different_hashes(self) -> None:
        # argon2 uses random salts → identical input, different output.
        full, _ = generate_api_key("test")
        assert hash_key(full) != hash_key(full)


class TestParse:
    def test_well_formed(self) -> None:
        full, prefix = generate_api_key("test")
        parsed = _parse_key(full)
        assert parsed is not None
        parsed_prefix, parsed_full = parsed
        assert parsed_prefix == prefix
        assert parsed_full == full

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-a-key",
            "pn_only_two",
            "wrong_live_abcdef123456_body",
            "pn_live_tooshort_body",
        ],
    )
    def test_malformed_returns_none(self, bad: str) -> None:
        assert _parse_key(bad) is None


# --------------------------------------------------------------------------- #
# verify_key with real SQLite                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verify_accepts_valid_key(auth_setup) -> None:  # type: ignore[no-untyped-def]
    # Use the auth_setup fixture but grab its session directly.
    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    async with sm() as session:
        principal = await verify_key(session, auth_setup.api_key)
    assert principal is not None
    assert principal.tenant_id == auth_setup.tenant_id
    assert principal.team_id == auth_setup.team_id
    assert "chat:write" in principal.scopes


@pytest.mark.asyncio
async def test_verify_rejects_revoked_key(auth_setup) -> None:  # type: ignore[no-untyped-def]
    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    async with sm() as session:
        principal = await verify_key(session, auth_setup.revoked_key)
    assert principal is None


@pytest.mark.asyncio
async def test_verify_rejects_unknown_prefix(auth_setup) -> None:  # type: ignore[no-untyped-def]
    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    unknown = "fg_test_ffffffffffff_somebodypart"
    async with sm() as session:
        principal = await verify_key(session, unknown)
    assert principal is None


@pytest.mark.asyncio
async def test_verify_rejects_malformed(auth_setup) -> None:  # type: ignore[no-untyped-def]
    sm = auth_setup.client._transport.app.state.db_sessionmaker  # type: ignore[attr-defined]
    async with sm() as session:
        principal = await verify_key(session, "garbage")
    assert principal is None
