"""Engine + policy behaviour.

Detectors are unit-tested in ``test_detectors.py``. This file exercises
how the engine composes them, applies policy, and short-circuits on
BLOCK. Uses simple in-test rules where possible so the assertions don't
depend on the concrete regex patterns.
"""

from __future__ import annotations

from pronaos.guardrails.base import (
    GuardrailAction,
    GuardrailRule,
    RuleHit,
)
from pronaos.guardrails.detectors import default_pii_detectors
from pronaos.guardrails.engine import DefaultGuardrailEngine

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class StubRule(GuardrailRule):
    """Test rule that emits one fixed hit when ``trigger`` appears in the text."""

    def __init__(
        self,
        name: str,
        trigger: str,
        *,
        default_action: GuardrailAction = GuardrailAction.REDACT,
        replacement: str = "[X]",
    ) -> None:
        self._name = name
        self._trigger = trigger
        self._action = default_action
        self._replacement = replacement

    @property
    def name(self) -> str:
        return self._name

    @property
    def default_action(self) -> GuardrailAction:
        return self._action

    def scan(self, text: str) -> list[RuleHit]:
        # Real rules emit one hit per occurrence — walk the whole string
        # so the multi-hit redaction path is exercised correctly.
        hits: list[RuleHit] = []
        start = 0
        while True:
            idx = text.find(self._trigger, start)
            if idx == -1:
                break
            hits.append(
                RuleHit(
                    rule=self._name,
                    span=(idx, idx + len(self._trigger)),
                    matched_text=self._trigger,
                    replacement_token=self._replacement,
                )
            )
            start = idx + len(self._trigger)
        return hits


# --------------------------------------------------------------------------- #
# REDACT action                                                                #
# --------------------------------------------------------------------------- #


def test_redact_replaces_matched_span() -> None:
    """A REDACT rule must rewrite the verdict text with the replacement
    token. This is the engine's primary correctness contract."""
    engine = DefaultGuardrailEngine(rules=[StubRule("test", "secret", replacement="[X]")])
    verdict = engine.scan_ingress("the secret is out")
    assert verdict.blocked is False
    assert verdict.text == "the [X] is out"
    assert len(verdict.hits) == 1


def test_multiple_hits_all_redacted() -> None:
    """Two separate trigger occurrences both get masked. Redaction is
    applied right-to-left so the second hit doesn't shift the first
    hit's offset."""
    engine = DefaultGuardrailEngine(rules=[StubRule("t", "abc", replacement="X")])
    verdict = engine.scan_ingress("abc def abc ghi")
    assert verdict.text == "X def X ghi"
    assert len(verdict.hits) == 2


# --------------------------------------------------------------------------- #
# BLOCK action                                                                 #
# --------------------------------------------------------------------------- #


def test_block_action_short_circuits_with_reason() -> None:
    """A BLOCK action marks the verdict and carries the rule name so
    the chat handler can pass it back to the client."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("forbidden", "kill-switch", default_action=GuardrailAction.BLOCK)]
    )
    verdict = engine.scan_ingress("please hit the kill-switch")
    assert verdict.blocked is True
    assert verdict.block_reason == "forbidden"


def test_block_on_egress_downgrades_to_redact() -> None:
    """Egress can never BLOCK — the provider call already happened.
    The engine must downgrade BLOCK to REDACT when scan_egress is
    called, even if the rule's default action is BLOCK."""
    engine = DefaultGuardrailEngine(
        rules=[
            StubRule(
                "leak", "training-data", default_action=GuardrailAction.BLOCK, replacement="[X]"
            )
        ]
    )
    verdict = engine.scan_egress("found training-data in response")
    assert verdict.blocked is False
    assert verdict.text == "found [X] in response"


# --------------------------------------------------------------------------- #
# LOG_ONLY action                                                              #
# --------------------------------------------------------------------------- #


def test_log_only_records_hit_but_does_not_change_text() -> None:
    """LOG_ONLY hits show up in ``verdict.hits`` (so they hit metrics)
    but the text is returned unmodified — the audit trail without the
    behavioural change."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("probe", "TRACE", default_action=GuardrailAction.LOG_ONLY)]
    )
    verdict = engine.scan_ingress("contains TRACE marker")
    assert verdict.text == "contains TRACE marker"
    assert len(verdict.hits) == 1


# --------------------------------------------------------------------------- #
# Policy override                                                              #
# --------------------------------------------------------------------------- #


def test_policy_can_override_rule_default() -> None:
    """A rule's default_action is the fallback; a configured policy
    override takes precedence. Operators promote LOG_ONLY → REDACT
    via this path to enforce after a shadow-mode period."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("probe", "X", default_action=GuardrailAction.LOG_ONLY, replacement="?")],
        policy={"probe": GuardrailAction.REDACT},
    )
    verdict = engine.scan_ingress("a X here")
    assert verdict.text == "a ? here"


# --------------------------------------------------------------------------- #
# Real rules end-to-end                                                       #
# --------------------------------------------------------------------------- #


