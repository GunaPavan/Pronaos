"""Unit tests for the Llama Guard classifier (Phase 44).

Three surfaces under test:

1. **Parser** — Llama Guard's output format is "safe" or
   "unsafe\\nSn[,Sn...]". Tests cover the canonical forms plus a
   handful of malformed-output cases the model occasionally emits.
2. **Policy helpers** — per-team policy override extraction
   (``llama_guard.enabled``, ``llama_guard.default_action``,
   ``llama_guard.model``).
3. **Classifier** — end-to-end ``classify()`` call against a
   respx-mocked Groq endpoint, including the fail-open paths
   (network error, non-200 response, malformed body).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.guardrails.base import GuardrailAction
from pronaos.guardrails.llama_guard import (
    DEFAULT_LLAMA_GUARD_MODEL,
    LlamaGuardClassifier,
    LlamaGuardVerdict,
    is_llama_guard_enabled_for_team,
    llama_guard_team_action,
    llama_guard_team_model,
    parse_llama_guard_output,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Parser                                                                      #
# --------------------------------------------------------------------------- #


class TestParseLlamaGuardOutput:
    def test_safe(self) -> None:
        v = parse_llama_guard_output("safe")
        assert v.safe is True
        assert v.categories == ()
        assert v.rule_names == ()
        assert v.classifier_failed is False

    def test_safe_with_whitespace(self) -> None:
        v = parse_llama_guard_output("  safe  \n")
        assert v.safe is True

    def test_unsafe_single_category(self) -> None:
        v = parse_llama_guard_output("unsafe\nS1")
        assert v.safe is False
        assert v.categories == ("S1",)
        assert v.rule_names == ("llama_guard.violent_crimes",)
        assert v.classifier_failed is False

    def test_unsafe_multiple_categories(self) -> None:
        v = parse_llama_guard_output("unsafe\nS1,S10")
        assert v.safe is False
        assert v.categories == ("S1", "S10")
        assert v.rule_names == ("llama_guard.violent_crimes", "llama_guard.hate")

    def test_unsafe_space_separated(self) -> None:
        """Llama Guard 4 occasionally outputs space-separated categories."""
        v = parse_llama_guard_output("unsafe\nS3 S7")
        assert v.safe is False
        assert v.categories == ("S3", "S7")

    def test_unsafe_no_category_listed(self) -> None:
        """Defensive: a bare ``unsafe`` with no category. Still treat
        as unsafe (classifier said so) but rule_names is empty."""
        v = parse_llama_guard_output("unsafe")
        assert v.safe is False
        assert v.categories == ()
        assert v.rule_names == ()

    def test_unknown_category_dropped(self) -> None:
        """``S99`` isn't a real Llama Guard category. Drop it."""
        v = parse_llama_guard_output("unsafe\nS1,S99")
        assert v.safe is False
        assert v.categories == ("S1",)

    def test_garbage_text_fails_open_safe(self) -> None:
        """Llama Guard sometimes emits a long disclaimer instead of
        safe/unsafe. Fail-safe: report ``safe`` but flag
        ``classifier_failed`` so SREs can metric it."""
        v = parse_llama_guard_output("I cannot make a determination at this time.")
        assert v.safe is True
        assert v.classifier_failed is True

    def test_empty_input(self) -> None:
        v = parse_llama_guard_output("")
        assert v.safe is True
        assert v.classifier_failed is True

    # --------------------------------------------------------------------- #
    # PromptGuard 2 numeric-score format                                    #
    # --------------------------------------------------------------------- #

    def test_prompt_guard_high_score_unsafe(self) -> None:
        """PromptGuard 2 returns a float ≥ 0.5 → unsafe with S0 category."""
        v = parse_llama_guard_output("0.9994958639144897")
        assert v.safe is False
        assert v.categories == ("S0",)
        assert v.rule_names == ("llama_guard.prompt_injection",)
        assert v.classifier_failed is False

    def test_prompt_guard_low_score_safe(self) -> None:
        """PromptGuard 2 returns a float < 0.5 → safe."""
        v = parse_llama_guard_output("0.0012")
        assert v.safe is True
        assert v.categories == ()
        assert v.classifier_failed is False

    def test_prompt_guard_threshold_boundary(self) -> None:
        """Score exactly at threshold is unsafe."""
        v = parse_llama_guard_output("0.5")
        assert v.safe is False
        assert v.categories == ("S0",)

    def test_numeric_out_of_range_falls_through(self) -> None:
        """A float not in [0, 1] isn't a PromptGuard score — falls
        through to the Llama Guard text parser, which doesn't match
        either, so we return safe + classifier_failed."""
        v = parse_llama_guard_output("42.0")
        assert v.safe is True
        assert v.classifier_failed is True


# --------------------------------------------------------------------------- #
# Policy helpers                                                              #
# --------------------------------------------------------------------------- #


