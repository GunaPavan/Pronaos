"""Concrete guardrail rules.

Regex-based detectors only — fast (microseconds per scan), deterministic,
and zero new deps. The trade-off is recall: regex misses unstructured PII
(names, addresses) and has occasional false positives on PII-shaped strings
that aren't actually sensitive ("123-45-6789" in a code example).

Phase 8.2 plan: layer a small Presidio classifier on top for the long tail
of names/addresses. The interface is unchanged — same Protocol, just a
different rule registered.

Patterns are deliberately conservative — better to miss a few than to
spuriously redact code, URLs, or technical references. Tighter recall
needs the classifier, not bigger regexes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from pronaos.guardrails.base import GuardrailAction, GuardrailRule, RuleHit

# --------------------------------------------------------------------------- #
# PII rules                                                                   #
# --------------------------------------------------------------------------- #


# Email: standard local@domain shape. Doesn't try to enforce RFC 5321 exactly
# (the full grammar is a horror story); covers the 99.9% case.
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# US-style phone: optional +1 country code, area code, exchange, line, with
# common separators. Won't match every international format — those go on
# a per-tenant rule shortlist later.
_PHONE_RE: Final = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")

# US SSN: NNN-NN-NNNN. Real SSNs have allocation rules (e.g. area number
# never 000 or 666); the regex doesn't enforce them — false positives on
# obviously-invalid strings are acceptable because the action is REDACT,
# not BLOCK.
_SSN_RE: Final = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# IPv4: classic dotted quad. IPv6 deferred — the regex is hairy and v6
# leaks in user prompts are much rarer than v4.
_IPV4_RE: Final = re.compile(
    r"\b(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}\b"
)

# Credit-card: digits with optional spaces or dashes, 13-19 long. We
# Luhn-check inside the rule so test-card-looking sequences (cards in
# code samples, dummy numbers) don't trigger.
_CC_CANDIDATE_RE: Final = re.compile(r"\b(?:\d[ \-]?){13,19}\d\b")


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum — the algorithm every PAN-validation
    library uses. Rejects obviously-fake credit-card patterns."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class RegexPIIDetector(GuardrailRule):
    """One detector instance covers one PII category.

    Why one-rule-per-category instead of one mega-rule: each category
    has its own metric label (so dashboards can split "emails redacted"
    from "SSNs redacted"), its own replacement token, and potentially
    its own policy (e.g. "redact emails, block SSN").
    """

    def __init__(
        self,
        *,
        name: str,
        pattern: re.Pattern[str],
        replacement: str,
        default_action: GuardrailAction = GuardrailAction.REDACT,
        post_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._name = name
        self._pattern = pattern
        self._replacement = replacement
        self._default_action = default_action
        # ``post_filter`` lets the credit-card detector reject Luhn-failing
        # candidates without baking that logic into the regex. The filter
        # receives the matched digits-only string.
        self._post_filter = post_filter

    @property
    def name(self) -> str:
        return self._name

    @property
    def default_action(self) -> GuardrailAction:
        return self._default_action

    def scan(self, text: str) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for m in self._pattern.finditer(text):
            matched = m.group(0)
            if self._post_filter is not None:
                digits = re.sub(r"\D", "", matched)
                if not self._post_filter(digits):
                    continue
            hits.append(
                RuleHit(
                    rule=self._name,
                    span=(m.start(), m.end()),
                    matched_text=matched,
                    replacement_token=self._replacement,
                )
            )
        return hits


# Factory helpers — one canonical instance per category so the rest of
# the codebase imports the rule, not the pattern.


def email_detector() -> RegexPIIDetector:
    return RegexPIIDetector(name="pii.email", pattern=_EMAIL_RE, replacement="[REDACTED-EMAIL]")


def phone_detector() -> RegexPIIDetector:
    return RegexPIIDetector(name="pii.phone", pattern=_PHONE_RE, replacement="[REDACTED-PHONE]")


def ssn_detector() -> RegexPIIDetector:
    return RegexPIIDetector(name="pii.ssn", pattern=_SSN_RE, replacement="[REDACTED-SSN]")


def ipv4_detector() -> RegexPIIDetector:
    return RegexPIIDetector(name="pii.ipv4", pattern=_IPV4_RE, replacement="[REDACTED-IP]")


def credit_card_detector() -> RegexPIIDetector:
    return RegexPIIDetector(
        name="pii.credit_card",
        pattern=_CC_CANDIDATE_RE,
        replacement="[REDACTED-CC]",
        post_filter=_luhn_ok,
    )


def default_pii_detectors() -> list[GuardrailRule]:
    """The set of PII detectors that ship enabled by default."""
    return [
        email_detector(),
        phone_detector(),
        ssn_detector(),
        credit_card_detector(),
        ipv4_detector(),
    ]


# --------------------------------------------------------------------------- #
# Prompt injection                                                            #
# --------------------------------------------------------------------------- #


# Pattern set covering known jailbreak preambles. Case-insensitive,
# multiline. Sources: published prompt-injection taxonomies (Greshake et al.
# 2023, OWASP LLM Top 10). Detection here is heuristic only — defaults to
# LOG_ONLY because legitimate prompts about prompt-injection theory get
# spuriously flagged.
_INJECTION_PATTERNS: Final = [
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions",
    r"forget\s+(?:everything|all)\s+(?:above|before)",
    r"you\s+are\s+now\s+(?:a|an)\s+\w+",  # role override attempts
    r"system\s*:\s*",  # naked role marker injection
    r"###\s*new\s+instructions",
    r"<\s*\|im_start\s*\|>",  # ChatML role marker leak
    r"BEGIN\s+JAILBREAK",
    r"do\s+anything\s+now",  # DAN-family preambles
]


class PromptInjectionDetector(GuardrailRule):
    """Heuristic prompt-injection scanner.

    Defaults to LOG_ONLY because false-positive risk on legitimate
    queries about prompt engineering / red-teaming / safety research
    is high. Tenants with strict policies can flip to BLOCK via
    per-tenant override.
    """

    _NAME = "injection"

    def __init__(self) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _INJECTION_PATTERNS]

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def default_action(self) -> GuardrailAction:
        return GuardrailAction.LOG_ONLY

    def scan(self, text: str) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for pat in self._patterns:
            for m in pat.finditer(text):
                hits.append(
                    RuleHit(
                        rule=self._NAME,
                        span=(m.start(), m.end()),
                        matched_text=m.group(0),
                        # If a tenant flips this to REDACT, mask the
                        # entire matched phrase with a generic marker
                        # rather than something that could itself be
                        # interpreted as an instruction.
                        replacement_token="[REDACTED-INJECTION]",  # noqa: S106 — redaction marker, not a credential
                    )
                )
        return hits
