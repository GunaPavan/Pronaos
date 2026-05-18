"""Guardrail engine construction at startup.

Opt-out by default — guardrails ship enabled with REDACT defaults for
PII and LOG_ONLY for prompt injection. Operators with strict privacy
or interactive-prompt-engineering use-cases can disable via
``PRONAOS_GUARDRAILS_ENABLED=false`` and get a ``NullGuardrailEngine``.

Per-tenant policy overrides are out of scope here — that's a later
phase that adds a ``teams.guardrail_policy`` JSON column.
"""

from __future__ import annotations

from pronaos.config import Settings
from pronaos.guardrails.base import GuardrailEngine, NullGuardrailEngine
from pronaos.guardrails.detectors import (
    PromptInjectionDetector,
    default_pii_detectors,
)
from pronaos.guardrails.engine import DefaultGuardrailEngine
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
    log.info(
        "guardrails.enabled",
        rules=[r.name for r in rules],
    )
    return DefaultGuardrailEngine(rules=rules)
