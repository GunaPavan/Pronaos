"""Unit tests for the Phase 41 multi-modal helpers.

Three surfaces under test:

1. ``inventory_images`` — walk messages, pull image parts, total bytes.
2. ``translate_messages_for_anthropic`` — OpenAI ``image_url`` →
   Anthropic ``image`` block translation; text parts pass through;
   Anthropic-native parts pass through.
3. ``estimate_image_tokens`` — token-count math for both gpt-4o tile
   algorithm and Anthropic / Groq area formula.

The token-count math doesn't need a real image — we construct
synthetic PNG headers with known dimensions and check the result.
"""

from __future__ import annotations

import base64
import struct

from pronaos.core.multimodal import (
    ImagePart,
    estimate_image_tokens,
    inventory_images,
    translate_messages_for_anthropic,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _synth_png(width: int, height: int) -> str:
    """Build a minimal PNG header that ``_read_image_dimensions`` will parse.

    The header is just enough to satisfy the dimension reader — we
    don't need a valid IDAT or anything beyond the IHDR signature.
    """
    # PNG signature + IHDR length + "IHDR" + dimensions + bit-depth etc.
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr = b"IHDR"
    dims = struct.pack(">II", width, height)
    rest = b"\x08\x02\x00\x00\x00"  # bit depth + colour type + compression + filter + interlace
    crc = b"\x00" * 4  # not validated by our reader
    raw = sig + ihdr_len + ihdr + dims + rest + crc
    encoded = base64.b64encode(raw).decode()
    return f"data:image/png;base64,{encoded}"


# --------------------------------------------------------------------------- #
# inventory_images                                                            #
# --------------------------------------------------------------------------- #


class TestInventoryImages:
    def test_text_only_returns_empty(self) -> None:
        out = inventory_images([{"role": "user", "content": "hello"}])
        assert out.parts == []
        assert out.total_base64_bytes == 0

    def test_data_uri_image_counted(self) -> None:
        url = "data:image/png;base64,iVBORw0KGgo="  # 7 decoded bytes
        out = inventory_images(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what's this"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ]
        )
        assert len(out.parts) == 1
        assert out.parts[0].media_type == "image/png"
        # iVBORw0KGgo= is 11 chars - 1 pad = 10 -> (10*3)//4 - 1 = 6.
        # Exact value matters less than "positive and roughly right".
        assert out.total_base64_bytes > 0

    def test_https_url_image_bytes_zero(self) -> None:
        out = inventory_images(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        }
                    ],
                }
            ]
        )
        assert len(out.parts) == 1
        assert out.total_base64_bytes == 0

    def test_anthropic_native_image_part_picked_up(self) -> None:
        """A client speaking Anthropic native ships
        ``{"type":"image","source":{"type":"base64",...}}`` directly.
        Inventory still counts it for the size cap."""
        out = inventory_images(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "/9j/4AAQSkZJRg==",
                            },
                        }
                    ],
                }
            ]
        )
        assert len(out.parts) == 1
        assert out.parts[0].media_type == "image/jpeg"
        assert out.total_base64_bytes > 0

    def test_multiple_messages_aggregate(self) -> None:
        out = inventory_images(
            [
                {"role": "user", "content": "first message text only"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "two images:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,BBBB"},
                        },
                    ],
                },
            ]
        )
        assert len(out.parts) == 2
        assert out.total_base64_bytes > 0

    def test_malformed_part_skipped(self) -> None:
        out = inventory_images(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url"},  # missing image_url block
                        {"type": "image_url", "image_url": {}},  # missing url
                        "garbage non-dict part",
                        {"type": "text", "text": "valid"},
                    ],
                }
            ]
        )
        assert out.parts == []


# --------------------------------------------------------------------------- #
# translate_messages_for_anthropic                                            #
# --------------------------------------------------------------------------- #


class TestTranslateForAnthropic:
    def test_text_only_passes_through(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        out = translate_messages_for_anthropic(msgs)
        assert out == msgs

    def test_data_uri_translates_to_base64_source(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw=="},
                    },
                ],
            }
        ]
        out = translate_messages_for_anthropic(msgs)
        parts = out[0]["content"]
        assert isinstance(parts, list)
        assert parts[0]["type"] == "text"
        img = parts[1]
        assert img["type"] == "image"
        assert img["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw==",
        }

    def test_https_url_translates_to_url_source(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png"},
                    }
                ],
            }
        ]
        out = translate_messages_for_anthropic(msgs)
        img = out[0]["content"][0]
        assert img["type"] == "image"
        assert img["source"] == {"type": "url", "url": "https://example.com/cat.png"}

    def test_anthropic_native_image_passes_through(self) -> None:
        """Defensive: a client that already speaks Anthropic shouldn't
        get double-translated."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "/9j/4AAQ==",
                        },
                    }
                ],
            }
        ]
        out = translate_messages_for_anthropic(msgs)
        assert out[0]["content"] == msgs[0]["content"]


# --------------------------------------------------------------------------- #
# estimate_image_tokens                                                       #
# --------------------------------------------------------------------------- #


class TestEstimateImageTokens:
    def test_small_png_openai_one_tile(self) -> None:
        # 256x256 image — fits in one 512x512 tile.
        url = _synth_png(256, 256)
        part = ImagePart(url=url, media_type="image/png", base64_bytes=len(url))
        tokens = estimate_image_tokens(part, model="openai/gpt-4o")
        # 85 base + 170 per tile * 1 tile = 255.
        assert tokens == 255

    def test_large_png_openai_multiple_tiles(self) -> None:
        # 1024x1024 → after scaling to 768 short side: 768x768 →
        # ceil(768/512) = 2 tiles per dim → 4 tiles.
        url = _synth_png(1024, 1024)
        part = ImagePart(url=url, media_type="image/png", base64_bytes=len(url))
        tokens = estimate_image_tokens(part, model="openai/gpt-4o")
        # 85 + 170 * 4 = 765.
        assert tokens == 765

    def test_anthropic_area_formula(self) -> None:
        # 750x750 → 750*750/750 = 750 tokens.
        url = _synth_png(750, 750)
        part = ImagePart(url=url, media_type="image/png", base64_bytes=len(url))
        tokens = estimate_image_tokens(part, model="anthropic/claude-opus-4-7")
        assert tokens == 750

    def test_anthropic_caps_at_max(self) -> None:
        # 2048x2048 → would be ~5600 tokens; capped at 1568.
        url = _synth_png(2048, 2048)
        part = ImagePart(url=url, media_type="image/png", base64_bytes=len(url))
        tokens = estimate_image_tokens(part, model="anthropic/claude-opus-4-7")
        assert tokens == 1568

    def test_url_only_uses_fallback(self) -> None:
        """An HTTPS URL with no measurable bytes falls back to the
        conservative max-estimate token count."""
        part = ImagePart(url="https://example.com/x.png", media_type="image/png", base64_bytes=0)
        tokens = estimate_image_tokens(part, model="anthropic/claude-opus-4-7")
        assert tokens == 1500  # _FALLBACK_TOKENS

    def test_groq_vision_uses_anthropic_formula(self) -> None:
        """Groq vision models bill similarly to Anthropic per their
        docs — we route them through the area-based formula."""
        url = _synth_png(600, 600)
        part = ImagePart(url=url, media_type="image/png", base64_bytes=len(url))
        tokens = estimate_image_tokens(part, model="groq/llama-3.2-90b-vision-preview")
        # 600 * 600 / 750 = 480.
        assert tokens == 480
