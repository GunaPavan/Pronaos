"""Unit tests for the AWS event-stream binary frame parser (Phase 52).

The parser handles the on-the-wire format Bedrock uses for streaming
responses — see :mod:`pronaos.providers.bedrock_eventstream` for the
frame layout. These tests exercise:

- Single-frame round trip via the test-only encoder
- Multi-frame stream
- Cross-chunk frame boundary (the parser must accumulate partial bytes)
- CRC32 validation (prelude + message; both must fail loudly)
- Type-specific header value parsing (string, int variants, byte-array, etc.)
- Truncated frames return ``(None, 0)`` so the caller reads more bytes

The end-to-end streaming chain (Bedrock -> gateway SSE) is exercised
by ``test_bedrock.py::TestBedrockStreaming`` and the live verify
script.
"""

from __future__ import annotations

import struct
from binascii import crc32
from collections.abc import AsyncIterator

import pytest

from pronaos.providers.bedrock_eventstream import (
    EventStreamFrame,
    EventStreamParseError,
    encode_frame,
    encode_string_header,
    iter_frames,
    parse_one_frame,
)


class TestSingleFrameRoundTrip:
    def test_simple_frame_round_trips_through_encoder_and_parser(self) -> None:
        frame_bytes = encode_frame(
            headers={
                ":message-type": "event",
                ":event-type": "chunk",
                ":content-type": "application/json",
            },
            payload=b'{"bytes":"aGVsbG8="}',
        )
        frame, consumed = parse_one_frame(frame_bytes)
        assert consumed == len(frame_bytes)
        assert isinstance(frame, EventStreamFrame)
        assert frame.headers == {
            ":message-type": "event",
            ":event-type": "chunk",
            ":content-type": "application/json",
        }
        assert frame.payload == b'{"bytes":"aGVsbG8="}'
        # Property helpers
        assert frame.message_type == "event"
        assert frame.event_type == "chunk"
        assert frame.is_exception is False

    def test_exception_frame_flagged(self) -> None:
        frame_bytes = encode_frame(
            headers={":message-type": "exception"},
            payload=b"throttled",
        )
        frame, _ = parse_one_frame(frame_bytes)
        assert frame is not None
        assert frame.is_exception is True

    def test_empty_payload_ok(self) -> None:
        frame_bytes = encode_frame(headers={":message-type": "event"}, payload=b"")
        frame, _ = parse_one_frame(frame_bytes)
        assert frame is not None
        assert frame.payload == b""


class TestTruncation:
    def test_empty_buffer_returns_none_zero(self) -> None:
        assert parse_one_frame(b"") == (None, 0)

    def test_prelude_only_returns_none_zero(self) -> None:
        # Need at least 12 bytes; give it 11
        assert parse_one_frame(b"\x00" * 11) == (None, 0)

    def test_full_prelude_but_short_body_returns_none_zero(self) -> None:
        # Construct a real prelude that claims total_length=100 but
        # provide only 50 bytes total. Parser should return None,0
        # — the buffer just isn't full yet.
        frame_bytes = encode_frame(
            headers={":message-type": "event"},
            payload=b"x" * 100,
        )
        # truncate to 50 bytes
        truncated = frame_bytes[:50]
        result = parse_one_frame(truncated)
        assert result == (None, 0)


class TestCrcValidation:
    def test_prelude_crc_mismatch_raises(self) -> None:
        frame_bytes = bytearray(encode_frame(headers={":message-type": "event"}, payload=b"x"))
        # Flip a bit in the prelude CRC (bytes 8..12).
        frame_bytes[8] ^= 0x01
        with pytest.raises(EventStreamParseError, match="prelude CRC32"):
            parse_one_frame(bytes(frame_bytes))

    def test_message_crc_mismatch_raises(self) -> None:
        frame_bytes = bytearray(encode_frame(headers={":message-type": "event"}, payload=b"hello"))
        # Flip a bit in the payload — message CRC will no longer match.
        frame_bytes[20] ^= 0x01
        with pytest.raises(EventStreamParseError, match="message CRC32"):
            parse_one_frame(bytes(frame_bytes))

    def test_implausibly_large_total_length_rejected(self) -> None:
        # 32 MB total length should be rejected.
        buf = bytearray(16)
        struct.pack_into(">II", buf, 0, 32 * 1024 * 1024, 0)
        # Compute a valid prelude CRC so the parser gets past that check.
        struct.pack_into(">I", buf, 8, crc32(bytes(buf[:8])))
        with pytest.raises(EventStreamParseError, match="exceeds sanity cap"):
            parse_one_frame(bytes(buf))


