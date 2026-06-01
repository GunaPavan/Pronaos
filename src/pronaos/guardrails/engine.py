"""Default ``GuardrailEngine`` implementation.

Composes a list of rules and applies a policy mapping per-rule action.
The engine is stateless and synchronous — guardrails are fast enough
that adding async indirection would cost more than it saves. Callers
that need to run guardrails off the request thread can ``asyncio.to_thread``
around ``scan_*`` themselves.

Policy resolution
-----------------
A ``policy`` dict maps rule name → action. Lookup order:

1. Per-tenant override (deferred; phase 8.2)
2. Engine-level ``policy`` argument
3. Rule's ``default_action``

This three-level fallback means a rule shipped with a safe default
keeps working even when no policy is configured.

Egress vs ingress
-----------------
Egress guardrails never BLOCK — by the time the response is in hand,
the upstream call already happened. Blocking now degrades UX without
preventing the leak. Egress treats every BLOCK as REDACT. Ingress
honours BLOCK fully (caller returns 422).

TOKENIZE action (Phase 38)
--------------------------
Honoured only when the caller passes ``tenant_id`` AND
``tokenization_enabled=True`` on the scan call. Otherwise the engine
silently downgrades TOKENIZE → REDACT (one-way), preserving existing
behaviour for teams that haven't opted in. The downgrade is by design:
operator writes ``"pii.email": "tokenize"`` in policy + flips the team
flag = tokenization on. Just one of those = fallback redaction. Two
opt-ins prevent accidents.
"""

from __future__ import annotations

from collections.abc import Mapping

from pronaos.core.pii_tokens import tokenize_hits
from pronaos.guardrails.base import (
    GuardrailAction,
    GuardrailEngine,
    GuardrailRule,
    GuardrailVerdict,
    RuleHit,
    apply_redactions,
)
from pronaos.logging import get_logger

log = get_logger(__name__)


