"""Eval data model: cases and golden sets.

A ``GoldenSet`` is a YAML file:

    name: basic
    description: smoke-level checks against a small set of factual /
      reasoning / summarization prompts.
    cases:
      - id: capital_france
        category: factual
        prompt: What is the capital of France?
        expected: |
          Answer should clearly state "Paris" as the capital.
      - id: quicksort_complexity
        category: cs_factual
        prompt: What's the average time complexity of quicksort?
        expected: |
          Should mention O(n log n) as the average case.

The ``expected`` field is a free-text **rubric**, not a literal answer
to match. That's the whole point of LLM-as-judge: humans describe what
"correct" looks like; the judge model decides whether the candidate's
response satisfies the rubric. Brittle exact-match scoring is a
common pitfall in LLM eval — this design avoids it on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One (prompt, expected behavior) pair.

    ``category`` and ``tags`` exist to slice aggregate scores — you
    can see at a glance "factual: 0.92, summarization: 0.71" rather
    than just a single mean across mixed-difficulty cases.
    """

    id: str
    prompt: str
    expected: str
    category: str = "uncategorized"
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GoldenSet:
    """A versioned, human-editable collection of eval cases."""

    name: str
    cases: list[EvalCase]
    description: str = ""

    def __len__(self) -> int:
        return len(self.cases)


def load_golden_set(path: Path | str) -> GoldenSet:
    """Parse a YAML golden-set file.

    Raises ``ValueError`` with a clear message on schema problems —
    catching these at load time (rather than mid-run) is what makes
    the harness usable in CI."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"golden set not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping, got {type(raw).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{p}: missing or invalid 'name'")

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError(f"{p}: 'cases' must be a non-empty list")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for i, c in enumerate(cases_raw):
        if not isinstance(c, dict):
            raise ValueError(f"{p}: cases[{i}] must be a mapping, got {type(c).__name__}")
        cid = c.get("id")
        prompt = c.get("prompt")
        expected = c.get("expected")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{p}: cases[{i}] missing 'id'")
        if cid in seen_ids:
            raise ValueError(f"{p}: duplicate case id {cid!r}")
        seen_ids.add(cid)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{p}: case {cid!r} missing 'prompt'")
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError(f"{p}: case {cid!r} missing 'expected' rubric")

        tags_raw = c.get("tags", [])
        if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
            raise ValueError(f"{p}: case {cid!r} 'tags' must be a list of strings")

        cases.append(
            EvalCase(
                id=cid,
                prompt=prompt,
                expected=expected,
                category=c.get("category", "uncategorized"),
                tags=tags_raw,
            )
        )

    return GoldenSet(
        name=name,
        cases=cases,
        description=raw.get("description", ""),
    )
