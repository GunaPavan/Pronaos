"""Heuristic token estimator for pre-flight quota gating.

Why a heuristic and not tiktoken
--------------------------------
The "real" answer for OpenAI-family models is ``tiktoken.encoding_for_model(...)
.encode(text)``. Two problems with bringing it in:

1. **Weight.** tiktoken pulls in Rust extensions + ~30MB of BPE tables for
   every model family. For one job — telling whether a request is going to
   blow a budget — that's overkill.
2. **Wrong target.** Llama/Mistral/DeepSeek/etc. don't use OpenAI's BPE.
   Estimates from tiktoken would be off by 5-15% on those models anyway.

The pre-flight gate is a *guardrail*, not a billing oracle. We need to be
in the right ballpark: when an operator sets a 50-token budget and a
client sends a 5000-token prompt, the gate should reject up-front. When
the client sends a 30-token prompt against the 50-token budget, the
estimate should be small enough not to false-positive.

A character/word heuristic gets us comfortably inside ±15% across the
common model families, which is enough for budget gating. The actual
post-call token count from the provider is what we bill on; this is
just to skip the upstream round-trip when it's going to deny anyway.

The heuristic
-------------
For each message:
- Latin-script content: ``words * 1.30 + count(',', '.', '!', '?', '"')``
- Anything else (CJK, Arabic, mixed): ``len(content) / 2.5``
- Plus a small per-message overhead (4 tokens) for role markers and
  message separators — matches OpenAI's documented overhead, close
  enough for Llama too.

For the response:
- Use ``max_tokens`` if the caller set it; otherwise fall back to a
  conservative default (4096) so unbounded requests don't slip past
  the gate by claiming zero-cost outputs.

The constant 1.30 is empirically derived from comparing this estimator
against actual Groq billing receipts for English prompts ranging 10 to
2000 tokens — see ``tests/unit/core/test_token_estimator.py`` for the
calibration data.
"""

from __future__ import annotations

from typing import Any, Final

# Per-message overhead matching OpenAI's documented 4-token-per-message
# wrapping (role tags, separators). Llama's chat template is similar.
PER_MESSAGE_OVERHEAD: Final = 4

# Multiplier from word count → estimated tokens for Latin-script text.
# Calibrated against Groq's actual prompt_tokens for English samples.
# Tightening or loosening this is the most-likely future change.
WORDS_TO_TOKENS_FACTOR: Final = 1.30

# Fallback character-per-token ratio for non-Latin scripts. Conservative
# (slightly over-estimates) so the gate is fail-safe under uncertainty.
NON_LATIN_CHARS_PER_TOKEN: Final = 2.5

# Default response-token allowance when the caller didn't set max_tokens.
# Conservative — assume the model might use its full default response
# window. Better to over-estimate (cause unnecessary preflight denials)
# than under-estimate (let a runaway request through).
DEFAULT_MAX_COMPLETION: Final = 4096


def estimate_tokens(
    messages: list[dict[str, Any]],
    *,
    max_completion_tokens: int | None = None,
) -> int:
    """Return an estimated total token count (prompt + max completion).

    The total is the number to compare against the remaining budget.
    A more granular split (prompt-only vs completion-only) is overkill
    here — quota is enforced on the SUM and that's what we estimate.

    Empty messages / non-string content are tolerated (treated as zero
    tokens for that message but the per-message overhead still applies)
    so a malformed request doesn't crash the gate; the provider will
    reject the request anyway.
    """
    prompt_tokens = 0
    for msg in messages:
        prompt_tokens += PER_MESSAGE_OVERHEAD
        content = msg.get("content")
        if not isinstance(content, str):
            # Multimodal content blocks, None for tool-call assistant
            # messages, etc. — we can't size those properly with a
            # heuristic; rely on overhead + per-message bias.
            continue
        prompt_tokens += _estimate_text(content)

    completion_budget = (
        max_completion_tokens
        if max_completion_tokens is not None
        else DEFAULT_MAX_COMPLETION
    )
    return prompt_tokens + completion_budget


def _estimate_text(text: str) -> int:
    """Estimate tokens for one text blob.

    Routes Latin-script vs non-Latin based on a quick character-class
    sample. We use a coarse split (does the string contain any Latin
    letters at all?) rather than a per-character classification — for
    budget gating the boundary case doesn't matter.
    """
    if not text:
        return 0
    if _is_mostly_non_latin(text):
        return int(len(text) / NON_LATIN_CHARS_PER_TOKEN) + 1
    # Words + punctuation count → tokens.
    words = text.split()
    word_tokens = int(len(words) * WORDS_TO_TOKENS_FACTOR)
    # Common punctuation is usually its own token in BPE encodings.
    punct_tokens = sum(text.count(c) for c in ",.!?\";:")
    return max(1, word_tokens + punct_tokens)


def _is_mostly_non_latin(text: str) -> bool:
    """Cheap heuristic for the script-class branch.

    Sample up to the first 200 characters and count Latin letters.
    If less than 30% are Latin we switch to the char/byte heuristic.
    The sample cap keeps long-text classification O(1)."""
    sample = text[:200]
    total = sum(1 for c in sample if c.isalpha())
    if total == 0:
        return False
    latin = sum(
        1
        for c in sample
        if c.isalpha() and ord(c) < 0x250  # Latin Extended-B endpoint
    )
    return (latin / total) < 0.3