class TestHeaderValueTypes:
    def test_string_header(self) -> None:
        frame_bytes = encode_frame(headers={":message-type": "event"}, payload=b"")
        frame, _ = parse_one_frame(frame_bytes)
        assert frame is not None
        assert frame.headers[":message-type"] == "event"

    def test_true_false_no_value_string_int_byte_array_timestamp_uuid(
        self,
    ) -> None:
        """Hand-encode a frame whose headers exercise the int / byte-array /
        timestamp / UUID / true / false value-type branches. The
        public encoder only emits strings, so we encode by hand here."""
        # Build header bytes manually so we hit non-string types.
        parts: list[bytes] = []
        # name="bool_t", type=0 (true), no value
        parts.append(bytes([6]) + b"bool_t" + bytes([0]))
        # name="bool_f", type=1 (false), no value
        parts.append(bytes([6]) + b"bool_f" + bytes([1]))
        # name="i8", type=2, value=-7
        parts.append(bytes([2]) + b"i8" + bytes([2]) + struct.pack(">b", -7))
        # name="i16", type=3, value=1234
        parts.append(bytes([3]) + b"i16" + bytes([3]) + struct.pack(">h", 1234))
        # name="i32", type=4, value=987654
        parts.append(bytes([3]) + b"i32" + bytes([4]) + struct.pack(">i", 987654))
        # name="i64", type=5, value=1<<40
        parts.append(bytes([3]) + b"i64" + bytes([5]) + struct.pack(">q", 1 << 40))
        # name="ba", type=6, value=b"abc"
        parts.append(bytes([2]) + b"ba" + bytes([6]) + struct.pack(">H", 3) + b"abc")
        # name="str", type=7, value="hi" (also exercised elsewhere — keep for completeness)
        parts.append(bytes([3]) + b"str" + bytes([7]) + struct.pack(">H", 2) + b"hi")
        # name="ts", type=8, value=1_700_000_000_000
        parts.append(bytes([2]) + b"ts" + bytes([8]) + struct.pack(">q", 1_700_000_000_000))
        # name="uuid", type=9, value=16 bytes of 0xAB
        parts.append(bytes([4]) + b"uuid" + bytes([9]) + bytes([0xAB] * 16))
        header_bytes = b"".join(parts)

        # Now wrap as a complete frame
        payload = b"P"
        total_length = 12 + len(header_bytes) + len(payload) + 4
        prelude = struct.pack(">II", total_length, len(header_bytes))
        prelude_crc = struct.pack(">I", crc32(prelude))
        body = prelude + prelude_crc + header_bytes + payload
        message_crc = struct.pack(">I", crc32(body))
        frame_bytes = body + message_crc

        frame, consumed = parse_one_frame(frame_bytes)
        assert consumed == total_length
        assert frame is not None
        h = frame.headers
        assert h["bool_t"] is True
        assert h["bool_f"] is False
        assert h["i8"] == -7
        assert h["i16"] == 1234
        assert h["i32"] == 987654
        assert h["i64"] == 1 << 40
        assert h["ba"] == b"abc"
        assert h["str"] == "hi"
        assert h["ts"] == 1_700_000_000_000
        assert h["uuid"] == bytes([0xAB] * 16)

    def test_unknown_header_value_type_raises(self) -> None:
        # Build a frame whose header has value_type = 99 (undefined).
        header_bytes = bytes([1]) + b"x" + bytes([99])
        payload = b""
        total_length = 12 + len(header_bytes) + len(payload) + 4
        prelude = struct.pack(">II", total_length, len(header_bytes))
        prelude_crc = struct.pack(">I", crc32(prelude))
        body = prelude + prelude_crc + header_bytes + payload
        message_crc = struct.pack(">I", crc32(body))
        with pytest.raises(EventStreamParseError, match="unknown header value type"):
            parse_one_frame(body + message_crc)

    def test_string_header_too_long_for_encoder(self) -> None:
        with pytest.raises(EventStreamParseError, match="header value too long"):
            encode_string_header("name", "x" * (0x10000))

    def test_string_header_name_too_long_for_encoder(self) -> None:
        with pytest.raises(EventStreamParseError, match="header name too long"):
            encode_string_header("x" * 256, "v")


class TestIterFrames:
    @pytest.mark.asyncio
    async def test_multi_frame_stream(self) -> None:
        # Concatenate three frames in one chunk; iter_frames should yield 3.
        frames_in = [
            encode_frame(headers={":message-type": "event"}, payload=b"one"),
            encode_frame(headers={":message-type": "event"}, payload=b"two"),
            encode_frame(headers={":message-type": "event"}, payload=b"three"),
        ]
        full = b"".join(frames_in)

        async def _src() -> AsyncIterator[bytes]:
            yield full

        out = [f.payload async for f in iter_frames(_src())]
        assert out == [b"one", b"two", b"three"]

    @pytest.mark.asyncio
    async def test_cross_chunk_frame_boundary(self) -> None:
        """A single frame split across two byte chunks must parse
        once the second chunk arrives — the parser holds the partial
        buffer in between."""
        frame_bytes = encode_frame(headers={":message-type": "event"}, payload=b"hello-world")
        # Split the frame at an awkward offset.
        split_at = 7
        chunk_a, chunk_b = frame_bytes[:split_at], frame_bytes[split_at:]

        async def _src() -> AsyncIterator[bytes]:
            yield chunk_a
            yield chunk_b

        out = [f.payload async for f in iter_frames(_src())]
        assert out == [b"hello-world"]

    @pytest.mark.asyncio
    async def test_empty_byte_chunks_skipped(self) -> None:
        frame_bytes = encode_frame(headers={":message-type": "event"}, payload=b"ok")

        async def _src() -> AsyncIterator[bytes]:
            yield b""
            yield frame_bytes
            yield b""

        out = [f.payload async for f in iter_frames(_src())]
        assert out == [b"ok"]

    @pytest.mark.asyncio
    async def test_stream_ending_mid_frame_drops_partial_silently(self) -> None:
        """If the upstream connection closes before a complete frame is
        delivered, the parser drops the trailing partial bytes rather
        than raising. The frame's worth of completed data is already
        on the consumer's plate."""
        frame_one = encode_frame(headers={":message-type": "event"}, payload=b"good")
        frame_two = encode_frame(headers={":message-type": "event"}, payload=b"truncated-soon")
        # Half of the second frame
        truncated = frame_two[: len(frame_two) // 2]

        async def _src() -> AsyncIterator[bytes]:
            yield frame_one
            yield truncated

        out = [f.payload async for f in iter_frames(_src())]
        assert out == [b"good"]
