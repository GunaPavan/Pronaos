"""Unit tests for the Phase 38 PII tokenization core.

Three surfaces under test, none of which require a live gateway:

1. **Token derivation** — ``make_token()`` is deterministic per
   (tenant, value), salted by tenant_id, and produces the documented
   ``[TYPE_HASH]`` shape.
2. **TokenStore** — Redis round-trips via ``fakeredis.aioredis`` so we
   exercise the real ``SET``/``MGET`` paths (TTL semantics, pipeline
   batching, namespace isolation).
3. **StreamingDetokenizer** — chunk-boundary buffering: a token split
   across two chunks must still resolve correctly when both chunks
   arrive. Tokens that resolve in one chunk emit immediately;
   no-token chunks pass through; the final flush picks up trailing
   tokens at stream end.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

from pronaos.core.pii_tokens import (
    StreamingDetokenizer,
    TokenStore,
    make_token,
    tokenize_hits,
)

# --------------------------------------------------------------------------- #
# Token derivation                                                            #
# --------------------------------------------------------------------------- #


class TestMakeToken:
    def test_shape_is_TYPE_HASH(self) -> None:
        tok = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        assert tok.startswith("[EMAIL_")
        assert tok.endswith("]")
        # 12 hex chars between the underscore and the closing bracket.
        assert len(tok) == len("[EMAIL_") + 12 + 1

    def test_deterministic_same_tenant_same_value(self) -> None:
        a = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        b = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        assert a == b

    def test_different_tenants_get_different_tokens(self) -> None:
        a = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        b = make_token(tenant_id="t2", rule_name="pii.email", value="a@b.c")
        assert a != b

    def test_different_values_get_different_tokens(self) -> None:
        a = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        b = make_token(tenant_id="t1", rule_name="pii.email", value="x@y.z")
        assert a != b

    def test_rule_suffix_drives_type_label(self) -> None:
        # Suffix mapping is in _type_label_for. Aliases first:
        assert make_token(tenant_id="t", rule_name="pii.credit_card", value="v").startswith("[CC_")
        assert make_token(tenant_id="t", rule_name="pii.ipv4", value="v").startswith("[IPV4_")
        assert make_token(tenant_id="t", rule_name="pii.email", value="v").startswith("[EMAIL_")
        # Presidio rule families:
        assert make_token(tenant_id="t", rule_name="pii.person", value="v").startswith("[NAME_")
        assert make_token(tenant_id="t", rule_name="pii.location", value="v").startswith("[LOC_")
        # Unrecognised suffix passes through uppercased:
        assert make_token(tenant_id="t", rule_name="pii.weird_thing", value="v").startswith(
            "[WEIRD_THING_"
        )

    def test_no_dot_falls_back_to_pii(self) -> None:
        # A rule name without any dot falls back to type ``PII`` per the
        # implementation contract (``suffix = name.rsplit('.', 1)[-1]``
        # only when there's a dot; otherwise default ``PII``).
        tok = make_token(tenant_id="t", rule_name="injection", value="v")
        assert tok.startswith("[PII_")


# --------------------------------------------------------------------------- #
# tokenize_hits (pure helper)                                                 #
# --------------------------------------------------------------------------- #


class TestTokenizeHits:
    def test_no_hits_returns_text_unchanged(self) -> None:
        out, mappings = tokenize_hits(tenant_id="t", text="hello", hits=[])
        assert out == "hello"
        assert mappings == []

    def test_single_hit_substitutes(self) -> None:
        text = "Email me at a@b.c please"
        hits = [("pii.email", (12, 17), "a@b.c")]
        out, mappings = tokenize_hits(tenant_id="t", text=text, hits=hits)
        assert "a@b.c" not in out
        assert "[EMAIL_" in out
        assert len(mappings) == 1
        token, original = mappings[0]
        assert token.startswith("[EMAIL_")
        assert original == "a@b.c"

    def test_same_value_twice_produces_same_token(self) -> None:
        # Entity-tracking property: two mentions get the same token.
        text = "a@b.c and a@b.c again"
        hits = [
            ("pii.email", (0, 5), "a@b.c"),
            ("pii.email", (10, 15), "a@b.c"),
        ]
        out, mappings = tokenize_hits(tenant_id="t", text=text, hits=hits)
        # Both occurrences replaced; only one entry in mappings.
        assert out.count("a@b.c") == 0
        assert len(mappings) == 1
        token, _value = mappings[0]
        assert out.count(token) == 2

    def test_different_values_produce_different_tokens(self) -> None:
        text = "a@b.c and x@y.z"
        hits = [
            ("pii.email", (0, 5), "a@b.c"),
            ("pii.email", (10, 15), "x@y.z"),
        ]
        out, mappings = tokenize_hits(tenant_id="t", text=text, hits=hits)
        assert len(mappings) == 2
        tokens = {m[0] for m in mappings}
        assert len(tokens) == 2
        for tok in tokens:
            assert tok in out


# --------------------------------------------------------------------------- #
# TokenStore — round-trip via fakeredis                                       #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def redis_client():  # type: ignore[no-untyped-def]
    """Fresh fakeredis per test."""
    client = fakeredis_aio.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


class TestTokenStore:
    @pytest.mark.asyncio
    async def test_store_and_reverse_roundtrip(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        token = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        n = await store.store_many(tenant_id="t1", mappings=[(token, "a@b.c")], ttl_seconds=60)
        assert n == 1
        outcome = await store.reverse_text(
            tenant_id="t1", text=f"contact me at {token} for details"
        )
        assert "a@b.c" in outcome.text
        assert outcome.reversed_count == 1
        assert outcome.orphaned_count == 0

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """A token minted under tenant A doesn't resolve under tenant B."""
        store = TokenStore(redis_client)
        token = make_token(tenant_id="alice", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="alice", mappings=[(token, "a@b.c")], ttl_seconds=60)
        outcome = await store.reverse_text(tenant_id="bob", text=f"see {token} there")
        # Bob's store has nothing under that key. Token left as-is.
        assert token in outcome.text
        assert outcome.reversed_count == 0
        assert outcome.orphaned_count == 1

    @pytest.mark.asyncio
    async def test_orphaned_token_left_in_place(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """LLM-emitted token that was never minted stays in the response."""
        store = TokenStore(redis_client)
        fake = "[EMAIL_deadbeef1234]"
        outcome = await store.reverse_text(tenant_id="t1", text=f"see {fake}")
        assert fake in outcome.text
        assert outcome.orphaned_count == 1
        assert outcome.reversed_count == 0

    @pytest.mark.asyncio
    async def test_multiple_tokens_in_one_text(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        t_email = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        t_phone = make_token(tenant_id="t1", rule_name="pii.phone", value="555-1234")
        await store.store_many(
            tenant_id="t1",
            mappings=[(t_email, "a@b.c"), (t_phone, "555-1234")],
            ttl_seconds=60,
        )
        outcome = await store.reverse_text(
            tenant_id="t1", text=f"email {t_email} or call {t_phone}"
        )
        assert "a@b.c" in outcome.text
        assert "555-1234" in outcome.text
        assert outcome.reversed_count == 2

    @pytest.mark.asyncio
    async def test_repeated_token_in_text_counted_per_occurrence(  # type: ignore[no-untyped-def]
        self, redis_client
    ) -> None:
        store = TokenStore(redis_client)
        token = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="t1", mappings=[(token, "a@b.c")], ttl_seconds=60)
        outcome = await store.reverse_text(tenant_id="t1", text=f"see {token} and again {token}")
        assert outcome.text.count("a@b.c") == 2
        assert outcome.reversed_count == 2

    @pytest.mark.asyncio
    async def test_empty_store_call_returns_zero(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        n = await store.store_many(tenant_id="t1", mappings=[], ttl_seconds=60)
        assert n == 0

    @pytest.mark.asyncio
    async def test_text_without_tokens_passes_through(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        outcome = await store.reverse_text(tenant_id="t1", text="no tokens here, just plain text")
        assert outcome.text == "no tokens here, just plain text"
        assert outcome.reversed_count == 0
        assert outcome.orphaned_count == 0


# --------------------------------------------------------------------------- #
# StreamingDetokenizer — chunk-boundary handling                              #
# --------------------------------------------------------------------------- #


class TestStreamingDetokenizer:
    @pytest.mark.asyncio
    async def test_passes_chunk_without_token_through(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        detok = StreamingDetokenizer(store, tenant_id="t1")
        out = await detok.feed("plain text no tokens here at all")
        # All safe to emit immediately (no ``[`` anywhere).
        assert out == "plain text no tokens here at all"
        flush = await detok.flush()
        assert flush == ""

    @pytest.mark.asyncio
    async def test_complete_token_in_one_chunk_reverses(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        store = TokenStore(redis_client)
        token = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="t1", mappings=[(token, "a@b.c")], ttl_seconds=60)
        detok = StreamingDetokenizer(store, tenant_id="t1")
        out = await detok.feed(f"email {token} ok")
        assert "a@b.c" in out
        assert token not in out

    @pytest.mark.asyncio
    async def test_token_split_across_two_chunks(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """The hardest case — a token straddles a chunk boundary.

        The detokenizer must hold back the partial token until the next
        chunk arrives, then concatenate and reverse correctly."""
        store = TokenStore(redis_client)
        token = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="t1", mappings=[(token, "a@b.c")], ttl_seconds=60)
        # Split the token roughly in half across two chunks.
        # Token shape: [EMAIL_aaaaaaaaaaaa] = 20 chars; split at index 10.
        full = f"contact {token} now"
        split_at = full.index("[") + 6  # mid-token
        first = full[:split_at]
        second = full[split_at:]

        detok = StreamingDetokenizer(store, tenant_id="t1")
        out1 = await detok.feed(first)
        out2 = await detok.feed(second)
        tail = await detok.flush()
        joined = out1 + out2 + tail
        assert "a@b.c" in joined
        assert token not in joined

    @pytest.mark.asyncio
    async def test_token_at_end_flushes_correctly(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """Token landing at the very end of the stream is reversed on flush."""
        store = TokenStore(redis_client)
        token = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="t1", mappings=[(token, "a@b.c")], ttl_seconds=60)
        detok = StreamingDetokenizer(store, tenant_id="t1")
        out = await detok.feed(f"see {token}")
        tail = await detok.flush()
        joined = out + tail
        assert "a@b.c" in joined

    @pytest.mark.asyncio
    async def test_metric_accounting_aggregates(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """Per-type counters track reversed + orphaned across chunks."""
        store = TokenStore(redis_client)
        good = make_token(tenant_id="t1", rule_name="pii.email", value="a@b.c")
        await store.store_many(tenant_id="t1", mappings=[(good, "a@b.c")], ttl_seconds=60)
        orphan = "[EMAIL_deadbeef0000]"
        detok = StreamingDetokenizer(store, tenant_id="t1")
        await detok.feed(f"hello {good} and {orphan} end")
        await detok.flush()
        assert detok.reversed_total == 1
        assert detok.orphaned_total == 1
        assert detok.reversed_by_type == {"email": 1}
        assert detok.orphaned_by_type == {"email": 1}

    @pytest.mark.asyncio
    async def test_unrelated_open_bracket_passes_through(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        """A ``[`` that's not the start of a token must not stall the stream."""
        store = TokenStore(redis_client)
        detok = StreamingDetokenizer(store, tenant_id="t1")
        # ``[1]`` and ``[link]`` are NOT PII tokens — the buffer should
        # release them once the close bracket arrives.
        out = await detok.feed("see ref [1] for details and [link] elsewhere")
        tail = await detok.flush()
        joined = out + tail
        assert joined == "see ref [1] for details and [link] elsewhere"