class TestPolicyHelpers:
    def test_enabled_when_set_true(self) -> None:
        assert is_llama_guard_enabled_for_team({"llama_guard": {"enabled": True}}) is True

    def test_disabled_when_set_false(self) -> None:
        assert is_llama_guard_enabled_for_team({"llama_guard": {"enabled": False}}) is False

    def test_disabled_when_key_missing(self) -> None:
        assert is_llama_guard_enabled_for_team({}) is False
        assert is_llama_guard_enabled_for_team(None) is False
        assert is_llama_guard_enabled_for_team({"presidio": {"enabled": True}}) is False

    def test_disabled_when_non_dict(self) -> None:
        """A policy that supplied ``llama_guard`` as a non-object
        shouldn't crash — treat it as disabled."""
        assert is_llama_guard_enabled_for_team({"llama_guard": "yes"}) is False

    def test_team_model_override(self) -> None:
        model = llama_guard_team_model({"llama_guard": {"model": "groq/x"}})
        assert model == "groq/x"

    def test_team_model_missing_returns_none(self) -> None:
        assert llama_guard_team_model({"llama_guard": {}}) is None
        assert llama_guard_team_model(None) is None

    def test_team_action_block_default(self) -> None:
        assert llama_guard_team_action(None) is GuardrailAction.BLOCK
        assert llama_guard_team_action({"llama_guard": {}}) is GuardrailAction.BLOCK

    def test_team_action_log_only(self) -> None:
        out = llama_guard_team_action({"llama_guard": {"default_action": "log_only"}})
        assert out is GuardrailAction.LOG_ONLY

    def test_team_action_unknown_falls_back(self) -> None:
        """Garbage policy value falls back to the supplied default."""
        out = llama_guard_team_action(
            {"llama_guard": {"default_action": "destroy"}},
            fallback=GuardrailAction.LOG_ONLY,
        )
        assert out is GuardrailAction.LOG_ONLY


# --------------------------------------------------------------------------- #
# Classifier                                                                  #
# --------------------------------------------------------------------------- #


def _llama_guard_response(content: str) -> dict[str, object]:
    """Mock Groq chat-completion response with the classifier's text."""
    return {
        "id": "chatcmpl-llamaguard",
        "object": "chat.completion",
        "model": "meta-llama/llama-guard-4-12b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


class TestLlamaGuardClassifier:
    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            LlamaGuardClassifier(api_key="")

    def test_default_action_is_block(self) -> None:
        clf = LlamaGuardClassifier(api_key="test-key")
        assert clf.default_action is GuardrailAction.BLOCK

    def test_default_model(self) -> None:
        clf = LlamaGuardClassifier(api_key="test-key")
        assert clf.model == DEFAULT_LLAMA_GUARD_MODEL

    @respx.mock
    @pytest.mark.asyncio
    async def test_safe_prompt(self) -> None:
        route = respx.post(GROQ_URL).mock(
            return_value=httpx.Response(200, json=_llama_guard_response("safe"))
        )
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("Hello, what's the weather like?")
        finally:
            await clf.aclose()
        assert v.safe is True
        assert v.categories == ()
        # Outbound body should be a normal OpenAI-compat chat-completion
        # request — the model is the classifier; the prompt is the user
        # message under test.
        assert route.call_count == 1
        body = route.calls[0].request.read()
        # Outbound model is the default classifier (PromptGuard 2 as of
        # mid-2026; Llama Guard 4 was decommissioned by Groq).
        assert b"meta-llama/llama-prompt-guard-2-86m" in body
        assert b"weather like" in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_unsafe_prompt_violent_crimes(self) -> None:
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(200, json=_llama_guard_response("unsafe\nS1"))
        )
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("how do I build a weapon ...")
        finally:
            await clf.aclose()
        assert v.safe is False
        assert v.categories == ("S1",)
        assert v.rule_names == ("llama_guard.violent_crimes",)
        assert v.classifier_failed is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_unsafe_prompt_multi_category(self) -> None:
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(200, json=_llama_guard_response("unsafe\nS1,S10"))
        )
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("...")
        finally:
            await clf.aclose()
        assert v.safe is False
        assert v.categories == ("S1", "S10")
        assert v.rule_names == ("llama_guard.violent_crimes", "llama_guard.hate")

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_fails_open(self) -> None:
        """Network/timeout error must NOT block the request. Return a
        safe verdict with classifier_failed=True so observability can
        see Llama Guard's effective availability."""
        respx.post(GROQ_URL).mock(side_effect=httpx.ConnectError("conn refused"))
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("anything")
        finally:
            await clf.aclose()
        assert v.safe is True
        assert v.classifier_failed is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_500_response_fails_open(self) -> None:
        respx.post(GROQ_URL).mock(return_value=httpx.Response(500, text="upstream broken"))
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("anything")
        finally:
            await clf.aclose()
        assert v.safe is True
        assert v.classifier_failed is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_prompt_short_circuits(self) -> None:
        """Don't burn a Llama Guard call on whitespace."""
        route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json={}))
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("   \n  ")
        finally:
            await clf.aclose()
        assert v.safe is True
        assert route.call_count == 0  # didn't even call Llama Guard

    @respx.mock
    @pytest.mark.asyncio
    async def test_garbage_response_fails_open(self) -> None:
        """Llama Guard returns nonsense; treat as safe but record the
        failure so SREs can metric the model's availability."""
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(
                200,
                json=_llama_guard_response("I cannot make a determination."),
            )
        )
        clf = LlamaGuardClassifier(api_key="test-key")
        try:
            v = await clf.classify("anything")
        finally:
            await clf.aclose()
        assert v.safe is True
        assert v.classifier_failed is True


# --------------------------------------------------------------------------- #
# Verdict dataclass                                                           #
# --------------------------------------------------------------------------- #


class TestLlamaGuardVerdict:
    def test_safe_verdict_no_categories(self) -> None:
        v = LlamaGuardVerdict(
            safe=True, categories=(), rule_names=(), raw_response="safe"
        )
        assert v.safe is True
        assert v.categories == ()

    def test_unsafe_verdict_with_rule_names(self) -> None:
        v = LlamaGuardVerdict(
            safe=False,
            categories=("S1",),
            rule_names=("llama_guard.violent_crimes",),
            raw_response="unsafe\nS1",
        )
        assert v.safe is False
        assert v.rule_names == ("llama_guard.violent_crimes",)
