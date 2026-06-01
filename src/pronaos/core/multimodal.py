"""Multi-modal (image) input helpers (Phase 41).

OpenAI and Anthropic ship the same capability — pass an image with the
prompt, model describes / reasons about it — through very different
wire shapes. Pronaos translates between them at the gateway so clients
can ship one canonical shape regardless of underlying provider.

Canonical shape (incoming, OpenAI-style)
----------------------------------------
The ``messages[i].content`` field becomes a list of parts:

    {
      "role": "user",
      "content": [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "https://..."}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      ]
    }

The text + image parts are siblings. A message can contain any mix.
For backward compat, ``content: "plain string"`` still works; the
chat handler treats it as a single text part.

Outbound (OpenAI-compat path)
-----------------------------
Verbatim pass-through. Every OpenAI-compat upstream (Groq vision
models, OpenAI gpt-4o-vision, Together, Fireworks, etc.) expects
exactly this shape.

Outbound (Anthropic native)
---------------------------
Anthropic's shape is::

    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    {"type": "image", "source": {"type": "url", "url": "https://..."}}

We translate ``image_url`` parts to ``image`` parts:

- ``data:image/png;base64,...`` → ``source: {type: base64, media_type: image/png, data: ...}``
- ``https://...`` → ``source: {type: url, url: ...}``

Text parts pass through (Anthropic also uses ``{"type": "text", ...}``).

Cost math
---------
OpenAI gpt-4o vision: ``85 + 170 * num_tiles`` where tiles are 512x512
after scaling. We implement the documented OpenAI sizing algorithm.

Anthropic Claude vision: tokens ≈ ``(width * height) / 750`` per
their docs. Capped at ~1568 for the largest supported input.

Both formulas need image dimensions, which we can read from PNG/JPEG
headers without a full decode (saves ~250 MB pillow dependency for
this one feature). For URL-based images we conservatively estimate
the maximum token cost (assume largest supported resolution).
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #


# OpenAI gpt-4o vision sizing per https://platform.openai.com/docs/guides/vision.
_OPENAI_TILE_TOKENS: Final = 170
_OPENAI_BASE_TOKENS: Final = 85
_OPENAI_TILE_SIZE: Final = 512
_OPENAI_SHORT_SIDE: Final = 768
_OPENAI_LONG_SIDE: Final = 2048

# Anthropic Claude vision approximate token formula. Source:
# Anthropic docs ("Each image will be tokenized at roughly
# (width x height) / 750 tokens"). Hard ceiling enforced server-side
# at ~1568 tokens.
_ANTHROPIC_TOKENS_PER_PIXEL: Final = 1.0 / 750.0
_ANTHROPIC_MAX_TOKENS_PER_IMAGE: Final = 1568

# Conservative max-resolution fallback when we can't measure the image
# (URL-based, malformed headers, unsupported format). Picks the upper
# bound so cost preflight is never under-counted.
_FALLBACK_TOKENS: Final = 1500

_DATA_URI_RE: Final = re.compile(
    r"^data:(?P<media_type>[a-zA-Z0-9/+.\-]+);base64,(?P<data>[A-Za-z0-9+/=]+)$"
)


# --------------------------------------------------------------------------- #
# Image content classification                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image part extracted from a multi-modal message content list.

    ``base64_bytes`` is the *decoded* byte count when the source is a
    data URI; 0 when the source is an HTTPS URL (the gateway doesn't
    fetch URLs to measure). ``media_type`` is the MIME type
    (``image/png``, ``image/jpeg``, ``image/gif``, ``image/webp``).
    ``url`` is the original URL or data URI string — kept verbatim so
    downstream translation has the raw source.
    """

    url: str
    media_type: str
    base64_bytes: int


@dataclass(frozen=True, slots=True)
class MessageImageInventory:
    """All image parts and their cumulative byte total for one request.

    Carries enough information for: (a) the size-cap gate, (b) the
    token-count math, (c) the metric labels. Built once at the front
    of the chat handler and threaded through.
    """

    parts: list[ImagePart]
    total_base64_bytes: int


