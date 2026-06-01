"""AWS event-stream binary frame parser (Phase 52).

AWS uses a proprietary binary framing protocol for streaming responses
from services like Bedrock, Lex, Transcribe, and S3 SelectObjectContent.
The content-type is ``application/vnd.amazon.eventstream`` and the
encoded form is NOT SSE — it's a length-prefixed binary frame format
with CRC32 checksums.

Why a pure-Python parser?
-------------------------
``botocore`` ships an event-stream parser (``botocore.eventstream``) but
it's tightly coupled to ``botocore``'s response model — it expects a
particular client wrapper that we don't use (the adapter speaks raw
``httpx`` for everything else, see :mod:`pronaos.providers.bedrock`).
A standalone parser stays consistent with the rest of the Bedrock
adapter's no-boto3-on-the-hot-path posture, and is small enough that
implementing it from the AWS spec is cheaper than fighting botocore's
abstractions.

Frame layout (per the AWS docs)
-------------------------------
::

    +-------------------------+
    | total_length (4 BE u32) |   bytes 0..4
    +-------------------------+
    | headers_length (4 BE)   |   bytes 4..8
    +-------------------------+
    | prelude_crc32 (4 BE u32)|   bytes 8..12  -- CRC32 of bytes [0, 8)
    +-------------------------+
    | headers (variable)      |   bytes 12 .. 12+headers_length
    +-------------------------+
    | payload (variable)      |   bytes 12+headers_length .. total_length-4
    +-------------------------+
    | message_crc32 (4 BE u32)|   last 4 bytes  -- CRC32 of bytes [0, total_length-4)
    +-------------------------+

``total_length`` INCLUDES the four prelude / trailer fields. So:

    payload_length = total_length - 16 - headers_length

Headers
-------
Each header inside the ``headers`` block::

    +-----------------+
    | name_length (1) |  uint8
    +-----------------+
    | name (N bytes)  |  ASCII, no terminator
    +-----------------+
    | value_type (1)  |  uint8 enum
    +-----------------+
    | value (variable)|  depends on value_type
    +-----------------+

Value types we decode:

- ``0``: ``true``  (no value bytes)
- ``1``: ``false`` (no value bytes)
- ``2``: byte (1 byte int8)
- ``3``: short (2 bytes int16 BE)
- ``4``: integer (4 bytes int32 BE)
- ``5``: long (8 bytes int64 BE)
- ``6``: byte-array (2 bytes length BE + raw bytes)
- ``7``: string (2 bytes length BE + UTF-8)
- ``8``: timestamp (8 bytes int64 BE; milliseconds since epoch)
- ``9``: UUID (16 bytes)

For Bedrock streaming, the headers we care about are all type 7
(strings): ``:message-type``, ``:event-type``, ``:content-type``.
"""

from __future__ import annotations

import struct
from binascii import crc32
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

_PRELUDE_LEN: Final = 12  # total_length(4) + headers_length(4) + prelude_crc(4)
_TRAILER_LEN: Final = 4  # message_crc(4)
_MIN_FRAME_LEN: Final = _PRELUDE_LEN + _TRAILER_LEN  # 16

# Maximum sane frame size — Bedrock chunks are typically < 4 KB; we
# accept up to 16 MB as a defence against junk inputs that would
# otherwise let an attacker convince us to wait for a colossal frame
# that will never arrive.
_MAX_FRAME_LEN: Final = 16 * 1024 * 1024


class EventStreamParseError(Exception):
    """Raised when a frame's CRC32 mismatches or its layout is malformed.

    The parser is strict: corrupt frames are fatal because mid-stream
    silent corruption would let a downstream LLM consumer ingest
    arbitrary bytes as model output. Better to fail the request loudly.
    """


@dataclass(frozen=True, slots=True)
class EventStreamFrame:
    """One parsed event-stream frame.

    ``headers`` carries the header name -> decoded-value map. For
    Bedrock streaming responses, this includes ``:message-type``
    (``event`` for normal chunks, ``exception`` for upstream errors),
    ``:event-type`` (``chunk``), and ``:content-type``
    (``application/json``).

    ``payload`` is the raw payload bytes. For Bedrock streaming, this
    is a JSON object whose ``bytes`` field is a base64-encoded JSON
    chunk (the actual model output) — see ``bedrock.py`` for the
    per-family chunk shapes.
    """

    headers: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""

    # Convenience helpers for the common Bedrock headers ----------------

    @property
    def message_type(self) -> str | None:
        v = self.headers.get(":message-type")
        return v if isinstance(v, str) else None

    @property
    def event_type(self) -> str | None:
        v = self.headers.get(":event-type")
        return v if isinstance(v, str) else None

    @property
    def is_exception(self) -> bool:
        return self.message_type == "exception"


