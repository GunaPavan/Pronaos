"""PII coverage experiment (Phase 22).

Question: how many PII items does the ML detector (Presidio) catch
that the regex detectors miss? This is the empirical answer to "is
Presidio worth its install weight?"

Method
------
For each prompt in the PII coverage golden set:
  1. Send it through the gateway with Presidio **disabled** at the
     team-policy level. Capture the ``X-Pronaos-Guardrails`` response
     header — that reports every rule that fired.
  2. Toggle the team policy to **enable** Presidio.
  3. Send the same prompt again. Capture the new header.
  4. Compare: which rules fired only in the regex+Presidio run?

Output
------
Per-case breakdown of which detectors fired in each mode, plus a
summary table:
- Prompts where ONLY regex fired       → "regex covered"
- Prompts where ONLY Presidio fired    → "presidio-exclusive catches"
- Prompts where BOTH fired             → "overlapping coverage"
- Prompts where NEITHER fired          → "uncovered (false negative)"

The headline number is **presidio-exclusive catches** — the cases
that *would have leaked* without the ML detector.

Why header-based detection (not body parsing)
--------------------------------------------
The chat response body is the model's reply, which we don't need.
The ``X-Pronaos-Guardrails`` header is set by the chat handler
right after the ingress scan and lists every rule that fired (e.g.
``redacted:pii.email,presidio.PERSON``). Reading the header is
zero-cost and avoids us having to ask the upstream model anything
about the PII we just sent it.

Requirements
------------
- Gateway running with ``PRONAOS_PRESIDIO_ENABLED=true`` (the
  per-team policy then controls whether it's used per request).
- An API key with both ``chat:write`` AND ``admin:usage`` scopes,
  for the team you want to test. The script toggles
  ``team.guardrail_policy.presidio.enabled`` between runs to
  control Presidio activation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pronaos.eval.data import EvalCase, load_golden_set

# --------------------------------------------------------------------------- #
# Per-case result                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaseObservation:
    case_id: str
    category: str
    rules_fired: set[str] = field(default_factory=set)
    http_status: int = 0
    error: str | None = None


@dataclass(slots=True)
class ModeAgg:
    label: str
    observations: list[CaseObservation] = field(default_factory=list)

    def rules_for(self, case_id: str) -> set[str]:
        for o in self.observations:
            if o.case_id == case_id:
                return o.rules_fired
        return set()


# --------------------------------------------------------------------------- #
# Header parsing                                                              #
# --------------------------------------------------------------------------- #


def _parse_guardrails_header(value: str | None) -> set[str]:
    """``X-Pronaos-Guardrails: redacted:pii.email,presidio.PERSON``
    → ``{"pii.email", "presidio.PERSON"}``.

    Header is absent when no rule fired. ``blocked:<rule>`` shape is
    also recognised (would indicate a BLOCK action).
    """
    if not value:
        return set()
    _, _, rest = value.partition(":")
    if not rest:
        return set()
    return {r.strip() for r in rest.split(",") if r.strip()}


# --------------------------------------------------------------------------- #
# Gateway interaction                                                         #
# --------------------------------------------------------------------------- #


async def _set_presidio_enabled(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    team_id: str,
    enabled: bool,
) -> None:
    """Toggle Presidio for the team via the admin policy endpoint.

    PUT replaces the policy wholesale. We send a minimal shape that
    only carries the Presidio block — anything else previously set on
    the policy will be cleared. This is fine for the experiment because
    we're testing in isolation; a real operator workflow would merge
    rather than replace.
    """
    # The admin endpoint expects the policy fields at the top level
    # (GuardrailPolicyBody), not wrapped under a "policy" key.
    body = {"presidio": {"enabled": enabled}}
    resp = await client.put(
        f"/v1/admin/team/{team_id}/guardrail-policy",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=10.0,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"policy update failed: {resp.status_code} {resp.text[:200]}"
        )


async def _fire_case(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    case: EvalCase,
    model: str,
) -> CaseObservation:
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": case.prompt}],
                "temperature": 0.0,
                "max_tokens": 10,
            },
            timeout=30.0,
        )
    except Exception as e:
        return CaseObservation(
            case_id=case.id,
            category=case.category,
            error=f"network: {e}",
        )
    obs = CaseObservation(
        case_id=case.id, category=case.category, http_status=resp.status_code
    )
    obs.rules_fired = _parse_guardrails_header(
        resp.headers.get("X-Pronaos-Guardrails")
    )
    if resp.status_code >= 400:
        obs.error = f"http {resp.status_code}: {resp.text[:160]}"
    return obs


async def _run_mode(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    cases: Sequence[EvalCase],
    model: str,
    label: str,
) -> ModeAgg:
    print(f"\n[{label}] running {len(cases)} cases")
    agg = ModeAgg(label=label)
    for i, case in enumerate(cases, 1):
        obs = await _fire_case(client, api_key=api_key, case=case, model=model)
        fired = ",".join(sorted(obs.rules_fired)) or "—"
        marker = "ok" if obs.error is None else "ERR"
        print(
            f"  [{i:>2}/{len(cases)}] {obs.case_id:<25} "
            f"fired=[{fired}] {marker}"
        )
        if obs.error:
            print(f"      ! {obs.error}")
        agg.observations.append(obs)
    return agg


# --------------------------------------------------------------------------- #
# Summary                                                                     #
# --------------------------------------------------------------------------- #


def _print_summary(
    regex_only: ModeAgg, regex_plus_ml: ModeAgg, cases: Sequence[EvalCase]
) -> int:
    print("\n" + "=" * 72)
    print("Phase 22 — PII coverage experiment")
    print("=" * 72)

    regex_covered = 0
    presidio_exclusive = 0
    overlapping = 0
    uncovered = 0

    print(
        f"{'case_id':<25}  {'regex_only':<32}  {'regex_plus_ml':<40}"
    )
    print("-" * 110)
    for case in cases:
        r = regex_only.rules_for(case.id)
        rml = regex_plus_ml.rules_for(case.id)

        only_ml = rml - r
        # _only_regex = r - rml — kept here for future asymmetry analysis;
        # not used by the current report layout.
        _only_regex = r - rml

        if r and not only_ml:
            regex_covered += 1
        elif only_ml and not r:
            presidio_exclusive += 1
        elif r and only_ml:
            overlapping += 1
        elif not r and not rml:
            uncovered += 1

        print(
            f"{case.id:<25}  {(','.join(sorted(r)) or '—'):<32}  "
            f"{(','.join(sorted(rml)) or '—'):<40}"
        )

    print()
    print(
        f"regex-covered cases:        {regex_covered:>3}  "
        "(regex alone caught these)"
    )
    print(
        f"presidio-exclusive catches: {presidio_exclusive:>3}  "
        "(ONLY caught with ML — would have leaked without Presidio)"
    )
    print(
        f"overlapping coverage:       {overlapping:>3}  "
        "(both fired)"
    )
    print(
        f"uncovered (FN):             {uncovered:>3}  "
        "(neither fired — recall gap)"
    )

    total = len(cases) - uncovered
    if total > 0:
        ml_share = (presidio_exclusive + overlapping) / total * 100.0
        print(
            f"\nPresidio contributed to {presidio_exclusive + overlapping}/{total} "
            f"covered cases ({ml_share:.1f}%)."
        )
    if presidio_exclusive > 0:
        print(
            f"VERDICT: claim holds — Presidio caught {presidio_exclusive} "
            "PII case(s) regex missed entirely."
        )
        return 0
    print(
        "VERDICT: claim does not hold — Presidio added no new catches "
        "beyond what regex already covered."
    )
    return 1


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8123",
        help="Pronaos gateway base URL.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help=(
            "Pronaos API key with both 'chat:write' AND 'admin:usage' "
            "scopes for the target team."
        ),
    )
    parser.add_argument(
        "--team-id",
        required=True,
        help="Team id whose guardrail_policy will be toggled between runs.",
    )
    parser.add_argument(
        "--golden-set",
        default="tests/eval/data/pii_coverage.yaml",
        help="Path to the PII coverage golden-set YAML file.",
    )
    parser.add_argument(
        "--model",
        default="groq/llama-3.1-8b-instant",
        help=(
            "Concrete model for the chat request. The model's response "
            "doesn't matter — we just need a successful round-trip so "
            "the guardrail scan runs."
        ),
    )
    args = parser.parse_args()

    cases = list(load_golden_set(Path(args.golden_set)).cases)
    if not cases:
        print("no cases in golden set — refusing to run.", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=args.gateway_url) as client:
        # Run 1: Presidio disabled.
        try:
            await _set_presidio_enabled(
                client, api_key=args.api_key, team_id=args.team_id, enabled=False
            )
        except RuntimeError as e:
            print(f"failed to disable Presidio for run 1: {e}", file=sys.stderr)
            return 2
        regex_only = await _run_mode(
            client,
            api_key=args.api_key,
            cases=cases,
            model=args.model,
            label="regex_only",
        )

        # Run 2: Presidio enabled.
        try:
            await _set_presidio_enabled(
                client, api_key=args.api_key, team_id=args.team_id, enabled=True
            )
        except RuntimeError as e:
            print(f"failed to enable Presidio for run 2: {e}", file=sys.stderr)
            return 2
        regex_plus_ml = await _run_mode(
            client,
            api_key=args.api_key,
            cases=cases,
            model=args.model,
            label="regex_plus_ml",
        )

        return _print_summary(regex_only, regex_plus_ml, cases)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
