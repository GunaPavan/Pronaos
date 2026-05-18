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
"""

from __future__ import annotations

from collections.abc import Mapping

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
    ) -> GuardrailVerdict:
        return self._scan(
            text,
            egress=False,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
        )

    def scan_egress(
        self,
        text: str,
        *,
        policy_override: Mapping[str, GuardrailAction] | None = None,
        disabled_rules: set[str] | None = None,
    ) -> GuardrailVerdict:
        return self._scan(
            text,
            egress=True,
            policy_override=policy_override,
            disabled_rules=disabled_rules,
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
    ) -> GuardrailVerdict:
        """Single pass over all rules. Collects hits, partitions by
        action, builds the verdict.

        Per-call ``policy_override`` and ``disabled_rules`` (Phase 8.2)
        let the chat handler apply tenant-specific policy without
        constructing a fresh engine. ``disabled_rules`` skips the rule
        entirely; ``policy_override`` changes the action for a rule
        that DOES fire.

        Wrapped in a broad try/except so a rule exception can never
        propagate to the chat handler — gateway must stay up under
        any guardrail bug. The trade-off is conscious: log + fail-open."""
        try:
            all_hits: list[RuleHit] = []
            redact_hits: list[RuleHit] = []
            block_reason: str | None = None

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

                if action == GuardrailAction.BLOCK and block_reason is None:
                    # First blocking hit wins for the reason string.
                    # Subsequent hits still get recorded but don't
                    # change the decision.
                    block_reason = rule.name

                if action == GuardrailAction.REDACT:
                    redact_hits.extend(hits)

            if block_reason is not None:
                return GuardrailVerdict(
                    blocked=True, text=text, hits=all_hits, block_reason=block_reason
                )

            redacted = apply_redactions(text, redact_hits) if redact_hits else text
            return GuardrailVerdict(blocked=False, text=redacted, hits=all_hits)
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
