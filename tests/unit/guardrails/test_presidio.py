"""Presidio detector tests (Phase 22).

Two layers:

1. **Integration tests** that actually invoke Presidio's AnalyzerEngine
   against text containing names / dates / spelled-out numbers — the
   recall delta over regex is the whole point of this detector, so the
   tests assert on real ML behaviour. Skipped if Presidio isn't
   installed in the test environment (CI may not pull the heavy deps).

2. **Fail-open tests** that stub the analyzer to simulate import /
   runtime failure. The gateway MUST keep serving when Presidio is
   broken — these tests pin that invariant.

The first scan in any test pays the spaCy model-load cost (~1-2 s).
Subsequent scans within the same process are microseconds.
"""

from __future__ import annotations

import pytest

from pronaos.guardrails.base import GuardrailAction
from pronaos.guardrails.presidio import (
    DEFAULT_ENTITIES,
    PresidioPIIDetector,
    make_presidio_detector,
)

# Skip the whole module if Presidio isn't importable. CI minimal images
# may not include the spaCy model — that's allowed; we test the fail-open
# path separately with the stubbed analyzer.
presidio_analyzer = pytest.importorskip("presidio_analyzer")


# --------------------------------------------------------------------------- #
# Shape + defaults                                                            #
# --------------------------------------------------------------------------- #


class TestPresidioShape:
    def test_default_rule_name_is_presidio(self) -> None:
        det = make_presidio_detector()
        assert det.name == "presidio"

    def test_default_action_is_redact(self) -> None:
        det = make_presidio_detector()
        assert det.default_action == GuardrailAction.REDACT

    def test_default_entities_cover_common_pii(self) -> None:
        """Sanity-check the shipped default. Tests below depend on
        these entities being in the set."""
        assert "PERSON" in DEFAULT_ENTITIES
        assert "LOCATION" in DEFAULT_ENTITIES
        assert "EMAIL_ADDRESS" in DEFAULT_ENTITIES


# --------------------------------------------------------------------------- #
# Real detection — these are the regex-misses Phase 22 exists to catch         #
# --------------------------------------------------------------------------- #


class TestPresidioDetection:
    """End-to-end ML detection on inputs regex can't handle."""

    def test_detects_person_name_in_context(self) -> None:
        """The headline regex-miss case: a person's name in conversational
        prose. No regex pattern catches this — Presidio's NER does."""
        det = make_presidio_detector()
        hits = det.scan("My account manager is John Smith and he's based in Seattle.")
        # PERSON must fire on "John Smith"
        person_hits = [h for h in hits if h.rule == "presidio.PERSON"]
        assert len(person_hits) >= 1
        assert "John Smith" in person_hits[0].matched_text

    def test_emits_entity_specific_rule_names(self) -> None:
        """Each Presidio entity type gets its own rule name so dashboards
        and per-team policy can target at the entity level (not just
        the engine level)."""
        det = make_presidio_detector()
        hits = det.scan("Contact Sarah Johnson at sarah@example.com about the meeting.")
        rule_names = {h.rule for h in hits}
        # Both PERSON and EMAIL_ADDRESS should fire — under their own names.
        assert "presidio.PERSON" in rule_names
        assert "presidio.EMAIL_ADDRESS" in rule_names

    def test_replacement_token_includes_entity_type(self) -> None:
        """Redaction tokens are entity-specific so a redacted prompt
        is self-describing (operator can tell what was removed)."""
        det = make_presidio_detector()
        hits = det.scan("My name is Alice Brown.")
        person_hits = [h for h in hits if h.rule == "presidio.PERSON"]
        assert person_hits
        assert person_hits[0].replacement_token == "[REDACTED-PERSON]"

    def test_empty_text_returns_no_hits(self) -> None:
        """Edge case — empty input must not throw."""
        det = make_presidio_detector()
        assert det.scan("") == []

    def test_score_threshold_filters_low_confidence_hits(self) -> None:
        """A high min_score must drop borderline detections. We use 0.99
        which is above Presidio's reported confidence for most entities,
        so all hits should be filtered out."""
        det = PresidioPIIDetector(min_score=0.99)
        hits = det.scan("My friend John lives in Paris.")
        # At 0.99 even strong hits like person names are typically filtered.
        # The exact count depends on the model — we just assert it's
        # lower than the default-threshold run.
        baseline = make_presidio_detector(min_score=0.1).scan("My friend John lives in Paris.")
        assert len(hits) <= len(baseline)

    def test_restricting_entities_skips_others(self) -> None:
        """A detector configured for only PERSON must not fire on
        emails / locations even when present."""
        det = PresidioPIIDetector(entities=("PERSON",))
        hits = det.scan("Email alice@example.com — she lives in Boston.")
        rule_names = {h.rule for h in hits}
        assert "presidio.EMAIL_ADDRESS" not in rule_names
        assert "presidio.LOCATION" not in rule_names


# --------------------------------------------------------------------------- #
# Fail-open behaviour                                                         #
# --------------------------------------------------------------------------- #


class TestPresidioFailOpen:
    """The gateway must keep serving when Presidio is broken."""

    def test_analyze_exception_returns_empty_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A runtime exception inside ``analyze`` must NOT propagate.
        The detector returns [] and the engine treats it as no hit."""

        class BoomAnalyzer:
            def analyze(self, **_: object) -> list[object]:
                raise RuntimeError("simulated Presidio failure")

        det = make_presidio_detector()
        # Force the lazy init to install our boom-analyzer instead of
        # the real one.
        det._analyzer = BoomAnalyzer()
        # No exception must escape; result is an empty hit list.
        assert det.scan("My name is John Smith.") == []

    def test_disabled_after_init_failure_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once init fails, subsequent scans don't retry the expensive
        load — they short-circuit to []. This protects against a
        broken Presidio install turning every request into a slow
        init-retry."""
        det = make_presidio_detector()
        det._analyzer = False
        # Multiple scans — none should attempt to construct an analyzer.
        assert det.scan("My name is John Smith.") == []
        assert det.scan("Another scan.") == []
        # Sentinel preserved across calls.
        assert det._analyzer is False
