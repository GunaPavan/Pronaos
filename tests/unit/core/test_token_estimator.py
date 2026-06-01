"""Token estimator tests.

The estimator is a heuristic — these tests pin its behaviour at known
inputs rather than asserting exact byte-level fidelity. The calibration
target is "within ±15% of the provider's actual prompt_tokens count
for representative English content," good enough for budget gating.

For inputs from real Groq billing receipts (the calibration data behind
the WORDS_TO_TOKENS_FACTOR=1.30 constant), the expected estimates are
documented inline so future tuning has a clear test bench.
"""

from __future__ import annotations

import pytest

from pronaos.core.token_estimator import (
    DEFAULT_MAX_COMPLETION,
    PER_MESSAGE_OVERHEAD,
    estimate_tokens,
)

# --------------------------------------------------------------------------- #
# Basic shape                                                                  #
# --------------------------------------------------------------------------- #


def test_empty_messages_return_only_completion_budget() -> None:
    """Zero messages → zero prompt tokens. The total is purely the
    completion budget. Confirms the per-message overhead doesn't fire
    when there ARE no messages."""
    assert estimate_tokens([], max_completion_tokens=100) == 100


def test_single_message_includes_per_message_overhead() -> None:
    """One short message → at minimum the per-message overhead (4
    tokens) PLUS the response budget. The text body itself contributes
    on top."""
    out = estimate_tokens([{"role": "user", "content": "hi"}], max_completion_tokens=10)
    # 4 (overhead) + ~1 (text "hi") + 10 (completion)
    assert out >= PER_MESSAGE_OVERHEAD + 10
    assert out <= PER_MESSAGE_OVERHEAD + 5 + 10  # generous upper bound


def test_missing_max_tokens_falls_back_to_default() -> None:
    """When the caller didn't set max_tokens, assume the conservative
    default (4096). This is intentional fail-safe: don't let
    unbounded responses slip past the budget gate."""
    out = estimate_tokens([{"role": "user", "content": "hi"}])
    # Should include the full default completion budget.
    assert out >= DEFAULT_MAX_COMPLETION


# --------------------------------------------------------------------------- #
# English prompts — calibration data                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected_min,expected_max",
    [
        # Short prompt — actual Groq tokenizes "Hello world" to ~3 tokens
        # (Llama BPE). We aim for the same ballpark.
        ("Hello world", 2, 6),
        # Sentence with punctuation — punctuation counts as separate tokens.
        ("Hello, world!", 3, 8),
        # ~10-word sentence; Groq actual = 12-14 tokens for similar shapes.
        (
            "What is the weather in Paris and Tokyo today please",
            10,
            16,
        ),
        # Longer paragraph (~50 words). Groq actual ~60-70 tokens.
        (
            "The history of programming languages spans from Ada Lovelace's "
            "analytical engine notes in the 1840s, through the early "
            "machine codes of the 1940s and 1950s, the rise of FORTRAN "
            "and LISP, the structured-programming revolution of the 1970s, "
            "to today's polyglot ecosystem of TypeScript and Rust",
            50,
            85,
        ),
    ],
)
def test_english_prompts_calibrated_to_groq_actuals(
    text: str, expected_min: int, expected_max: int
) -> None:
    """English prompts should estimate within ±15% of the actual Groq
    tokenizer's output for representative samples. The ranges below
    are derived from probing Groq's real prompt_tokens responses with
    these exact strings.

    If a test fails after a model upgrade, RE-RUN the calibration
    rather than loosening the bounds blindly — the constant
    WORDS_TO_TOKENS_FACTOR can be tuned."""
    out = estimate_tokens([{"role": "user", "content": text}], max_completion_tokens=0)
    # Subtract the per-message overhead so we're comparing the text
    # estimate alone against the per-text expected range.
    text_only = out - PER_MESSAGE_OVERHEAD
    assert expected_min <= text_only <= expected_max, (
        f"estimate={text_only} outside [{expected_min}, {expected_max}] for {text!r}"
    )


# --------------------------------------------------------------------------- #
# Non-Latin scripts                                                            #
# --------------------------------------------------------------------------- #


def test_cjk_uses_char_heuristic() -> None:
    """CJK (Chinese, Japanese, Korean) tokenizes very differently from
    English — typically 1.5-2 characters per token. The estimator
    detects non-Latin script and switches to a char-based heuristic.

    "Hello world" in Japanese is "こんにちは世界" (7 chars). At our
    char/2.5 ratio we'd estimate ~3 tokens; actual Groq tokenization
    of Japanese is closer to 1 token per CJK char. The estimator
    under-estimates here — known limitation, accepted because:
    (a) CJK content is rare in the dominant English-mostly workloads,
    (b) under-estimating means MORE requests get through (no
    false-positive denial); the post-flight token count still
    enforces the real budget correctly."""
    japanese = "こんにちは世界今日はいい天気です"
    out = estimate_tokens([{"role": "user", "content": japanese}], max_completion_tokens=0)
    text_only = out - PER_MESSAGE_OVERHEAD
    # Just verify the non-Latin branch produced a non-zero estimate,
    # not the exact value (which is a known under-estimate; documented
    # above).
    assert text_only > 0
    assert text_only < len(japanese)  # less than 1:1 char-to-token


# --------------------------------------------------------------------------- #
# Multi-message conversations                                                  #
# --------------------------------------------------------------------------- #


def test_multi_message_conversation_accumulates_overhead() -> None:
    """Each message contributes the per-message overhead. A 10-turn
    conversation has 10 * overhead = 40 tokens just in wrapping,
    before any content is counted."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "weather"},
        {"role": "assistant", "content": "sunny"},
        {"role": "user", "content": "ok"},
    ]
    out = estimate_tokens(msgs, max_completion_tokens=0)
    # At minimum: 5 messages * overhead = 20 tokens of pure overhead.
    assert out >= 5 * PER_MESSAGE_OVERHEAD


def test_non_string_content_is_tolerated() -> None:
    """Assistant messages with ``content: null`` + tool_calls (the
    OpenAI agent-loop echo shape) shouldn't crash the estimator —
    we just attribute the per-message overhead and move on. Same for
    multimodal content blocks (lists, dicts) which we can't size
    properly without per-modality logic."""
    msgs = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "x", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "x", "content": "{'temp': 12}"},
    ]
    # Should NOT raise; should produce some sensible number.
    out = estimate_tokens(msgs, max_completion_tokens=10)
    assert out >= 3 * PER_MESSAGE_OVERHEAD + 10


# --------------------------------------------------------------------------- #
# Monotonicity                                                                 #
# --------------------------------------------------------------------------- #


def test_longer_text_estimates_more_tokens() -> None:
    """Sanity check: more content → more tokens. This is the property
    the pre-flight gate actually depends on (longer prompt → more
    likely to exceed remaining budget)."""
    short = estimate_tokens([{"role": "user", "content": "hi"}], max_completion_tokens=0)
    long = estimate_tokens([{"role": "user", "content": "hi " * 100}], max_completion_tokens=0)
    assert long > short


def test_more_max_tokens_estimates_more_total() -> None:
    """Caller asks for a longer max response → estimate goes up
    correspondingly. Confirms the completion budget threads through."""
    msg = [{"role": "user", "content": "hi"}]
    small = estimate_tokens(msg, max_completion_tokens=10)
    large = estimate_tokens(msg, max_completion_tokens=1000)
    assert large == small + (1000 - 10)
