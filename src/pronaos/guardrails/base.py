"""Protocol + result types shared by every guardrail rule.

Design notes
------------
- ``GuardrailRule`` is a Protocol, not an ABC. Same rationale as the
  cache: production rules and test fakes both satisfy it without
  inheritance, keeping the engine single-dispatch.
- The engine returns a single ``GuardrailVerdict`` rather than raw rule
  hits. The verdict carries the post-policy decision (blocked? redacted
  text? hits to log?) so the chat handler has one shape to consume.
- Rules report *what they found*. The engine decides *what to do about
  it* based on the configured policy. This split lets the same rule run
  under different policies (e.g. PII redaction at the gateway, PII
  blocking on a strict tenant) without rewriting detection logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class GuardrailAction(StrEnum):
    """What the policy tells the engine to do with a rule hit."""

    BLOCK = "block"
    REDACT = "redact"
    LOG_ONLY = "log_only"


@dataclass(frozen=True, slots=True)
class RuleHit:
    """One detection from a single rule.

    ``span`` is an inclusive-exclusive offset into the original text;
    the engine uses it to perform redaction with the rule's
    ``replacement_token``. ``rule`` is the canonical short name used
    for metric labels (e.g. "pii.email", "pii.ssn", "injection").
    """

    rule: str
    span: tuple[int, int]
    matched_text: str
    # The string that replaces the matched span on REDACT. Kept on the
    # hit (not derived from rule name) so a single rule can vary its
    # replacement by what it matched — e.g. a credit-card detector
    # might want to preserve last-4 digits while masking the rest.
    replacement_token: str


class GuardrailRule(Protocol):
    """Stateless content scanner."""

    @property
    def name(self) -> str:
        """Canonical rule id used for metrics + policy lookup."""
        ...

    @property
    def default_action(self) -> GuardrailAction:
        """What to do by default when this rule fires. Tenant policy
        can override; this is the engine fallback."""
        ...

    def scan(self, text: str) -> list[RuleHit]:
        """Return all hits in ``text``. Empty list = clean."""
        ...


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """Engine output after applying policy to one text input.

    ``blocked`` short-circuits the chat handler (caller returns 422).
    ``text`` is the (possibly redacted) content to use downstream —
    the chat handler must use *this* string, never the original, when
    computing cache keys or forwarding to the provider.
    ``hits`` carries every fired rule for metrics/logging regardless
    of action.
    """

    blocked: bool
    text: str
    hits: list[RuleHit] = field(default_factory=list)
    # The first rule whose policy was BLOCK. Carried so the chat
    # handler can include it in the 422 error body for the client.
    block_reason: str | None = None


class GuardrailEngine(Protocol):
    """Composes rules and applies policy. One engine per app.

    The optional ``policy_override`` and ``disabled_rules`` parameters
    let the chat handler apply per-tenant policy (Phase 8.2) without
    constructing a new engine per request. Merge precedence:

        per-call override > engine-level policy > rule.default_action

    ``disabled_rules`` skips a rule entirely (it isn't even scanned —
    saves the regex pass).
    """

    def scan_ingress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
    ) -> GuardrailVerdict:
        """Run ingress rules against a user message."""
        ...

    def scan_egress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
    ) -> GuardrailVerdict:
        """Run egress rules against an assistant response. Egress
        defaults to REDACT-only behaviour even if ingress would BLOCK —
        the upstream call already happened, blocking now just degrades
        UX without preventing the leak."""
        ...


class NullGuardrailEngine(GuardrailEngine):
    """No-op engine for tests / when guardrails are disabled.

    Returns a clean verdict for every input. Lets the chat handler
    stay guardrail-aware without special-casing the disabled path."""

    def scan_ingress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
    ) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=False, text=text)

    def scan_egress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
    ) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=False, text=text)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def apply_redactions(text: str, hits: Iterable[RuleHit]) -> str:
    """Apply ``hits`` to ``text``, returning the redacted string.

    Hits are applied right-to-left so earlier spans' indices aren't
    invalidated by upstream replacements. Overlapping hits are resolved
    by keeping the first one — the engine is responsible for de-duping
    before calling this (typically by giving rule order priority).
    """
    # Sort descending by start offset.
    ordered = sorted(hits, key=lambda h: h.span[0], reverse=True)
    out = text
    last_start: int | None = None
    for hit in ordered:
        start, end = hit.span
        # Skip a hit that overlaps a later (higher-offset) span we
        # already applied. ``last_start`` tracks the leftmost edge of
        # the most recently-applied replacement.
        if last_start is not None and end > last_start:
            continue
        out = out[:start] + hit.replacement_token + out[end:]
        last_start = start
    return out
