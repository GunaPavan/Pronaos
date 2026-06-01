"""Guardrail engine construction at startup.

Opt-out by default — guardrails ship enabled with REDACT defaults for
PII and LOG_ONLY for prompt injection. Operators with strict privacy
or interactive-prompt-engineering use-cases can disable via
``PRONAOS_GUARDRAILS_ENABLED=false`` and get a ``NullGuardrailEngine``.

Per-tenant policy overrides happen at request time via the
``guardrail_policy`` column on Team — not at construction (Phase 8.2).

Presidio (Phase 22) is opt-in via ``PRONAOS_PRESIDIO_ENABLED=true``.
When enabled the ML detector runs alongside the regex detectors. Each
detector reports under its own rule name so per-team policy and
metrics work at the entity-type level (``presidio.PERSON``,
``presidio.LOCATION``, etc.).
"""

from __future__ import annotations

from pronaos.config import Settings
from pronaos.guardrails.base import GuardrailEngine, NullGuardrailEngine
from pronaos.guardrails.detectors import (
    PromptInjectionDetector,
    default_pii_detectors,
)
from pronaos.guardrails.engine import DefaultGuardrailEngine
from pronaos.guardrails.presidio import make_presidio_detector
from pronaos.logging import get_logger

log = get_logger(__name__)


def make_guardrail_engine(settings: Settings) -> GuardrailEngine:
    """Pick a guardrail engine based on configuration."""
    if not settings.guardrails_enabled:
        log.info("guardrails.disabled")
        return NullGuardrailEngine()

    rules = [
        *default_pii_detectors(),
        PromptInjectionDetector(),
    ]
    if settings.presidio_enabled:
        # Lazy init — the detector only loads spaCy on first scan.
        # Registering here keeps the operator opt-in explicit (one
        # env var) and lets per-team policy still toggle it off.
        rules.append(make_presidio_detector(min_score=settings.presidio_min_score))
        log.info(
            "guardrails.presidio.registered",
            min_score=settings.presidio_min_score,
        )

    log.info(
        "guardrails.enabled",
        rules=[r.name for r in rules],
    )
    return DefaultGuardrailEngine(rules=rules)
