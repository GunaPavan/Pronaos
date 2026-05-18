"""Evaluation harness — systematic scoring of model responses against
human-authored "expected behavior" rubrics.

The setup is gateway-native: instead of a separate eval framework you'd
add later, the eval runner calls the running gateway through its public
API. Every feature that touches a response — routing, caching,
guardrails — gets evaluated end-to-end automatically, because the eval
runs through the same pipeline production traffic does.

Three building blocks:

- ``EvalCase`` — one (prompt, expected behavior) pair plus optional
  category / tags for slicing aggregate scores.
- ``GoldenSet`` — a YAML file of cases. Human-editable, version-
  controlled, diffable.
- ``LLMJudgeScorer`` — uses a separately-configured model to grade
  the candidate model's response 0.0-1.0 against the expected behavior.
  The judge model is a hyperparameter — you can A/B different judges
  to check that your scoring is robust.

The data model is intentionally narrow: we score one response against
one rubric. No multi-turn conversation evaluation, no tool-use scoring,
no embedding-similarity matching. Those are valuable but each could
swallow a whole evaluation framework on their own. Start narrow; layer.
"""

from pronaos.eval.data import EvalCase, GoldenSet, load_golden_set
from pronaos.eval.scorer import LLMJudgeScorer, Scorer, ScoreResult

__all__ = [
    "EvalCase",
    "GoldenSet",
    "LLMJudgeScorer",
    "ScoreResult",
    "Scorer",
    "load_golden_set",
]
