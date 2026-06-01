"""Model allowlist matching + validator tests.

Two layers covered here:

1. ``is_model_allowed`` — the runtime predicate the chat handler asks.
   Pure function, no I/O; tests are fast and exhaustive on edge cases.

2. ``validate_allowed_models`` — the input validator the CLI / admin
   endpoint runs before persisting. Rejects malformed shapes with
   human-readable reasons.

Handler-level integration (403 response on a real chat request) lives
in test_chat_endpoint.py — these tests stay focused on the primitive.
"""

from __future__ import annotations

import pytest

from pronaos.core.model_access import is_model_allowed, validate_allowed_models

# --------------------------------------------------------------------------- #
# is_model_allowed                                                             #
# --------------------------------------------------------------------------- #


def test_null_allowlist_means_unrestricted() -> None:
    """A team with no policy (NULL) should be able to invoke any model.
    Critical for backwards compatibility — every team that existed
    before this feature shipped must keep working."""
    assert is_model_allowed("groq/llama-3.1-8b-instant", None) is True
    assert is_model_allowed("anthropic/claude-opus-4-7", None) is True
    assert is_model_allowed("openai/gpt-4o", None) is True


def test_empty_list_means_deny_all() -> None:
    """An empty list ``[]`` is an intentional "deny everything" policy —
    distinguishable from NULL. Used for paused teams whose keys
    shouldn't be revoked but whose access should be temporarily off."""
    assert is_model_allowed("groq/llama-3.1-8b-instant", []) is False
    assert is_model_allowed("anthropic/claude-opus-4-7", []) is False


def test_exact_match_allows_only_that_model() -> None:
    """The most-restrictive form: pin to a single exact model id."""
    policy = ["groq/llama-3.1-8b-instant"]
    assert is_model_allowed("groq/llama-3.1-8b-instant", policy) is True
    assert is_model_allowed("groq/llama-3.3-70b-versatile", policy) is False
    assert is_model_allowed("anthropic/claude-opus-4-7", policy) is False


def test_provider_wildcard_matches_all_models_under_prefix() -> None:
    """``"groq/*"`` is the typical operator entry — "everything from this
    provider." fnmatch treats ``/`` as a regular character, so the
    wildcard greedily matches the entire model id after the prefix."""
    policy = ["groq/*"]
    assert is_model_allowed("groq/llama-3.1-8b-instant", policy) is True
    assert is_model_allowed("groq/llama-3.3-70b-versatile", policy) is True
    # Nested-namespace models (Groq sometimes does this with Meta-published
    # ids) should also match — fnmatch's ``*`` includes slashes.
    assert is_model_allowed("groq/meta-llama/llama-4-scout-17b-16e-instruct", policy) is True
    assert is_model_allowed("anthropic/claude-opus-4-7", policy) is False


def test_multiple_patterns_match_any_of() -> None:
    """An allowlist is OR — match any pattern and you're in. This is
    the standard composition: ``cheap providers OR a specific
    expensive model``."""
    policy = ["groq/*", "anthropic/claude-opus-4-7"]
    assert is_model_allowed("groq/llama-3.1-8b-instant", policy) is True
    assert is_model_allowed("anthropic/claude-opus-4-7", policy) is True
    # Anthropic Sonnet isn't on the list:
    assert is_model_allowed("anthropic/claude-sonnet-4-6", policy) is False
    # Neither is OpenAI:
    assert is_model_allowed("openai/gpt-4o", policy) is False


def test_partial_provider_prefix_pattern() -> None:
    """``"anthropic/claude-opus-*"`` lets operators restrict to a model
    family within one provider — common in cost-conscious deployments
    where Opus is the only approved tier."""
    policy = ["anthropic/claude-opus-*"]
    assert is_model_allowed("anthropic/claude-opus-4-7", policy) is True
    assert is_model_allowed("anthropic/claude-opus-4-6", policy) is True
    assert is_model_allowed("anthropic/claude-sonnet-4-6", policy) is False
    assert is_model_allowed("anthropic/claude-haiku-4-5", policy) is False


def test_universal_wildcard_matches_everything() -> None:
    """``"*"`` is the explicit-allow-all form. Semantically equivalent
    to NULL but operators sometimes prefer the explicit token."""
    policy = ["*"]
    assert is_model_allowed("groq/llama-3.1-8b-instant", policy) is True
    assert is_model_allowed("anthropic/claude-opus-4-7", policy) is True


# --------------------------------------------------------------------------- #
# validate_allowed_models                                                      #
# --------------------------------------------------------------------------- #


def test_validator_accepts_valid_list() -> None:
    """Standard happy path — a list of non-empty strings round-trips
    unchanged. The validator returns the input so callers can chain
    directly into the DB write."""
    out = validate_allowed_models(["groq/*", "anthropic/claude-opus-*"])
    assert out == ["groq/*", "anthropic/claude-opus-*"]


def test_validator_accepts_empty_list() -> None:
    """``[]`` is a valid policy (deny-all). The validator must NOT
    reject it — the deny-all semantic is intentional."""
    assert validate_allowed_models([]) == []


def test_validator_rejects_non_list() -> None:
    """A dict or string is the most common typo (operator wrote
    ``"groq/*"`` instead of ``["groq/*"]``). Error message must point
    at the type so the operator self-corrects."""
    with pytest.raises(ValueError, match="must be a JSON list"):
        validate_allowed_models("groq/*")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a JSON list"):
        validate_allowed_models({"patterns": ["groq/*"]})  # type: ignore[arg-type]


def test_validator_rejects_non_string_entry() -> None:
    """One bad entry in an otherwise valid list — surface its index so
    the operator can fix without scanning manually."""
    with pytest.raises(ValueError, match=r"allowed_models\[1\] must be a string"):
        validate_allowed_models(["groq/*", 42, "anthropic/*"])  # type: ignore[list-item]


def test_validator_rejects_empty_string_entry() -> None:
    """Empty strings are silently-deny by fnmatch (no input matches
    an empty pattern), which is the worst possible failure mode for
    an operator — looks like ``set the policy`` but actually denies
    everything. Reject up front."""
    with pytest.raises(ValueError, match=r"allowed_models\[0\] is empty"):
        validate_allowed_models([""])
    with pytest.raises(ValueError, match=r"allowed_models\[1\] is empty"):
        validate_allowed_models(["groq/*", "   "])
