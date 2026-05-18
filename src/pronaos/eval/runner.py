"""Orchestrator: runs a golden set through a candidate model and scores
every response via the configured Scorer.

The runner is concurrency-aware but conservative: it serialises calls by
default. Eval runs are rarely throughput-sensitive (a 50-case set
takes ~5 minutes at sequential ~6s/call). Parallelism would risk hitting
provider rate limits or coloring the judge with noisy-neighbor latency
artifacts in the scoring.

Output shape
------------
A run produces an ``EvalRunSummary`` with:
- per-case ``EvalRow`` (case id, response, score, justification, judge_error)
- aggregate stats (mean / median / pass-rate-at-threshold)
- per-category breakdown so you can see "factual: 0.92, summarization: 0.71"

The summary can be saved to JSON for diffing across runs — that's the
basis for the CI gate (Phase 9.2): "did this PR regress any
category by more than X?".
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pronaos.eval.data import EvalCase, GoldenSet
from pronaos.eval.scorer import Scorer, ScoreResult

# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class EvalRow:
    """One (case, candidate response, judge verdict) row."""

    case_id: str
    category: str
    prompt: str
    candidate_response: str
    score: float
    justification: str
    judge_error: str | None = None
    candidate_error: str | None = None

    @property
    def is_scored(self) -> bool:
        """True iff both the candidate call and the judge call succeeded.
        Failed cases are excluded from aggregate stats."""
        return self.candidate_error is None and self.judge_error is None


@dataclass(slots=True)
class CategorySummary:
    name: str
    count: int
    mean: float
    median: float
    pass_rate: float  # fraction with score >= threshold


@dataclass(slots=True)
class EvalRunSummary:
    """Full result of one eval run. JSON-serializable via ``to_dict``."""

    golden_set: str
    candidate_model: str
    judge_model: str
    pass_threshold: float
    total_cases: int
    scored_cases: int
    candidate_errors: int
    judge_errors: int
    overall_mean: float
    overall_median: float
    overall_pass_rate: float
    categories: list[CategorySummary] = field(default_factory=list)
    rows: list[EvalRow] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Round-trip-friendly JSON shape. Used for saving + diffing."""
        return {
            "golden_set": self.golden_set,
            "candidate_model": self.candidate_model,
            "judge_model": self.judge_model,
            "pass_threshold": self.pass_threshold,
            "total_cases": self.total_cases,
            "scored_cases": self.scored_cases,
            "candidate_errors": self.candidate_errors,
            "judge_errors": self.judge_errors,
            "overall_mean": self.overall_mean,
            "overall_median": self.overall_median,
            "overall_pass_rate": self.overall_pass_rate,
            "duration_seconds": self.duration_seconds,
            "categories": [asdict(c) for c in self.categories],
            "rows": [asdict(r) for r in self.rows],
        }

    def save_json(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #


class EvalRunner:
    """Drives an eval run end-to-end.

    The candidate call goes through the same OpenAI-shape endpoint as
    production traffic (typically the gateway itself); the judge call
    goes through the configured ``Scorer``. Caller chooses both — the
    runner is agnostic.
    """

    def __init__(
        self,
        *,
        candidate_base_url: str,
        candidate_api_key: str,
        candidate_model: str,
        scorer: Scorer,
        judge_model_id: str,
        pass_threshold: float = 0.7,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = candidate_base_url.rstrip("/")
        self._api_key = candidate_api_key
        self._candidate_model = candidate_model
        self._scorer = scorer
        self._judge_model_id = judge_model_id
        self._pass_threshold = pass_threshold
        self._timeout = timeout_seconds

    # ------------------------------------------------------------------ #
    # Run                                                                #
    # ------------------------------------------------------------------ #

    async def run(self, golden_set: GoldenSet) -> EvalRunSummary:
        start = time.monotonic()
        rows: list[EvalRow] = []

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for case in golden_set.cases:
                row = await self._run_case(client, case)
                rows.append(row)

        duration = time.monotonic() - start
        return self._summarize(golden_set, rows, duration)

    async def _run_case(self, client: httpx.AsyncClient, case: EvalCase) -> EvalRow:
        # Step 1: call the candidate model.
        try:
            resp = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._candidate_model,
                    "messages": [{"role": "user", "content": case.prompt}],
                    "temperature": 0.0,
                    "max_tokens": 400,
                },
            )
        except Exception as e:
            return EvalRow(
                case_id=case.id,
                category=case.category,
                prompt=case.prompt,
                candidate_response="",
                score=0.0,
                justification="",
                candidate_error=f"network: {e}",
            )

        if resp.status_code != 200:
            return EvalRow(
                case_id=case.id,
                category=case.category,
                prompt=case.prompt,
                candidate_response="",
                score=0.0,
                justification="",
                candidate_error=f"http {resp.status_code}: {resp.text[:200]}",
            )

        candidate_text = resp.json()["choices"][0]["message"]["content"]

        # Step 2: score via the judge.
        verdict: ScoreResult = await self._scorer.score(
            prompt=case.prompt, expected=case.expected, candidate=candidate_text
        )

        return EvalRow(
            case_id=case.id,
            category=case.category,
            prompt=case.prompt,
            candidate_response=candidate_text,
            score=verdict.score,
            justification=verdict.justification,
            judge_error=verdict.judge_error,
        )

    # ------------------------------------------------------------------ #
    # Aggregation                                                        #
    # ------------------------------------------------------------------ #

    def _summarize(
        self,
        golden_set: GoldenSet,
        rows: list[EvalRow],
        duration_seconds: float,
    ) -> EvalRunSummary:
        scored = [r for r in rows if r.is_scored]
        scores = [r.score for r in scored]
        candidate_errors = sum(1 for r in rows if r.candidate_error is not None)
        judge_errors = sum(1 for r in rows if r.judge_error is not None)

        return EvalRunSummary(
            golden_set=golden_set.name,
            candidate_model=self._candidate_model,
            judge_model=self._judge_model_id,
            pass_threshold=self._pass_threshold,
            total_cases=len(rows),
            scored_cases=len(scored),
            candidate_errors=candidate_errors,
            judge_errors=judge_errors,
            overall_mean=statistics.mean(scores) if scores else 0.0,
            overall_median=statistics.median(scores) if scores else 0.0,
            overall_pass_rate=_pass_rate(scores, self._pass_threshold),
            categories=_category_summaries(scored, self._pass_threshold),
            rows=rows,
            duration_seconds=duration_seconds,
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _pass_rate(scores: Iterable[float], threshold: float) -> float:
    """Fraction of scores >= threshold. Returns 0.0 for an empty set."""
    scores = list(scores)
    if not scores:
        return 0.0
    return sum(1 for s in scores if s >= threshold) / len(scores)


def _category_summaries(
    rows: list[EvalRow], threshold: float
) -> list[CategorySummary]:
    """Group scored rows by category, computing per-category stats.

    Categories with zero scored rows are skipped — they'd produce
    nonsense stats and would confuse the dashboard reader."""
    by_category: dict[str, list[float]] = {}
    for r in rows:
        by_category.setdefault(r.category, []).append(r.score)

    summaries: list[CategorySummary] = []
    for cat, scores in sorted(by_category.items()):
        if not scores:
            continue
        summaries.append(
            CategorySummary(
                name=cat,
                count=len(scores),
                mean=statistics.mean(scores),
                median=statistics.median(scores),
                pass_rate=_pass_rate(scores, threshold),
            )
        )
    return summaries