def inventory_images(messages: list[dict[str, Any]]) -> MessageImageInventory:
    """Walk ``messages`` and pull out every image part.

    Handles three content shapes:

    1. ``content: "string"`` — text-only, no images.
    2. ``content: [{"type": "text", ...}, {"type": "image_url", ...}]``
       — OpenAI multi-modal. Image parts extracted.
    3. ``content: [{"type": "text", ...}, {"type": "image", "source": ...}]``
       — Anthropic-native shape (passed by a client that already speaks
       Anthropic). Image parts also extracted.

    Returns the inventory regardless of the upstream provider — the
    inventory is for *gateway-side* enforcement (size cap, metrics),
    not for translation."""
    parts: list[ImagePart] = []
    total_bytes = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            extracted = _extract_image_part(part)
            if extracted is not None:
                parts.append(extracted)
                total_bytes += extracted.base64_bytes
    return MessageImageInventory(parts=parts, total_base64_bytes=total_bytes)


def _extract_image_part(part: dict[str, Any]) -> ImagePart | None:
    """Return an ImagePart when ``part`` is an image; None otherwise.

    Two shapes recognised: OpenAI ``image_url`` and Anthropic ``image``.
    """
    ptype = part.get("type")
    if ptype == "image_url":
        url_block = part.get("image_url")
        if not isinstance(url_block, dict):
            return None
        url = url_block.get("url")
        if not isinstance(url, str):
            return None
        return _from_image_url(url)
    if ptype == "image":
        source = part.get("source")
        if not isinstance(source, dict):
            return None
        return _from_anthropic_source(source)
    return None


def _from_image_url(url: str) -> ImagePart | None:
    """Build an ImagePart from an OpenAI-style ``image_url.url`` value.

    Data URIs are decoded just enough to compute the byte length;
    HTTPS URLs pass through with byte count 0 (we don't fetch).
    """
    m = _DATA_URI_RE.match(url)
    if m is not None:
        media_type = m.group("media_type")
        data = m.group("data")
        # Computing the exact decoded byte count: base64 expands by
        # 4/3, so the raw size is roughly (len(data) * 3) // 4 minus
        # padding. We measure exactly by counting non-pad chars.
        pad = data.count("=")
        decoded_len = (len(data) * 3) // 4 - pad
        return ImagePart(url=url, media_type=media_type, base64_bytes=decoded_len)
    if url.startswith(("http://", "https://")):
        return ImagePart(url=url, media_type="application/octet-stream", base64_bytes=0)
    # Anything else (raw base64 without prefix, malformed URL) — treat
    # as opaque pass-through with unknown byte count. Operators can
    # tighten this if they see abuse.
    return ImagePart(url=url, media_type="application/octet-stream", base64_bytes=0)


def _from_anthropic_source(source: dict[str, Any]) -> ImagePart | None:
    """Build an ImagePart from an Anthropic-native ``image.source``."""
    src_type = source.get("type")
    if src_type == "base64":
        data = source.get("data")
        media_type = source.get("media_type", "image/png")
        if not isinstance(data, str) or not isinstance(media_type, str):
            return None
        pad = data.count("=")
        decoded_len = (len(data) * 3) // 4 - pad
        return ImagePart(
            url=f"data:{media_type};base64,{data}",
            media_type=media_type,
            base64_bytes=decoded_len,
        )
    if src_type == "url":
        url = source.get("url")
        if not isinstance(url, str):
            return None
        return ImagePart(url=url, media_type="application/octet-stream", base64_bytes=0)
    return None


# --------------------------------------------------------------------------- #
# OpenAI → Anthropic translation                                              #
# --------------------------------------------------------------------------- #