# --------------------------------------------------------------------------- #
# Public parsing API                                                          #
# --------------------------------------------------------------------------- #


def parse_one_frame(
    buf: bytes,
) -> tuple[EventStreamFrame | None, int]:
    """Parse a single frame from the start of ``buf``.

    Returns ``(frame, n_consumed)`` when a complete frame is available;
    returns ``(None, 0)`` when ``buf`` doesn't yet contain a full frame
    (the caller should accumulate more bytes and retry).

    Raises :class:`EventStreamParseError` on:
    - prelude CRC32 mismatch
    - message CRC32 mismatch
    - implausibly large ``total_length`` (defence against truncation
      attacks where an attacker sets a huge length field)
    - malformed header layout
    """
    if len(buf) < _PRELUDE_LEN:
        return None, 0

    total_length, headers_length = struct.unpack(">II", buf[:8])
    prelude_crc_expected = struct.unpack(">I", buf[8:12])[0]

    # Sanity: total_length must include prelude + trailer + headers.
    if total_length < _MIN_FRAME_LEN + headers_length:
        raise EventStreamParseError(
            f"frame total_length {total_length} smaller than minimum "
            f"({_MIN_FRAME_LEN + headers_length})"
        )
    if total_length > _MAX_FRAME_LEN:
        raise EventStreamParseError(
            f"frame total_length {total_length} exceeds sanity cap ({_MAX_FRAME_LEN})"
        )

    if len(buf) < total_length:
        # Not enough bytes for the complete frame yet — caller should
        # keep reading.
        return None, 0

    prelude_crc_actual = crc32(buf[:8])
    if prelude_crc_actual != prelude_crc_expected:
        raise EventStreamParseError(
            f"prelude CRC32 mismatch: got {prelude_crc_actual:08x}, "
            f"expected {prelude_crc_expected:08x}"
        )

    # Message CRC covers EVERYTHING except the trailing 4-byte CRC itself.
    message_crc_expected = struct.unpack(">I", buf[total_length - 4 : total_length])[0]
    message_crc_actual = crc32(buf[: total_length - 4])
    if message_crc_actual != message_crc_expected:
        raise EventStreamParseError(
            f"message CRC32 mismatch: got {message_crc_actual:08x}, "
            f"expected {message_crc_expected:08x}"
        )

    headers_bytes = buf[_PRELUDE_LEN : _PRELUDE_LEN + headers_length]
    payload_bytes = buf[_PRELUDE_LEN + headers_length : total_length - _TRAILER_LEN]
    headers = _parse_headers(headers_bytes)
    return EventStreamFrame(headers=headers, payload=payload_bytes), total_length


async def iter_frames(
    byte_iter: AsyncIterator[bytes],
) -> AsyncIterator[EventStreamFrame]:
    """Async-iterate complete event-stream frames over a stream of byte
    chunks. Handles cross-chunk frame boundaries by accumulating
    partial bytes in a buffer until a full frame's bytes are present.

    If the source stream ends mid-frame, the trailing partial bytes
    are dropped silently (matches botocore's behaviour — the upstream
    closing the connection mid-frame is not necessarily corruption).
    """
    buf = bytearray()
    async for chunk in byte_iter:
        if not chunk:
            continue
        buf.extend(chunk)
        # Drain as many complete frames as the buffer now holds.
        while True:
            frame, consumed = parse_one_frame(bytes(buf))
            if frame is None or consumed == 0:
                break
            yield frame
            del buf[:consumed]


# --------------------------------------------------------------------------- #
# Header decoding                                                             #
# --------------------------------------------------------------------------- #


def _parse_headers(headers_bytes: bytes) -> dict[str, Any]:
    """Walk the headers block and produce the name -> value map.

    Raises :class:`EventStreamParseError` on malformed entries —
    truncated bytes, unknown value type, name length running past
    the buffer end, etc.
    """
    headers: dict[str, Any] = {}
    offset = 0
    length = len(headers_bytes)
    while offset < length:
        if offset + 1 > length:
            raise EventStreamParseError("truncated header: missing name length")
        name_length = headers_bytes[offset]
        offset += 1
        if offset + name_length + 1 > length:
            raise EventStreamParseError("truncated header: name + value_type runs past buffer")
        name = headers_bytes[offset : offset + name_length].decode("ascii")
        offset += name_length
        value_type = headers_bytes[offset]
        offset += 1
        value, consumed = _parse_header_value(value_type, headers_bytes, offset)
        offset += consumed
        headers[name] = value
    return headers


