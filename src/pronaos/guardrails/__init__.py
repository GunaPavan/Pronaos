"""Guardrails — request/response inspection for PII and prompt injection.

Two pass points in the chat pipeline:

- **Ingress** (before cache lookup): scan user messages. PII is redacted
  before forming the cache key, so cached responses never contain
  customer PII regardless of downstream behaviour.
- **Egress** (after provider call): scan the assistant response. Catches
  the model regurgitating PII from its training data (a real attack
  surface — Anthropic, OpenAI, and others have all published incidents
  where models leak training-set strings).

Two rule families ship today:

- ``RegexPIIDetector`` — fast, deterministic. Catches emails, US phone
  numbers, SSNs, credit-card numbers (with Luhn check), and IPv4
  addresses. Misses unstructured PII (names, addresses) — that's a
  Phase 8.2 ML-classifier job.
- ``PromptInjectionDetector`` — heuristic pattern match for known
  jailbreak preambles ("ignore previous instructions", role-confusion
  attacks). Defaults to LOG_ONLY because false-positive risk is high
  on legitimate "how do I write a prompt that…" queries.

Policy
------
Each rule has a configured action: BLOCK | REDACT | LOG_ONLY. PII
defaults to REDACT (request still goes through with sensitive spans
masked). Injection defaults to LOG_ONLY in shadow mode. Per-tenant
overrides ship in a later phase.

Fail-open
---------
Engine errors are logged but never propagate — a guardrail bug must
not break the gateway. The trade-off is conscious: silently letting
one request through is preferable to taking the gateway down.
"""

from pronaos.guardrails.base import (
    GuardrailAction,
    GuardrailEngine,
    GuardrailRule,
    GuardrailVerdict,
    NullGuardrailEngine,
    RuleHit,
)
from pronaos.guardrails.detectors import (
    PromptInjectionDetector,
    RegexPIIDetector,
)

__all__ = [
    "GuardrailAction",
    "GuardrailEngine",
    "GuardrailRule",
    "GuardrailVerdict",
    "NullGuardrailEngine",
    "PromptInjectionDetector",
    "RegexPIIDetector",
    "RuleHit",
]
