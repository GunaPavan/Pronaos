"""Llama Guard ML-based jailbreak / unsafe-content classifier (Phase 44).

Why a separate classifier
-------------------------
Phase 8 ships a regex-based prompt-injection detector. It catches the
canonical jailbreak templates ("ignore previous instructions",
"you are now DAN", etc.) but misses:

- **Novel jailbreak phrasings** that weren't in the regex set
- **Multi-turn jailbreaks** building up over conversation
- **Role-play attacks** that don't use canonical wording
- **Indirect attacks** ("a friend asked me how to ...")
- **Content categories beyond injection** — violence, hate, sexual
  content, illegal-activity solicitation that a generic gateway
  shouldn't be forwarding to upstream LLMs

Meta's Llama Guard is a purpose-trained safety classifier. It outputs
a categorical verdict (``safe`` / ``unsafe`` + one of 14 standardised
content categories ``S1..S14``) on every prompt. Pronaos ships an
adapter that calls Llama Guard via the existing Groq endpoint (no
additional provider key needed) and decides BLOCK / LOG_ONLY at the
gateway edge before the prompt reaches the user's chosen upstream.

Why NOT a sync GuardrailRule
----------------------------
Existing detectors (regex, Presidio) are synchronous — they run in
the request thread and complete in microseconds. Llama Guard is a
network call (~200-500 ms on Groq). Implementing the sync
``GuardrailRule.scan()`` interface would block the event loop on
every request. So Llama Guard runs as a **separate async pre-check**
in the chat handler, in front of the regex/Presidio engine.

Per-team policy
---------------
Same shape as Presidio (Phase 22):

    team.guardrail_policy = {
        "llama_guard": {
            "enabled": true,
            "model": "groq/meta-llama/llama-guard-4-12b",
            "default_action": "block"
        }
    }

Default action is BLOCK because Llama Guard is the **intentional**
safety layer — false-positives are explicitly the design choice
(better to refuse a borderline prompt than to forward a true unsafe
one). Operators on permissive workloads (red-teaming, research) can
flip to LOG_ONLY per-team.

Fail-open
---------
Network errors, model unavailable, parse failures — all fail open
(the gateway continues with the existing regex/Presidio guardrails).
The trade-off is conscious: blocking the gateway on a Llama Guard
outage would be worse than briefly losing the ML safety layer. We
log + metric every fail-open so SREs can see when it's happening.

The OTel ``gen_ai.system`` for the classification span is
``meta.llama-guard`` (Phase 43 conventions).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx

from pronaos.guardrails.base import GuardrailAction
from pronaos.logging import get_logger

log = get_logger(__name__)

# Llama Guard 3 + Llama Guard 4 use the same hazard taxonomy (S1-S14).
# Names are the canonical Meta safety categories. We expose them as
# rule-name suffixes so per-team policy can target a single category
# (e.g. allow S13 for an elections-research workload) without disabling
# the whole classifier.
_CATEGORY_NAMES: Final[dict[str, str]] = {
    # S0 is a Pronaos-specific category for PromptGuard 2's binary
    # jailbreak/injection signal — when the upstream model only
    # produces a float score (not a categorical Sn verdict), we map
    # the unsafe verdict to S0. Llama Guard's spec doesn't define S0.
    "S0": "prompt_injection",
    "S1": "violent_crimes",
    "S2": "non_violent_crimes",
    "S3": "sex_related_crimes",
    "S4": "child_sexual_exploitation",
    "S5": "defamation",
    "S6": "specialized_advice",
    "S7": "privacy",
    "S8": "intellectual_property",
    "S9": "indiscriminate_weapons",
    "S10": "hate",
    "S11": "suicide_self_harm",
    "S12": "sexual_content",
    "S13": "elections",
    "S14": "code_interpreter_abuse",
}

# Default model — Llama PromptGuard 2 (86M params) via Groq. This
# replaces the now-decommissioned Llama Guard 4 12B that Groq retired
# in mid-2026. PromptGuard 2 has a different output shape than
# Llama Guard:
#   - Llama Guard 3 / 4: ``safe`` or ``unsafe\nSn[,Sn...]`` with the
#     14 hazard categories (S1..S14).
#   - PromptGuard 2: a single float in [0, 1] representing the
#     probability the input is a jailbreak / prompt injection.
#
# The parser in this module accepts BOTH shapes: numeric scores are
# treated as unsafe when the score crosses ``PROMPT_GUARD_THRESHOLD``,
# with a generic ``S0_prompt_injection`` category surfaced. Operators
# preferring the hazard-category taxonomy can override the model
# per-team if Groq adds a Llama Guard successor to the catalog
# (or point at a non-Groq Llama Guard endpoint).
DEFAULT_LLAMA_GUARD_MODEL: Final = "groq/meta-llama/llama-prompt-guard-2-86m"

# Threshold above which a PromptGuard 2 score counts as unsafe.
# 0.5 is the natural decision boundary; operators on workloads with
# different false-positive tolerances can override per-team in a
# future setting (not exposed in Phase 44).
PROMPT_GUARD_THRESHOLD: Final = 0.5

# Default rule name prefix. Hits surface as ``llama_guard.violent_crimes``
# etc. so dashboards + per-team policy can filter at the category level.
RULE_NAME_PREFIX: Final = "llama_guard"


@dataclass(frozen=True, slots=True)
class LlamaGuardVerdict:
    """One classification call's result.

    ``safe`` is the headline — if False, ``categories`` lists every
    Sn hazard category Llama Guard flagged (often a single one, but
    Llama Guard 4 can return multiple).

    ``rule_names`` is the per-category dotted name suitable for
    dashboards/metrics: ``llama_guard.violent_crimes``,
    ``llama_guard.hate``, etc. Empty when ``safe``.

    ``raw_response`` keeps Llama Guard's literal output for audit /
    debug. Should never be logged at default level (might contain
    fragments of the user's input).
    """

    safe: bool
    categories: tuple[str, ...]  # ("S1", "S10", ...)
    rule_names: tuple[str, ...]  # ("llama_guard.violent_crimes", ...)
    raw_response: str
    classifier_failed: bool = False  # True on network/parse error; safe=True (fail-open)


class LlamaGuardClassifier:
    """Async Llama Guard classifier.

    One instance per gateway process. Internally reuses an
    ``httpx.AsyncClient`` so connection pooling works for the hot
    path; the same client survives across all requests until
    ``aclose()`` is called.

    ``base_url`` defaults to Groq's OpenAI-compat endpoint. The
    adapter speaks OpenAI chat-completions shape so we don't need
    a custom provider — Groq treats Llama Guard like any other
    chat model, and the prompt format is what makes it a classifier.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_LLAMA_GUARD_MODEL,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
        default_action: GuardrailAction = GuardrailAction.BLOCK,
    ) -> None:
        if not api_key:
            raise ValueError("llama_guard: api_key required")
        self._api_key = api_key
        self._model_full = model
        self._model_short = model.removeprefix("groq/")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._default_action = default_action
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def default_action(self) -> GuardrailAction:
        return self._default_action

    @property
    def model(self) -> str:
        return self._model_full

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def classify(self, prompt: str) -> LlamaGuardVerdict:
        """Classify ``prompt`` as safe / unsafe via Llama Guard.

        Returns ``LlamaGuardVerdict(safe=True)`` on:
            - Empty / whitespace-only prompt (nothing to classify)
            - Network error / non-200 response (fail-open + log)
            - Parse error on Llama Guard's output (fail-open + log)

        Sets ``classifier_failed=True`` on the latter two — operators
        can metric on that to detect when Llama Guard is silently
        unhelpful.
        """
        if not prompt or not prompt.strip():
            return LlamaGuardVerdict(safe=True, categories=(), rule_names=(), raw_response="")

        try:
            content = await self._call_upstream(prompt)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning(
                "llama_guard.upstream_error",
                error=str(e),
                model=self._model_full,
            )
            return LlamaGuardVerdict(
                safe=True,
                categories=(),
                rule_names=(),
                raw_response=f"<error: {e!s}>",
                classifier_failed=True,
            )

        verdict = parse_llama_guard_output(content)
        # Log the verdict at info level (the raw response stays out of
        # the log — only the categorical verdict).
        log.info(
            "llama_guard.classified",
            safe=verdict.safe,
            categories=list(verdict.categories),
        )
        return verdict

    async def _call_upstream(self, prompt: str) -> str:
        """Issue the OpenAI-shape chat-completions call to Llama Guard.

        Llama Guard's prompt format is the chat-completions shape with
        a single user message. The model's output is what we parse.
        """
        body = {
            "model": self._model_short,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100,
            "temperature": 0.0,
        }
        resp = await self._http.post(
            f"{self._base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"llama_guard upstream {resp.status_code}: {resp.text[:200]}",
                request=resp.request,
                response=resp,
            )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        return content if isinstance(content, str) else ""


