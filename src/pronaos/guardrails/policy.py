"""Per-tenant guardrail policy resolver.

The team's ``guardrail_policy`` JSON column carries operator-authored
overrides for the gateway-wide rule set. This module parses that JSON
into the shape ``DefaultGuardrailEngine.scan_*`` accepts:

    disabled_rules:    set[str]       # rules to skip entirely
    policy_override:   dict[str, GuardrailAction]   # per-rule action override

Validation is defensive: a malformed JSON policy degrades gracefully to
"use engine defaults" rather than crashing the request. The trade-off:
an operator who mis-types a rule name gets silent fallback to defaults
rather than a guardrail outage. The CLI / admin endpoint should
validate at write time so malformed policies don't reach this code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pronaos.guardrails.base import GuardrailAction
from pronaos.logging import get_logger

log = get_logger(__name__)

_VALID_ACTIONS = {a.value for a in GuardrailAction}


def resolve_policy(
    raw: Mapping[str, Any] | None,
) -> tuple[set[str] | None, dict[str, GuardrailAction] | None]:
    """Return ``(disabled_rules, policy_override)`` from a raw JSON policy.

    Either or both can be ``None`` when the policy is missing or empty.
    The chat handler passes them through to ``engine.scan_ingress`` /
    ``scan_egress``; ``None`` means "no override, use engine defaults."
    """
    if not isinstance(raw, Mapping):
        return None, None

    disabled = _parse_disabled_rules(raw.get("disabled_rules"))
    override = _parse_rule_actions(raw.get("rule_actions"))
    return disabled, override


def _parse_disabled_rules(value: object) -> set[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        log.warning(
            "guardrails.policy.disabled_rules_invalid",
            type=type(value).__name__,
        )
        return None
    rules = {r for r in value if isinstance(r, str) and r}
    return rules or None


def _parse_rule_actions(
    value: object,
) -> dict[str, GuardrailAction] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        log.warning(
            "guardrails.policy.rule_actions_invalid",
            type=type(value).__name__,
        )
        return None

    out: dict[str, GuardrailAction] = {}
    for rule_name, action_str in value.items():
        if not isinstance(rule_name, str) or not isinstance(action_str, str):
            continue
        if action_str.lower() not in _VALID_ACTIONS:
            log.warning(
                "guardrails.policy.unknown_action",
                rule=rule_name,
                action=action_str,
            )
            continue
        out[rule_name] = GuardrailAction(action_str.lower())
    return out or None


def validate_policy(raw: Mapping[str, Any] | None) -> list[str]:
    """Return a list of error strings for an admin-supplied policy.

    Used by the CLI / admin endpoint at write time. Empty list = valid.
    """
    errors: list[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, Mapping):
        errors.append(f"policy must be a JSON object, got {type(raw).__name__}")
        return errors

    extra = set(raw.keys()) - {"disabled_rules", "rule_actions"}
    if extra:
        errors.append(f"unknown policy keys: {sorted(extra)}")

    disabled = raw.get("disabled_rules", [])
    if not isinstance(disabled, list) or not all(
        isinstance(r, str) for r in disabled
    ):
        errors.append("disabled_rules must be a list of strings")

    actions = raw.get("rule_actions", {})
    if not isinstance(actions, Mapping):
        errors.append("rule_actions must be a mapping")
    else:
        for rule, action in actions.items():
            if not isinstance(rule, str) or not isinstance(action, str):
                errors.append(
                    f"rule_actions entries must be (str, str); "
                    f"got ({type(rule).__name__}, {type(action).__name__})"
                )
                continue
            if action.lower() not in _VALID_ACTIONS:
                errors.append(
                    f"rule_actions[{rule!r}] = {action!r}; "
                    f"valid actions: {sorted(_VALID_ACTIONS)}"
                )

    return errors
