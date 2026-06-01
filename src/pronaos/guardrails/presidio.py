"""ML-backed PII detector built on Microsoft Presidio (Phase 22).

Why Presidio
------------
Regex catches structured PII (emails, US-format phones, SSN dashes,
IPv4, credit cards). It misses:

- **Names** (no syntactic pattern in English)
- **Locations** (street addresses without obvious zip codes)
- **Spelled-out numbers** ("two three four five..." for SSN)
- **Foreign formats** that don't match the US patterns
- **Conversational PII** ("my date of birth is January 3rd, 1985")

Presidio's AnalyzerEngine wraps spaCy NER + a pluggable recognizer
pipeline that handles those long-tail cases. The trade-off is weight:
spaCy + presidio-analyzer pull in ~600 MB of model + lib state. That's
why Pronaos ships with this detector **OFF by default** — operators
on regulated workloads opt in via ``PRONAOS_PRESIDIO_ENABLED=true``
and the per-team ``guardrail_policy`` then decides which entity
types to actually scan.

Interface
---------
``PresidioPIIDetector`` exposes the same ``GuardrailRule`` protocol
the regex detectors use. The engine doesn't care that one is ML and
the others are regex — they all return ``RuleHit`` objects on the
same span/replacement contract.

Rule naming is ``presidio.<ENTITY_TYPE>`` (e.g. ``presidio.PERSON``,
``presidio.LOCATION``, ``presidio.DATE_TIME``) so dashboards and
per-team policy can filter at the entity-type level. A single detector
*instance* covers a configured list of entity types; the per-call
``policy_override`` and ``disabled_rules`` shapes work unchanged.

Lazy initialisation
-------------------
Building an ``AnalyzerEngine`` triggers spaCy's model load (~1-2 s,
~250 MB RAM). We defer that until the first ``scan`` call so the
gateway boots fast even with Presidio enabled — the first request
in pays the cost, every subsequent one is microseconds.

Fail-open
---------
Presidio is a soft dependency. ``ImportError`` / model-load errors
return an empty hit list and log a warning. The gateway must keep
serving even if the ML model is missing — the regex detectors are
still in place.
"""

from __future__ import annotations

from typing import Any

from pronaos.guardrails.base import GuardrailAction, GuardrailRule, RuleHit
from pronaos.logging import get_logger

log = get_logger(__name__)

# Entity types Pronaos cares about by default. Presidio knows many more
# (URL, IBAN, MEDICAL_LICENSE, etc.) — operators can extend this via
# the per-team policy. Kept conservative so an out-of-the-box deploy
# doesn't redact aggressive noise (e.g. random datetime strings in code
# samples becoming [REDACTED-DATE]).
DEFAULT_ENTITIES: tuple[str, ...] = (
    "PERSON",
    "LOCATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "DATE_TIME",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_PASSPORT",
    "IBAN_CODE",
)


def _replacement_for(entity_type: str) -> str:
    """Map a Presidio entity type to a human-readable redaction token.

    Token shape matches the regex detectors' ``[REDACTED-EMAIL]`` /
    ``[REDACTED-SSN]`` convention so downstream consumers (logs,
    audit summaries) can grep for ``[REDACTED-*]`` regardless of
    which detector fired.
    """
    return f"[REDACTED-{entity_type}]"


class PresidioPIIDetector(GuardrailRule):
    """ML-based PII detector wrapping Presidio's AnalyzerEngine.

    One *detector* covers a tuple of entity types. Each hit reports
    its specific entity type via ``RuleHit.rule`` so metrics and
    per-team policy can target the entity granularly:

    - ``presidio.PERSON`` for names
    - ``presidio.LOCATION`` for places
    - ``presidio.DATE_TIME`` for dates
    - ...etc.

    The detector itself reports under the generic name ``presidio``
    (so a team policy can disable the whole engine with one
    ``"disabled_rules": ["presidio"]`` entry without listing every
    entity type), while each emitted hit carries the entity-specific
    name for fine-grained metrics + override.

    Confidence filter: Presidio reports a ``score`` on every match.
    Below ``min_score`` we drop the hit. The default 0.5 matches
    Presidio's own conservative threshold; tighter thresholds reduce
    false-positives at the cost of recall.
    """

    _NAME = "presidio"

    def __init__(
        self,
        *,
        entities: tuple[str, ...] = DEFAULT_ENTITIES,
        min_score: float = 0.5,
        language: str = "en",
        default_action: GuardrailAction = GuardrailAction.REDACT,
    ) -> None:
        self._entities = entities
        self._min_score = min_score
        self._language = language
        self._default_action = default_action
        # ``_analyzer`` is lazily built on first scan. ``None`` = not yet
        # attempted. ``False`` = attempted and failed (sentinel-as-state
        # avoids retrying every request when Presidio is broken).
        self._analyzer: Any | None | bool = None

    # ------------------------------------------------------------------ #
    # GuardrailRule                                                      #
    # ------------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def default_action(self) -> GuardrailAction:
        return self._default_action

    def scan(self, text: str) -> list[RuleHit]:
        analyzer = self._ensure_analyzer()
        if analyzer is None:
            return []
        if not text:
            return []
        try:
            results = analyzer.analyze(
                text=text,
                language=self._language,
                entities=list(self._entities),
                score_threshold=self._min_score,
            )
        except Exception as e:
            # Presidio runtime error during analyze() — log and fail-open.
            # We DON'T null the analyzer here because the failure may be
            # input-specific; the next scan should still try.
            log.warning("guardrails.presidio.analyze_failed", error=str(e))
            return []

        hits: list[RuleHit] = []
        for r in results:
            # Each Presidio result has: entity_type, start, end, score.
            # Our RuleHit names per-entity-type so policies + metrics
            # work at that granularity.
            entity = r.entity_type
            hits.append(
                RuleHit(
                    rule=f"{self._NAME}.{entity}",
                    span=(r.start, r.end),
                    matched_text=text[r.start : r.end],
                    replacement_token=_replacement_for(entity),
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    # Lazy init                                                          #
    # ------------------------------------------------------------------ #

    def _ensure_analyzer(self) -> Any | None:
        """Build the AnalyzerEngine on first call; cache for the lifetime.

        Returns ``None`` if Presidio isn't importable or the engine
        failed to construct — the scanner then degrades to a no-op for
        every future call. We don't retry init: spaCy model load is
        expensive enough that a fail-loop would be worse than a clean
        disable.
        """
        if self._analyzer is False:
            # Previous init failed; permanently no-op.
            return None
        if self._analyzer is not None:
            return self._analyzer

        try:
            from presidio_analyzer import AnalyzerEngine
        except ImportError as e:
            log.warning(
                "guardrails.presidio.import_failed",
                error=str(e),
                hint="pip install presidio-analyzer && python -m spacy download en_core_web_lg",
            )
            self._analyzer = False
            return None

        try:
            engine = AnalyzerEngine()
        except Exception as e:
            log.warning(
                "guardrails.presidio.init_failed",
                error=str(e),
                hint="ensure spaCy model en_core_web_lg is installed",
            )
            self._analyzer = False
            return None

        log.info(
            "guardrails.presidio.ready",
            entities=list(self._entities),
            min_score=self._min_score,
            language=self._language,
        )
        self._analyzer = engine
        return engine


def make_presidio_detector(
    *,
    entities: tuple[str, ...] = DEFAULT_ENTITIES,
    min_score: float = 0.5,
) -> PresidioPIIDetector:
    """Construct a Presidio detector with the default entity set.

    Convenience wrapper so the factory can build one without
    threading every keyword arg.
    """
    return PresidioPIIDetector(entities=entities, min_score=min_score)