# --------------------------------------------------------------------------- #
# Output parser                                                               #
# --------------------------------------------------------------------------- #


def parse_llama_guard_output(text: str) -> LlamaGuardVerdict:
    """Parse a safety-classifier output into a structured verdict.

    Two output shapes accepted:

    1. **Llama Guard 3 / 4 format** — ``safe`` or ``unsafe\\nSn[,Sn...]``
       where Sn is one of the 14 hazard categories (S1..S14).
    2. **PromptGuard 2 format** — a single float in [0, 1] giving the
       probability the input is a jailbreak / prompt injection. Scores
       >= ``PROMPT_GUARD_THRESHOLD`` (default 0.5) are mapped to
       ``unsafe`` with the generic ``S0`` (prompt_injection) category.

    The parser detects which shape we're looking at by trying the
    float-parse first; if it works AND the value is in [0, 1] we
    treat it as a PromptGuard 2 score. Otherwise we fall through to
    the Llama Guard text parser.

    Anything that doesn't match either shape is treated as safe with
    ``classifier_failed=True`` so the metric can surface "we got a
    response the classifier shouldn't have produced" without blocking
    the gateway.
    """
    if not text:
        return LlamaGuardVerdict(
            safe=True,
            categories=(),
            rule_names=(),
            raw_response="",
            classifier_failed=True,
        )

    stripped = text.strip()

    # Try the PromptGuard 2 numeric-score path first.
    score: float | None
    try:
        score = float(stripped)
    except ValueError:
        score = None
    if score is not None and 0.0 <= score <= 1.0:
        if score >= PROMPT_GUARD_THRESHOLD:
            return LlamaGuardVerdict(
                safe=False,
                categories=("S0",),
                rule_names=(f"{RULE_NAME_PREFIX}.{_CATEGORY_NAMES['S0']}",),
                raw_response=text,
            )
        return LlamaGuardVerdict(
            safe=True,
            categories=(),
            rule_names=(),
            raw_response=text,
        )

    # Llama Guard text format.
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return LlamaGuardVerdict(
            safe=True,
            categories=(),
            rule_names=(),
            raw_response=text,
            classifier_failed=True,
        )

    head = lines[0].lower()
    if head.startswith("safe"):
        return LlamaGuardVerdict(safe=True, categories=(), rule_names=(), raw_response=text)
    if not head.startswith("unsafe"):
        # Something else entirely — defensive fail-safe.
        return LlamaGuardVerdict(
            safe=True,
            categories=(),
            rule_names=(),
            raw_response=text,
            classifier_failed=True,
        )

    # Category tokens. They might be on lines[1] (the canonical
    # format), in lines[0] right after "unsafe", or comma/space-separated.
    raw_categories = " ".join(lines[1:]) if len(lines) > 1 else head[len("unsafe") :]
    categories = _extract_categories(raw_categories)

    rule_names = tuple(
        f"{RULE_NAME_PREFIX}.{_CATEGORY_NAMES[cat]}"
        for cat in categories
        if cat in _CATEGORY_NAMES
    )
    return LlamaGuardVerdict(
        safe=False,
        categories=categories,
        rule_names=rule_names,
        raw_response=text,
    )


