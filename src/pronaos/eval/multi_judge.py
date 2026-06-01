"""Multi-judge eval runner (Phase 23).

Why multi-judge
---------------
A single LLM judge is one opinion. When the judge agrees with itself
(temperature 0), you get *consistency*. When two *different* judges
agree on the same response, you get *robustness* — independent
confirmation that the score reflects the rubric rather than the
judge's own writing style or biases.

This module runs N judges concurrently on the same (prompt, expected,
candidate) triple and reports both their individual scores and the
agreement between them. The headline output is the **inter-judge
agreement rate**: how often the judges land within ε of each other.
Disagreements are the cases a human should inspect — they're where
the rubric is ambiguous or the candidate's response was borderline.

Agreement metrics
-----------------
For each pair of judges A, B over N cases:

- **mean_abs_delta** — average |score_A - score_B|. Lower is better.
- **within_epsilon_rate** — fraction of cases with |Δ| ≤ ε. The headline.
- **cohens_kappa** — chance-corrected binary agreement on pass/fail at
  the pass_threshold. Useful when scores are bimodal (cases mostly
  pass or fail rather than spreading across 0..1).

Cohen's kappa formula:
    κ = (P_o - P_e) / (1 - P_e)
    P_o = observed agreement rate
    P_e = P(both pass) + P(both fail) under independence

κ = 1.0 means perfect agreement, 0.0 means no better than chance,
< 0 means worse than chance (rare, indicates a sign flip).

Why a separate module rather than extending EvalRunner
------------------------------------------------------
The single-judge runner has stable callers (CLI, embedded experiments).
Refactoring its output shape would force every caller to change. The
multi-judge runner produces a richer output shape that includes the
single-judge data as a special case, but it's clearer as its own type.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pronaos.eval.data import EvalCase, GoldenSet
from pronaos.eval.scorer import Scorer

# Default epsilon for "within ε agreement." 0.1 on a 0..1 scale means
# judges are considered to agree when they're within one tenth of a
# point — looser than exact match but tight enough to be meaningful.
DEFAULT_EPSILON: float = 0.1

# Default pass threshold for Cohen's kappa binarization. Matches the
# single-judge runner's default.
DEFAULT_PASS_THRESHOLD: float = 0.7


# --------------------------------------------------------------------------- #
# Per-judge verdict + row                                                     #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class JudgeVerdict:
    """One judge's score for one case."""

    judge_id: str
    score: float
    justification: str = ""
    judge_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.judge_error is None


@dataclass(slots=True)
class MultiJudgeEvalRow:
    """One case + candidate response + N judge verdicts.

    A row is "scored" when the candidate call succeeded AND at least
    one judge produced a valid score. Cases where ALL judges failed
    are excluded from agreement metrics (you can't compute agreement
    with no data).
    """

    case_id: str
    category: str
    prompt: str
    candidate_response: str
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    candidate_error: str | None = None

    @property
    def valid_verdicts(self) -> list[JudgeVerdict]:
        return [v for v in self.verdicts if v.is_valid]

    @property
    def is_fully_scored(self) -> bool:
        """All judges produced valid scores. Required for pairwise agreement."""
        return self.candidate_error is None and all(v.is_valid for v in self.verdicts)


# --------------------------------------------------------------------------- #
# Agreement metrics                                                           #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PairAgreement:
    """Agreement between two specific judges, over the rows where both scored."""

    judge_a: str
    judge_b: str
    n: int  # rows where both judges produced a valid score
    mean_abs_delta: float
    within_epsilon_rate: float
    cohens_kappa: float


def _pair_agreement(
    rows: list[MultiJudgeEvalRow],
    judge_a: str,
    judge_b: str,
    *,
    epsilon: float,
    pass_threshold: float,
) -> PairAgreement:
    """Compute agreement between two judges across all rows where both
    produced a valid score. Rows where either judge errored are dropped
    — counted in ``n``, not in the deltas/kappa.
    """
    deltas: list[float] = []
    a_passes: list[bool] = []
    b_passes: list[bool] = []
    for row in rows:
        if row.candidate_error is not None:
            continue
        va = next((v for v in row.verdicts if v.judge_id == judge_a), None)
        vb = next((v for v in row.verdicts if v.judge_id == judge_b), None)
        if va is None or vb is None or not va.is_valid or not vb.is_valid:
            continue
        deltas.append(abs(va.score - vb.score))
        a_passes.append(va.score >= pass_threshold)
        b_passes.append(vb.score >= pass_threshold)

    n = len(deltas)
    if n == 0:
        return PairAgreement(
            judge_a=judge_a,
            judge_b=judge_b,
            n=0,
            mean_abs_delta=0.0,
            within_epsilon_rate=0.0,
            cohens_kappa=0.0,
        )

    mean_abs = statistics.mean(deltas)
    within_eps = sum(1 for d in deltas if d <= epsilon) / n
    kappa = _cohens_kappa(a_passes, b_passes)
    return PairAgreement(
        judge_a=judge_a,
        judge_b=judge_b,
        n=n,
        mean_abs_delta=mean_abs,
        within_epsilon_rate=within_eps,
        cohens_kappa=kappa,
    )