def _parse_header_value(value_type: int, buf: bytes, offset: int) -> tuple[Any, int]:
    """Decode a single header value starting at ``buf[offset]`` and
    return ``(value, n_consumed_from_offset)``.

    Each value type's wire encoding is fixed by the AWS spec — see
    module docstring for the table. Unknown types raise
    :class:`EventStreamParseError`; passing them through silently would
    risk header-name collisions later.
    """
    if value_type == 0:
        return True, 0
    if value_type == 1:
        return False, 0
    if value_type == 2:  # int8
        if offset + 1 > len(buf):
            raise EventStreamParseError("truncated int8 header value")
        return struct.unpack_from(">b", buf, offset)[0], 1
    if value_type == 3:  # int16
        if offset + 2 > len(buf):
            raise EventStreamParseError("truncated int16 header value")
        return struct.unpack_from(">h", buf, offset)[0], 2
    if value_type == 4:  # int32
        if offset + 4 > len(buf):
            raise EventStreamParseError("truncated int32 header value")
        return struct.unpack_from(">i", buf, offset)[0], 4
    if value_type == 5:  # int64
        if offset + 8 > len(buf):
            raise EventStreamParseError("truncated int64 header value")
        return struct.unpack_from(">q", buf, offset)[0], 8
    if value_type == 6:  # byte-array
        if offset + 2 > len(buf):
            raise EventStreamParseError("truncated byte-array length")
        n = struct.unpack_from(">H", buf, offset)[0]
        if offset + 2 + n > len(buf):
            raise EventStreamParseError("truncated byte-array payload")
        return bytes(buf[offset + 2 : offset + 2 + n]), 2 + n
    if value_type == 7:  # UTF-8 string
        if offset + 2 > len(buf):
            raise EventStreamParseError("truncated string length")
        n = struct.unpack_from(">H", buf, offset)[0]
        if offset + 2 + n > len(buf):
            raise EventStreamParseError("truncated string payload")
        return buf[offset + 2 : offset + 2 + n].decode("utf-8"), 2 + n
    if value_type == 8:  # timestamp (int64 millis)
        if offset + 8 > len(buf):
            raise EventStreamParseError("truncated timestamp header value")
        return struct.unpack_from(">q", buf, offset)[0], 8
    if value_type == 9:  # UUID (16 bytes)
        if offset + 16 > len(buf):
            raise EventStreamParseError("truncated UUID header value")
        return bytes(buf[offset : offset + 16]), 16
    raise EventStreamParseError(f"unknown header value type {value_type!r}")


# --------------------------------------------------------------------------- #
# Frame encoding — used by tests + the live verify script                     #
# --------------------------------------------------------------------------- #


def encode_string_header(name: str, value: str) -> bytes:
    """Encode one ``(name, string-value)`` header pair for use in
    test/verify fixtures that synthesize real Bedrock-shaped frames.

    Only the string (type 7) encoding is exposed here — all the Bedrock
    headers Pronaos cares about are strings.
    """
    name_bytes = name.encode("ascii")
    if len(name_bytes) > 255:
        raise EventStreamParseError(f"header name too long: {len(name_bytes)} bytes")
    value_bytes = value.encode("utf-8")
    if len(value_bytes) > 0xFFFF:
        raise EventStreamParseError(f"header value too long: {len(value_bytes)} bytes")
    return (
        bytes([len(name_bytes)])
        + name_bytes
        + bytes([7])
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def encode_frame(headers: dict[str, str], payload: bytes) -> bytes:
    """Encode a complete event-stream frame from headers + payload.

    Computes both CRCs correctly so the resulting bytes round-trip
    through :func:`parse_one_frame` cleanly. Test fixtures use this
    to build streams of realistic Bedrock frames without depending on
    botocore's encoder.
    """
    header_bytes = b"".join(encode_string_header(name, value) for name, value in headers.items())
    headers_length = len(header_bytes)
    total_length = _PRELUDE_LEN + headers_length + len(payload) + _TRAILER_LEN
    prelude = struct.pack(">II", total_length, headers_length)
    prelude_crc = struct.pack(">I", crc32(prelude))
    body_without_trailer = prelude + prelude_crc + header_bytes + payload
    message_crc = struct.pack(">I", crc32(body_without_trailer))
    return body_without_trailer + message_crc