def translate_messages_for_anthropic(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate multi-modal ``image_url`` parts to Anthropic ``image`` blocks.

    Walks ``messages`` and rewrites each message's content list in
    place (returns a new list — does NOT mutate). Text parts pass
    through. Existing Anthropic-native ``image`` parts also pass
    through unchanged (defensive — advanced clients sometimes ship
    the native shape).

    Single-string content (no image parts) returns unchanged.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            # Plain string or None content — Anthropic accepts both.
            out.append(dict(msg))
            continue
        new_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                # Unknown shape — pass through. Upstream provider's own
                # validation handles rejection.
                new_parts.append(part)
                continue
            ptype = part.get("type")
            if ptype == "text":
                new_parts.append(part)
                continue
            if ptype == "image_url":
                anthropic_part = _translate_image_url_to_anthropic(part)
                if anthropic_part is not None:
                    new_parts.append(anthropic_part)
                continue
            # ``image`` parts (already native) and anything else pass
            # through verbatim. Anthropic will reject what it doesn't
            # recognise; not our job to second-guess.
            new_parts.append(part)
        new_msg = dict(msg)
        new_msg["content"] = new_parts
        out.append(new_msg)
    return out


def _translate_image_url_to_anthropic(part: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one ``image_url`` part to an Anthropic ``image`` part."""
    url_block = part.get("image_url")
    if not isinstance(url_block, dict):
        return None
    url = url_block.get("url")
    if not isinstance(url, str):
        return None
    m = _DATA_URI_RE.match(url)
    if m is not None:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": m.group("media_type"),
                "data": m.group("data"),
            },
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    # Unknown shape — refuse to translate, drop the part. Logging the
    # drop is the chat handler's responsibility; this helper stays
    # silent.
    return None


# --------------------------------------------------------------------------- #
# Token-count math                                                            #
# --------------------------------------------------------------------------- #


def estimate_image_tokens(part: ImagePart, *, model: str) -> int:
    """Estimate the token cost of one image for ``model``.

    Branches on the model family:

    - ``openai/gpt-4o*`` / ``openai/o1*`` — OpenAI tile algorithm.
    - ``anthropic/claude*`` / ``groq/llama-3.2-*vision*`` — area formula
      with the Anthropic ceiling. Groq's vision models bill similarly
      per their pricing docs.
    - Anything else — conservative fallback (1500 tokens).

    Returns 0 when ``base64_bytes`` is 0 AND we can't measure the
    image — the caller's cost preflight then under-estimates by
    ~1500 tokens, which the post-flight billing corrects. Better than
    crashing on a URL we can't fetch."""
    dims = _read_image_dimensions(part) if part.base64_bytes > 0 else None
    family = model.split("/", 1)[0].lower()
    if family in ("openai",) or model.startswith("openai/"):
        return _openai_tile_tokens(dims)
    return _anthropic_area_tokens(dims)


def _openai_tile_tokens(dims: tuple[int, int] | None) -> int:
    """Compute OpenAI gpt-4o vision token count for an image of size ``dims``.

    Algorithm (OpenAI docs, "Calculating costs"):

    1. Scale the image to fit within 2048x2048 (preserving aspect).
    2. Then scale so the shortest side is 768.
    3. Tile the result into 512x512 tiles, ceiling.
    4. Tokens = 85 + 170 * num_tiles.

    Falls back to ``_FALLBACK_TOKENS`` when dims unknown.
    """
    if dims is None:
        return _FALLBACK_TOKENS
    w, h = dims
    if w <= 0 or h <= 0:
        return _FALLBACK_TOKENS
    # Step 1: fit within 2048x2048.
    longest = max(w, h)
    if longest > _OPENAI_LONG_SIDE:
        scale = _OPENAI_LONG_SIDE / longest
        w = int(w * scale)
        h = int(h * scale)
    # Step 2: scale so shortest side is 768.
    shortest = min(w, h)
    if shortest > _OPENAI_SHORT_SIDE:
        scale = _OPENAI_SHORT_SIDE / shortest
        w = int(w * scale)
        h = int(h * scale)
    # Step 3: tile.
    tiles_w = (w + _OPENAI_TILE_SIZE - 1) // _OPENAI_TILE_SIZE
    tiles_h = (h + _OPENAI_TILE_SIZE - 1) // _OPENAI_TILE_SIZE
    return _OPENAI_BASE_TOKENS + _OPENAI_TILE_TOKENS * tiles_w * tiles_h


def _anthropic_area_tokens(dims: tuple[int, int] | None) -> int:
    """Compute Anthropic / Groq-vision token count.

    Area-based formula capped at ``_ANTHROPIC_MAX_TOKENS_PER_IMAGE``.
    Falls back to ``_FALLBACK_TOKENS`` when dims unknown.
    """
    if dims is None:
        return _FALLBACK_TOKENS
    w, h = dims
    if w <= 0 or h <= 0:
        return _FALLBACK_TOKENS
    estimated = int(w * h * _ANTHROPIC_TOKENS_PER_PIXEL)
    return min(estimated, _ANTHROPIC_MAX_TOKENS_PER_IMAGE)


# --------------------------------------------------------------------------- #
# Image dimension extraction (no PIL dependency)                              #
# --------------------------------------------------------------------------- #


def _read_image_dimensions(part: ImagePart) -> tuple[int, int] | None:
    """Read (width, height) from PNG / JPEG / GIF / WEBP headers.

    No full decode — we read just enough header bytes to extract
    dimensions. Saves the ~250 MB Pillow dependency for one feature
    that needs width x height.

    Returns ``None`` when the image isn't a recognised format OR the
    header read fails. The caller then falls back to ``_FALLBACK_TOKENS``.
    """
    m = _DATA_URI_RE.match(part.url)
    if m is None:
        return None
    try:
        raw = base64.b64decode(m.group("data"), validate=False)
    except (ValueError, TypeError):
        return None
    if len(raw) < 24:
        return None
    # PNG: bytes 16-23 are width + height as big-endian uint32.
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            width, height = struct.unpack(">II", raw[16:24])
            return width, height
        except struct.error:
            return None
    # JPEG: walk markers until SOF0/SOF2 frame header.
    if raw[:2] == b"\xff\xd8":
        return _read_jpeg_dimensions(raw)
    # GIF87a / GIF89a: bytes 6-9 are width + height as little-endian uint16.
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        try:
            width, height = struct.unpack("<HH", raw[6:10])
            return width, height
        except struct.error:
            return None
    # WEBP: RIFF...WEBP at bytes 0-3 + 8-11. VP8X/VP8/VP8L chunk
    # carries dimensions. We support the common VP8X case (animated
    # / extended) and VP8 (lossy).
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return _read_webp_dimensions(raw)
    return None


def _read_jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Walk JPEG markers to find the SOF (Start of Frame) section.

    Skips standalone markers, reads variable-length segments to find
    SOF0 (0xFFC0) or SOF2 (0xFFC2). The frame header carries
    precision (1 byte), height (2 bytes), width (2 bytes).
    """
    i = 2
    n = len(raw)
    while i < n - 9:
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        # SOI/EOI/RSTn are standalone (no length).
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        # SOF0 / SOF2 — frame header.
        if marker in (0xC0, 0xC2):
            try:
                height, width = struct.unpack(">HH", raw[i + 5 : i + 9])
                return width, height
            except struct.error:
                return None
        # Variable-length segment — read length, skip.
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None


def _read_webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Read width / height from a WEBP container.

    Supports VP8X (extended) and VP8 (lossy). VP8L (lossless) and
    others fall back to None.
    """
    if len(raw) < 30:
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X":
        # VP8X: width-1 (3 bytes, LE) at offset 24; height-1 at 27.
        w_minus_1 = raw[24] | (raw[25] << 8) | (raw[26] << 16)
        h_minus_1 = raw[27] | (raw[28] << 8) | (raw[29] << 16)
        return w_minus_1 + 1, h_minus_1 + 1
    if chunk == b"VP8 ":
        # VP8: keyframe header — bytes 26-29 hold scaled dimensions.
        # The 14 low bits of each 16-bit word are the actual width/height.
        try:
            width = (raw[26] | (raw[27] << 8)) & 0x3FFF
            height = (raw[28] | (raw[29] << 8)) & 0x3FFF
            return width, height
        except IndexError:
            return None
    return None


__all__ = [
    "ImagePart",
    "MessageImageInventory",
    "estimate_image_tokens",
    "inventory_images",
    "translate_messages_for_anthropic",
]
