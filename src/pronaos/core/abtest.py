"""A/B testing harness — per-team routing experiments with statistical-significance reporting.

Phase 29.

Why this lives in ``core/`` and not ``api/v1/``: the bucketing logic
needs to be deterministic and tested independently of FastAPI plumbing.
The chat handler reads the team's active config (loaded onto the
Principal at auth time), calls :func:`resolve_arm`, and substitutes
the request's model when an arm fires.

Determinism is important: a logical client request that retries (or
fails the preflight and resubmits) must land in the **same** arm or
the per-call attribution gets noisy. We bucket by
``sha256(team_id || ab_test_id || request_id)`` so the assignment is
stable across retries of the same request_id, while still being
uniformly distributed across requests.

Aggregation lives in :mod:`pronaos.core.abtest_stats` — separated so
the bucketing module can be imported in the hot request path without
pulling in scipy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ABArm:
    """One arm of an A/B test. Substitutes the request's model when its
    bucket fires."""

    model: str
    weight: float


@dataclass(frozen=True, slots=True)
class ABTest:
    """Parsed view of the JSON stored in ``Team.ab_test``.

    ``id`` is the test's unique identifier (UUID hex). ``started_at``
    is iso8601 UTC. Weights are normalised to sum to 1.0 at parse time
    so the bucketing math doesn't need a divide step on the hot path.
    """

    id: str
    name: str
    started_at: str
    arm_a: ABArm
    arm_b: ABArm

    @property
    def covers(self) -> frozenset[str]:
        """Model fqmns the A/B test substitutes between.

        The chat handler checks the requested model against this set
        before substituting — an A/B test between haiku and sonnet
        shouldn't touch a request for Groq llama-8B.
        """
        return frozenset({self.arm_a.model, self.arm_b.model})


def parse_ab_test(raw: dict[str, Any] | None) -> ABTest | None:
    """Parse the JSON column into an ``ABTest`` or return None.

    The JSON shape is owned by the CLI/admin endpoint that wrote it,
    so we don't bother with a Pydantic model here — the JSON is opaque
    to the DB and the gateway just needs the bucketing fields. Any
    malformed JSON falls back to "no active test" rather than 500ing
    the request — A/B should never break the gateway.
    """
    if not isinstance(raw, dict):
        return None
    try:
        test_id = str(raw["id"])
        name = str(raw["name"])
        started_at = str(raw["started_at"])
        arm_a_raw = raw["arm_a"]
        arm_b_raw = raw["arm_b"]
        if not (isinstance(arm_a_raw, dict) and isinstance(arm_b_raw, dict)):
            return None
        arm_a_w = float(arm_a_raw["weight"])
        arm_b_w = float(arm_b_raw["weight"])
        total = arm_a_w + arm_b_w
        if total <= 0:
            return None
        # Normalise so the bucketing math sees probabilities, not raw
        # weights — an operator who set 80/40 instead of 80/20 (typo)
        # still gets a sensible 2:1 split rather than a 1.2 sum that
        # would put 20% of requests into "neither arm" purgatory.
        arm_a = ABArm(model=str(arm_a_raw["model"]), weight=arm_a_w / total)
        arm_b = ABArm(model=str(arm_b_raw["model"]), weight=arm_b_w / total)
    except (KeyError, TypeError, ValueError):
        return None
    return ABTest(id=test_id, name=name, started_at=started_at, arm_a=arm_a, arm_b=arm_b)


def resolve_arm(
    *,
    test: ABTest,
    team_id: str,
    request_id: str,
) -> tuple[str, str]:
    """Bucket ``request_id`` into one of the two arms.

    Returns ``(arm_letter, model_fqmn)`` where ``arm_letter`` is
    ``"a"`` or ``"b"``. The split is deterministic in ``request_id`` —
    retries of the same logical request land in the same arm so the
    per-call attribution stays clean.

    The hash mixes in ``team_id`` and the test's ``id`` so the same
    ``request_id`` lands in different arms in different tests (avoids
    the pathological case where one bad request_id pinned to arm A
    poisons every test the team ever runs).
    """
    digest = hashlib.sha256(f"{team_id}:{test.id}:{request_id}".encode()).hexdigest()
    # First 8 hex chars → uint32 → divide by 2^32 to get a uniform
    # fraction in [0, 1). 8 chars (32 bits) is more than enough
    # precision for a weight that's a float; 4 chars would be enough
    # for percentage-level weights but 8 chars costs nothing and keeps
    # the math obvious.
    fraction = int(digest[:8], 16) / 0x100000000
    if fraction < test.arm_a.weight:
        return ("a", test.arm_a.model)
    return ("b", test.arm_b.model)


def should_apply(test: ABTest, requested_model: str) -> bool:
    """True if the request's model is one of the test's arms.

    An A/B test only fires when the client is asking for one of the
    arms — testing haiku-vs-sonnet shouldn't redirect a Groq call.
    Allows multiple A/B tests per gateway (one per team) without
    collisions and gives the operator predictable behaviour: their
    apps that pin a model unrelated to the test continue working
    untouched.
    """
    return requested_model in test.covers