def _extract_categories(blob: str) -> tuple[str, ...]:
    """Pull ``S1``..``S14`` tokens out of a comma/space-separated string.

    Returns them in document order, de-duplicated. Unknown tokens
    (``S99``, ``foo``) are silently dropped — Llama Guard sometimes
    emits filler text after the category list.
    """
    out: list[str] = []
    seen: set[str] = set()
    # Walk the string token-by-token. ``re.findall`` would also work
    # but a simple loop is clearer about behaviour on weird inputs.
    for raw in blob.replace(",", " ").split():
        token = raw.strip(".,;:").upper()
        if token in _CATEGORY_NAMES and token not in seen:
            out.append(token)
            seen.add(token)
    return tuple(out)


def is_llama_guard_enabled_for_team(policy: dict[str, object] | None) -> bool:
    """Resolve whether Llama Guard is active for a team's policy.

    Per-team override shape mirrors Presidio (Phase 22):

        {"llama_guard": {"enabled": true, "model": "...", "default_action": "block"}}

    Missing key, missing dict, or ``enabled=false`` all return False.
    """
    if not policy:
        return False
    cfg = policy.get("llama_guard")
    if not isinstance(cfg, dict):
        return False
    enabled = cfg.get("enabled")
    return enabled is True


def llama_guard_team_model(policy: dict[str, object] | None) -> str | None:
    """Extract the team's preferred Llama Guard model, if overridden."""
    if not policy:
        return None
    cfg = policy.get("llama_guard")
    if not isinstance(cfg, dict):
        return None
    model = cfg.get("model")
    return model if isinstance(model, str) and model else None


def llama_guard_team_action(
    policy: dict[str, object] | None,
    *,
    fallback: GuardrailAction = GuardrailAction.BLOCK,
) -> GuardrailAction:
    """Extract the team's preferred default action on unsafe verdict.

    Recognises the policy field ``llama_guard.default_action`` with the
    standard ``GuardrailAction`` enum values. Falls back to ``fallback``
    (default BLOCK) on missing/invalid input.
    """
    if not policy:
        return fallback
    cfg = policy.get("llama_guard")
    if not isinstance(cfg, dict):
        return fallback
    raw = cfg.get("default_action")
    if not isinstance(raw, str):
        return fallback
    try:
        return GuardrailAction(raw.lower())
    except ValueError:
        return fallback