def _cohens_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's kappa for two parallel boolean sequences.

    Returns 0.0 when either input is empty or when the marginals are
    degenerate (e.g. one judge passes everything). Kappa is undefined
    in those cases; 0.0 is the safe sentinel for "no signal."
    """
    n = len(a)
    if n == 0 or len(b) != n:
        return 0.0
    # Observed agreement.
    p_o = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    # Marginal probabilities → expected agreement under independence.
    a_pass = sum(a) / n
    b_pass = sum(b) / n
    p_e = (a_pass * b_pass) + ((1 - a_pass) * (1 - b_pass))
    if p_e == 1.0:
        # Both judges pass (or both fail) everything → kappa undefined.
        # Convention: report 0.0 rather than NaN so downstream code
        # doesn't have to special-case.
        return 0.0
    return (p_o - p_e) / (1.0 - p_e)


# --------------------------------------------------------------------------- #
# Per-judge stats                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class JudgeStats:
    """Per-judge summary stats — same shape across all judges so a
    Markdown table renderer can do one pass."""

    judge_id: str
    n_scored: int
    mean: float
    median: float
    pass_rate: float


def _judge_stats(
    rows: list[MultiJudgeEvalRow], judge_id: str, *, pass_threshold: float
) -> JudgeStats:
    scores: list[float] = []
    for row in rows:
        v = next(
            (v for v in row.verdicts if v.judge_id == judge_id and v.is_valid),
            None,
        )
        if v is not None:
            scores.append(v.score)
    if not scores:
        return JudgeStats(judge_id=judge_id, n_scored=0, mean=0.0, median=0.0, pass_rate=0.0)
    passing = sum(1 for s in scores if s >= pass_threshold)
    return JudgeStats(
        judge_id=judge_id,
        n_scored=len(scores),
        mean=statistics.mean(scores),
        median=statistics.median(scores),
        pass_rate=passing / len(scores),
    )


# --------------------------------------------------------------------------- #
# Summary                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MultiJudgeEvalSummary:
    """Full result of one multi-judge run. JSON-serializable via ``to_dict``."""

    golden_set: str
    candidate_model: str
    judges: list[str]
    pass_threshold: float
    epsilon: float
    total_cases: int
    candidate_errors: int
    per_judge: list[JudgeStats] = field(default_factory=list)
    pairs: list[PairAgreement] = field(default_factory=list)
    rows: list[MultiJudgeEvalRow] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "golden_set": self.golden_set,
            "candidate_model": self.candidate_model,
            "judges": self.judges,
            "pass_threshold": self.pass_threshold,
            "epsilon": self.epsilon,
            "total_cases": self.total_cases,
            "candidate_errors": self.candidate_errors,
            "duration_seconds": self.duration_seconds,
            "per_judge": [asdict(s) for s in self.per_judge],
            "pairs": [asdict(p) for p in self.pairs],
            "rows": [
                {
                    "case_id": r.case_id,
                    "category": r.category,
                    "prompt": r.prompt,
                    "candidate_response": r.candidate_response,
                    "candidate_error": r.candidate_error,
                    "verdicts": [asdict(v) for v in r.verdicts],
                }
                for r in self.rows
            ],
        }

    def save_json(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #


class MultiJudgeRunner:
    """Drives a multi-judge eval run end-to-end.

    The candidate is called once per case; the result is then handed to
    every configured judge **concurrently** (asyncio.gather). Concurrency
    is safe here because the judges are independent and live on
    separate provider quotas — there's no shared state to race over.

    The candidate call is intentionally serial: rate limits live on the
    candidate's provider, and eval runs don't benefit from parallelism
    enough to justify the cost in upstream pressure.
    """

    def __init__(
        self,
        *,
        candidate_base_url: str,
        candidate_api_key: str,
        candidate_model: str,
        scorers: Sequence[tuple[str, Scorer]],
        pass_threshold: float = DEFAULT_PASS_THRESHOLD,
        epsilon: float = DEFAULT_EPSILON,
        timeout_seconds: float = 30.0,
    ) -> None:
        if len(scorers) < 2:
            raise ValueError(
                "MultiJudgeRunner requires at least 2 scorers; "
                f"got {len(scorers)}. For single-judge use EvalRunner."
            )
        seen: set[str] = set()
        for judge_id, _ in scorers:
            if judge_id in seen:
                raise ValueError(f"duplicate judge_id: {judge_id!r}")
            seen.add(judge_id)
        self._base_url = candidate_base_url.rstrip("/")
        self._api_key = candidate_api_key
        self._candidate_model = candidate_model
        self._scorers = scorers
        self._pass_threshold = pass_threshold
        self._epsilon = epsilon
        self._timeout = timeout_seconds

    async def run(self, golden_set: GoldenSet) -> MultiJudgeEvalSummary:
        start = time.monotonic()
        rows: list[MultiJudgeEvalRow] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for case in golden_set.cases:
                row = await self._run_case(client, case)
                rows.append(row)
        duration = time.monotonic() - start
        return self._summarize(golden_set, rows, duration)

    async def _run_case(self, client: httpx.AsyncClient, case: EvalCase) -> MultiJudgeEvalRow:
        # Step 1: call the candidate model once.
        candidate_text, candidate_error = await self._call_candidate(client, case)
        if candidate_error is not None:
            return MultiJudgeEvalRow(
                case_id=case.id,
                category=case.category,
                prompt=case.prompt,
                candidate_response="",
                candidate_error=candidate_error,
            )

        # Step 2: hand the response to every judge concurrently. The
        # judges are independent; gather is safe.
        verdicts = await asyncio.gather(
            *(
                self._score_with(judge_id, scorer, case, candidate_text)
                for judge_id, scorer in self._scorers
            )
        )
        return MultiJudgeEvalRow(
            case_id=case.id,
            category=case.category,
            prompt=case.prompt,
            candidate_response=candidate_text,
            verdicts=list(verdicts),
        )

    async def _call_candidate(
        self, client: httpx.AsyncClient, case: EvalCase
    ) -> tuple[str, str | None]:
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
            return "", f"network: {e}"
        if resp.status_code != 200:
            return "", f"http {resp.status_code}: {resp.text[:200]}"
        return resp.json()["choices"][0]["message"]["content"], None

    async def _score_with(
        self,
        judge_id: str,
        scorer: Scorer,
        case: EvalCase,
        candidate_text: str,
    ) -> JudgeVerdict:
        result = await scorer.score(
            prompt=case.prompt, expected=case.expected, candidate=candidate_text
        )
        return JudgeVerdict(
            judge_id=judge_id,
            score=result.score,
            justification=result.justification,
            judge_error=result.judge_error,
        )

    # ------------------------------------------------------------------ #
    # Aggregation                                                        #
    # ------------------------------------------------------------------ #

    def _summarize(
        self,
        golden_set: GoldenSet,
        rows: list[MultiJudgeEvalRow],
        duration_seconds: float,
    ) -> MultiJudgeEvalSummary:
        judge_ids = [jid for jid, _ in self._scorers]
        per_judge = [
            _judge_stats(rows, jid, pass_threshold=self._pass_threshold) for jid in judge_ids
        ]
        # All unordered pairs (a, b) with a != b.
        pairs: list[PairAgreement] = []
        for i, a in enumerate(judge_ids):
            for b in judge_ids[i + 1 :]:
                pairs.append(
                    _pair_agreement(
                        rows,
                        a,
                        b,
                        epsilon=self._epsilon,
                        pass_threshold=self._pass_threshold,
                    )
                )
        candidate_errors = sum(1 for r in rows if r.candidate_error is not None)
        return MultiJudgeEvalSummary(
            golden_set=golden_set.name,
            candidate_model=self._candidate_model,
            judges=judge_ids,
            pass_threshold=self._pass_threshold,
            epsilon=self._epsilon,
            total_cases=len(rows),
            candidate_errors=candidate_errors,
            per_judge=per_judge,
            pairs=pairs,
            rows=rows,
            duration_seconds=duration_seconds,
        )