def test_default_pii_rules_redact_in_one_pass() -> None:
    """A prompt with email + SSN + IP all gets redacted in a single
    engine pass. This is the full-stack assertion that the default
    PII rule set wires correctly through the engine."""
    engine = DefaultGuardrailEngine(rules=default_pii_detectors())
    verdict = engine.scan_ingress("Contact alice@example.com, SSN 123-45-6789, server 10.0.0.1")
    assert verdict.blocked is False
    assert "[REDACTED-EMAIL]" in verdict.text
    assert "[REDACTED-SSN]" in verdict.text
    assert "[REDACTED-IP]" in verdict.text
    # And the original PII is gone:
    assert "alice@example.com" not in verdict.text
    assert "123-45-6789" not in verdict.text
    assert "10.0.0.1" not in verdict.text


# --------------------------------------------------------------------------- #
# Fail-open                                                                   #
# --------------------------------------------------------------------------- #


class ExplodingRule(GuardrailRule):
    @property
    def name(self) -> str:
        return "boom"

    @property
    def default_action(self) -> GuardrailAction:
        return GuardrailAction.REDACT

    def scan(self, text: str) -> list[RuleHit]:
        raise RuntimeError("intentional test failure")


def test_engine_fails_open_on_rule_exception() -> None:
    """If a rule raises, the engine returns the original text and an
    empty hit list — gateway must stay up under a guardrail bug. The
    trade-off is conscious: one request slips through unscanned
    rather than tear down the whole serving path."""
    engine = DefaultGuardrailEngine(rules=[ExplodingRule()])
    verdict = engine.scan_ingress("anything")
    assert verdict.blocked is False
    assert verdict.text == "anything"
    assert verdict.hits == []


# --------------------------------------------------------------------------- #
# TOKENIZE action (Phase 38)                                                  #
# --------------------------------------------------------------------------- #


def test_tokenize_action_produces_token_and_mapping() -> None:
    """When tokenization is active, the engine emits a deterministic
    token and surfaces the (token, original) mapping on the verdict
    so the chat handler can persist it to Redis."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("pii.email", "a@b.c")],
        policy={"pii.email": GuardrailAction.TOKENIZE},
    )
    verdict = engine.scan_ingress(
        "email me at a@b.c please",
        tenant_id="tenant-1",
        tokenization_enabled=True,
    )
    assert verdict.blocked is False
    assert "a@b.c" not in verdict.text
    assert "[EMAIL_" in verdict.text
    assert len(verdict.tokenizations) == 1
    token, original = verdict.tokenizations[0]
    assert original == "a@b.c"
    assert token in verdict.text


def test_tokenize_falls_back_to_redact_when_team_not_opted_in() -> None:
    """Policy says ``tokenize`` but the team flag is off — the engine
    degrades to REDACT. Preserves existing behaviour for teams that
    haven't enabled tokenization."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("pii.email", "a@b.c", replacement="[REDACTED-EMAIL]")],
        policy={"pii.email": GuardrailAction.TOKENIZE},
    )
    verdict = engine.scan_ingress(
        "email a@b.c please",
        tenant_id="tenant-1",
        tokenization_enabled=False,
    )
    # Redacted, not tokenized.
    assert "[REDACTED-EMAIL]" in verdict.text
    assert verdict.tokenizations == []


def test_tokenize_falls_back_to_redact_when_tenant_missing() -> None:
    """Tokenization needs a tenant_id (for the salt); missing it
    degrades to REDACT. Same defence-in-depth as the flag check."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("pii.email", "a@b.c", replacement="[REDACTED-EMAIL]")],
        policy={"pii.email": GuardrailAction.TOKENIZE},
    )
    verdict = engine.scan_ingress(
        "email a@b.c please",
        tokenization_enabled=True,  # but tenant_id is None
    )
    assert "[REDACTED-EMAIL]" in verdict.text
    assert verdict.tokenizations == []


def test_tokenize_same_value_twice_uses_same_token() -> None:
    """Entity tracking — two mentions of the same value produce the
    same token. The mapping list has ONE entry."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("pii.email", "a@b.c")],
        policy={"pii.email": GuardrailAction.TOKENIZE},
    )
    verdict = engine.scan_ingress(
        "a@b.c first then a@b.c again",
        tenant_id="t",
        tokenization_enabled=True,
    )
    assert len(verdict.tokenizations) == 1
    token, _value = verdict.tokenizations[0]
    assert verdict.text.count(token) == 2
    assert "a@b.c" not in verdict.text


def test_tokenize_different_tenants_get_different_tokens() -> None:
    """Tenant isolation — the same value under two tenants produces
    distinct tokens (salted by tenant_id)."""
    engine = DefaultGuardrailEngine(
        rules=[StubRule("pii.email", "a@b.c")],
        policy={"pii.email": GuardrailAction.TOKENIZE},
    )
    v1 = engine.scan_ingress("a@b.c", tenant_id="alice", tokenization_enabled=True)
    v2 = engine.scan_ingress("a@b.c", tenant_id="bob", tokenization_enabled=True)
    assert v1.tokenizations != v2.tokenizations