class DefaultGuardrailEngine(GuardrailEngine):
    """Rules-in-order + policy-applied implementation."""

    def __init__(
        self,
        *,
        rules: list[GuardrailRule],
        policy: Mapping[str, GuardrailAction] | None = None,
    ) -> None:
        self._rules = rules
        self._policy = dict(policy or {})

    # ------------------------------------------------------------------ #
    # GuardrailEngine                                                    #
    # ------------------------------------------------------------------ #

    def scan_ingress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
        tenant_id: str | None = None,
        tokenization_enabled: bool = False,
    ) -> GuardrailVerdict:
        return self._scan(
            text,
            egress=False,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
            tenant_id=tenant_id,
            tokenization_enabled=tokenization_enabled,
        )

    def scan_egress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
        tenant_id: str | None = None,
        tokenization_enabled: bool = False,
    ) -> GuardrailVerdict:
        return self._scan(
            text,
            egress=True,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
            tenant_id=tenant_id,
            tokenization_enabled=tokenization_enabled,
        )

    # ------------------------------------------------------------------ #
    # Internal                                                           #
    # ------------------------------------------------------------------ #

    def _scan(
        self,
        text: str,
        *,
        egress: bool,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
        tenant_id: str | None = None,
        tokenization_enabled: bool = False,
    ) -> GuardrailVerdict:
        """Single pass over all rules. Collects hits, partitions by
        action, builds the verdict.

        Per-call ``policy_override`` and ``disabled_rules`` (Phase 8.2)
        let the chat handler apply tenant-specific policy without
        constructing a fresh engine. ``disabled_rules`` skips the rule
        entirely; ``policy_override`` changes the action for a rule
        that DOES fire.

        Phase 38: when a rule's resolved action is ``TOKENIZE`` AND
        ``tokenization_enabled`` is True AND ``tenant_id`` is set,
        the hit is moved to the tokenize bucket instead of the redact
        bucket. The engine emits the per-tenant tokens and surfaces
        the mappings on the verdict. When tokenization isn't fully
        opted in, TOKENIZE silently degrades to REDACT — preserves
        existing behaviour for teams that haven't enabled it.

        Wrapped in a broad try/except so a rule exception can never
        propagate to the chat handler — gateway must stay up under
        any guardrail bug. The trade-off is conscious: log + fail-open."""
        try:
            all_hits: list[RuleHit] = []
            redact_hits: list[RuleHit] = []
            tokenize_hits_collected: list[tuple[str, tuple[int, int], str]] = []
            block_reason: str | None = None

            tokenize_active = tokenization_enabled and bool(tenant_id)

            for rule in self._rules:
                # Tenant policy can shut off entire rules. Skipping early
                # avoids running the regex on disabled rules — free win
                # for tenants that turn off expensive detectors.
                if disabled_rules and rule.name in disabled_rules:
                    continue

                hits = rule.scan(text)
                if not hits:
                    continue
                all_hits.extend(hits)

                action = self._resolve_action(rule, policy_override)
                if egress and action == GuardrailAction.BLOCK:
                    # Egress can't actually block — degrade to redact.
                    # The hits still get logged so we know it happened.
                    action = GuardrailAction.REDACT

                # Phase 38: TOKENIZE only fires when fully opted in.
                # Otherwise degrade to REDACT — operator writing a
                # policy that says "tokenize" without flipping the
                # team flag still gets PII protection (just one-way).
                if action == GuardrailAction.TOKENIZE and not tokenize_active:
                    action = GuardrailAction.REDACT

                if action == GuardrailAction.BLOCK and block_reason is None:
                    # First blocking hit wins for the reason string.
                    # Subsequent hits still get recorded but don't
                    # change the decision.
                    block_reason = rule.name

                if action == GuardrailAction.REDACT:
                    redact_hits.extend(hits)
                elif action == GuardrailAction.TOKENIZE:
                    for h in hits:
                        tokenize_hits_collected.append(
                            (rule.name, h.span, h.matched_text)
                        )

            if block_reason is not None:
                return GuardrailVerdict(
                    blocked=True, text=text, hits=all_hits, block_reason=block_reason
                )

            # Apply tokenization FIRST (right-to-left, span-aware), THEN
            # redactions on the tokenized text. Order matters when the
            # same value matched two rules with different actions —
            # tokenize takes precedence since the policy explicitly
            # opted into it. Indices for redact_hits are computed
            # against the ORIGINAL text, so after the tokenization
            # rewrites positions, we need to re-scan redact targets.
            # In practice rules don't overlap (different patterns),
            # so the simpler implementation: tokenize first, then if
            # there are any redact hits, re-scan + apply to the
            # tokenized text using the matched_text strings rather
            # than the now-stale spans.
            out_text = text
            tokenizations: list[tuple[str, str]] = []
            if tokenize_hits_collected and tenant_id is not None:
                out_text, tokenizations = tokenize_hits(
                    tenant_id=tenant_id,
                    text=text,
                    hits=tokenize_hits_collected,
                )
            if redact_hits:
                # If tokenization already ran, the indices in
                # ``redact_hits`` no longer point at the right
                # positions. Rebuild hits against the current text
                # by matched_text substring search. For the common
                # case (no tokenize hits) the spans are still valid
                # and this is a no-op.
                if tokenizations:
                    redact_hits = _rebind_hits_to_text(out_text, redact_hits)
                out_text = apply_redactions(out_text, redact_hits)

            return GuardrailVerdict(
                blocked=False,
                text=out_text,
                hits=all_hits,
                tokenizations=tokenizations,
            )
        except Exception as e:
            log.warning("guardrails.scan_failed", error=str(e), egress=egress)
            return GuardrailVerdict(blocked=False, text=text)

    def _resolve_action(
        self,
        rule: GuardrailRule,
        policy_override: Mapping[str, GuardrailAction] | None,
    ) -> GuardrailAction:
        """Precedence: per-call override > engine policy > rule default."""
        if policy_override and rule.name in policy_override:
            return policy_override[rule.name]
        if rule.name in self._policy:
            return self._policy[rule.name]
        return rule.default_action


def _rebind_hits_to_text(text: str, hits: list[RuleHit]) -> list[RuleHit]:
    """Rebuild a list of hits with spans re-anchored to ``text``.

    Phase 38: after tokenization rewrites the source string, redact
    hits' original spans no longer index correctly. Search for each
    hit's ``matched_text`` substring in the new text and emit a fresh
    hit at the discovered offset. Hits whose ``matched_text`` no
    longer appears (e.g. because the substring overlapped a region
    that got tokenized) are dropped — they're already gone from the
    output, so redacting them is a no-op anyway.
    """
    out: list[RuleHit] = []
    for h in hits:
        idx = text.find(h.matched_text)
        if idx == -1:
            continue
        out.append(
            RuleHit(
                rule=h.rule,
                span=(idx, idx + len(h.matched_text)),
                matched_text=h.matched_text,
                replacement_token=h.replacement_token,
            )
        )
    return out
