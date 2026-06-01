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

    # Phase 22 — per-team Presidio toggle.
    # Shorthand: ``"presidio": {"enabled": false}`` adds the ``presidio``
    # rule (and any pre-configured entity-level rules) to disabled_rules.
    # This lets a team opt out of the ML detector entirely without having
    # to know that the rule canonical name is ``presidio``.
    presidio_block = raw.get("presidio")
    if isinstance(presidio_block, Mapping) and presidio_block.get("enabled") is False:
        disabled = (disabled or set()) | {"presidio"}

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

    extra = set(raw.keys()) - {"disabled_rules", "rule_actions", "presidio", "llama_guard"}
    if extra:
        errors.append(f"unknown policy keys: {sorted(extra)}")

    # Phase 44 — Llama Guard ML jailbreak classifier block. Shape:
    #   "llama_guard": {
    #       "enabled": bool,
    #       "model": str (optional override; falls back to settings),
    #       "default_action": "block" | "log_only" | "redact"
    #   }
    lg = raw.get("llama_guard")
    if lg is not None:
        if not isinstance(lg, Mapping):
            errors.append(f"llama_guard must be a mapping, got {type(lg).__name__}")
        else:
            allowed_lg = {"enabled", "model", "default_action"}
            unknown_lg = set(lg.keys()) - allowed_lg
            if unknown_lg:
                errors.append(
                    f"unknown llama_guard keys: {sorted(unknown_lg)}; "
                    f"allowed: {sorted(allowed_lg)}"
                )
            enabled = lg.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append(
                    f"llama_guard.enabled must be a boolean, got {type(enabled).__name__}"
                )
            model = lg.get("model")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                errors.append("llama_guard.model must be a non-empty string when set")
            action = lg.get("default_action")
            if action is not None:
                if not isinstance(action, str):
                    errors.append("llama_guard.default_action must be a string")
                elif action.lower() not in _VALID_ACTIONS:
                    errors.append(
                        f"llama_guard.default_action={action!r}; "
                        f"valid actions: {sorted(_VALID_ACTIONS)}"
                    )

    # Presidio block (Phase 22). Shape:
    #   "presidio": {
    #       "enabled": bool,
    #       "min_score": float (0..1),
    #       "entities": ["PERSON", "LOCATION", ...]
    #   }
    presidio = raw.get("presidio")
    if presidio is not None:
        if not isinstance(presidio, Mapping):
            errors.append(f"presidio must be a mapping, got {type(presidio).__name__}")
        else:
            allowed = {"enabled", "min_score", "entities"}
            unknown = set(presidio.keys()) - allowed
            if unknown:
                errors.append(
                    f"unknown presidio keys: {sorted(unknown)}; allowed: {sorted(allowed)}"
                )
            enabled = presidio.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append(f"presidio.enabled must be a boolean, got {type(enabled).__name__}")
            min_score = presidio.get("min_score")
            if min_score is not None:
                if not isinstance(min_score, int | float) or isinstance(min_score, bool):
                    errors.append("presidio.min_score must be a number between 0 and 1")
                elif not 0.0 <= float(min_score) <= 1.0:
                    errors.append(f"presidio.min_score must be between 0 and 1, got {min_score}")
            entities = presidio.get("entities")
            if entities is not None and (
                not isinstance(entities, list)
                or not all(isinstance(e, str) and e for e in entities)
            ):
                errors.append("presidio.entities must be a non-empty list of strings")

    disabled = raw.get("disabled_rules", [])
    if not isinstance(disabled, list) or not all(isinstance(r, str) for r in disabled):
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
                    f"rule_actions[{rule!r}] = {action!r}; valid actions: {sorted(_VALID_ACTIONS)}"
                )

    return errors
