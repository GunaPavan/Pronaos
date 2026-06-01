"""Unit tests for the A/B bucketing core + stats engine (Phase 29)."""

from __future__ import annotations

from pronaos.core.abtest import (
    ABArm,
    ABTest,
    parse_ab_test,
    resolve_arm,
    should_apply,
)
from pronaos.core.abtest_stats import (
    chi_squared_pass_fail,
    summarise_arm,
    welchs_t_test,
)

# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def test_parse_valid_config() -> None:
    raw = {
        "id": "test-1",
        "name": "haiku-vs-sonnet",
        "started_at": "2026-05-20T18:00:00+00:00",
        "arm_a": {"model": "anthropic/claude-3-5-haiku", "weight": 0.5},
        "arm_b": {"model": "anthropic/claude-3-5-sonnet", "weight": 0.5},
    }
    parsed = parse_ab_test(raw)
    assert parsed is not None
    assert parsed.id == "test-1"
    assert parsed.arm_a.model == "anthropic/claude-3-5-haiku"
    assert parsed.arm_a.weight == 0.5


def test_parse_normalises_unbalanced_weights() -> None:
    """Operator typo: 80 + 40 should still produce a 2:1 split, not
    push 20% of requests into a no-man's-land."""
    raw = {
        "id": "test-2",
        "name": "typo-test",
        "started_at": "2026-05-20T18:00:00+00:00",
        "arm_a": {"model": "groq/model-a", "weight": 80},
        "arm_b": {"model": "groq/model-b", "weight": 40},
    }
    parsed = parse_ab_test(raw)
    assert parsed is not None
    # 80/(80+40) = 0.6666...
    assert abs(parsed.arm_a.weight - (2.0 / 3.0)) < 1e-9
    assert abs(parsed.arm_b.weight - (1.0 / 3.0)) < 1e-9


def test_parse_returns_none_on_garbage() -> None:
    """Malformed JSON should fall back to 'no active test', not raise."""
    assert parse_ab_test(None) is None
    assert parse_ab_test({"id": "x"}) is None  # missing fields
    assert (
        parse_ab_test({"id": "x", "name": "y", "started_at": "z", "arm_a": None, "arm_b": None})
        is None
    )


# --------------------------------------------------------------------------- #
# Bucketing                                                                   #
# --------------------------------------------------------------------------- #


def _test(weight_a: float = 0.5, weight_b: float = 0.5) -> ABTest:
    return ABTest(
        id="test-id",
        name="t",
        started_at="2026-05-20T18:00:00+00:00",
        arm_a=ABArm(model="A", weight=weight_a),
        arm_b=ABArm(model="B", weight=weight_b),
    )


def test_resolve_arm_is_deterministic_in_request_id() -> None:
    """Same request_id → same arm. Critical for retry attribution."""
    t = _test()
    a1 = resolve_arm(test=t, team_id="team-1", request_id="req-A")
    a2 = resolve_arm(test=t, team_id="team-1", request_id="req-A")
    assert a1 == a2


def test_resolve_arm_50_50_split_is_roughly_even() -> None:
    """At 50/50 with N=2000 the empirical split should be close to even
    (within ~5% tolerance — the hash is uniform, so the count is
    binomial with sd = sqrt(N*0.25) = ~22, well under 5% of 1000)."""
    t = _test()
    a = 0
    n = 2000
    for i in range(n):
        arm, _ = resolve_arm(test=t, team_id="team-x", request_id=f"req-{i}")
        if arm == "a":
            a += 1
    # Expect ~1000 in arm A; tolerance 100 (3.3% — well beyond noise).
    assert abs(a - 1000) < 100, f"split too uneven: a={a}, b={n - a}"


def test_resolve_arm_respects_weights_at_80_20() -> None:
    """80/20 weight should land ~80% of requests in arm A."""
    t = _test(weight_a=0.8, weight_b=0.2)
    a = 0
    n = 2000
    for i in range(n):
        arm, _ = resolve_arm(test=t, team_id="team-y", request_id=f"req-{i}")
        if arm == "a":
            a += 1
    # Expect ~1600 in arm A; tolerance ~100 (2-sigma).
    assert abs(a - 1600) < 120, f"expected ~1600 in arm A, got {a}"


def test_resolve_arm_independent_across_tests() -> None:
    """Same request_id in different tests must not always land in the
    same arm — guards against a pathological request_id pinning every
    test to one arm."""
    t1 = _test()
    t1_alt = ABTest(
        id="other-test",
        name="t2",
        started_at="2026-05-20T18:00:00+00:00",
        arm_a=ABArm(model="A", weight=0.5),
        arm_b=ABArm(model="B", weight=0.5),
    )
    crossovers = 0
    for i in range(200):
        arm1, _ = resolve_arm(test=t1, team_id="team-z", request_id=f"req-{i}")
        arm2, _ = resolve_arm(test=t1_alt, team_id="team-z", request_id=f"req-{i}")
        if arm1 != arm2:
            crossovers += 1
    # Should cross over ~50% of the time across 200 reqs.
    assert 50 < crossovers < 150


# --------------------------------------------------------------------------- #
# should_apply                                                                #
# --------------------------------------------------------------------------- #


def test_should_apply_only_when_model_is_an_arm() -> None:
    t = _test()
    assert should_apply(t, "A") is True
    assert should_apply(t, "B") is True
    assert should_apply(t, "C") is False
    assert should_apply(t, "") is False


# --------------------------------------------------------------------------- #
# Stats: Welch's t-test                                                       #
# --------------------------------------------------------------------------- #


def test_welch_detects_clear_difference() -> None:
    """When the two arms have means 10 and 20 with low variance,
    t-test should reject the null at p<0.05 with N=10 per arm."""
    a = [10, 12, 11, 13, 9, 10, 11, 12, 14, 10]
    b = [20, 22, 21, 19, 23, 20, 21, 22, 20, 21]
    result = welchs_t_test(a, b)
    assert result is not None
    assert result.significant_at_05
    assert result.p_value < 0.001


def test_welch_no_difference_returns_high_p() -> None:
    """Identical distributions: p ≈ 1, t ≈ 0."""
    a = [10, 11, 12, 10, 11, 10, 11, 12]
    b = [10, 11, 12, 10, 11, 10, 11, 12]
    result = welchs_t_test(a, b)
    assert result is not None
    assert not result.significant_at_05
    assert abs(result.t_statistic) < 0.001


def test_welch_returns_none_for_tiny_samples() -> None:
    """One sample per arm is undefined."""
    assert welchs_t_test([1.0], [2.0]) is None
    assert welchs_t_test([], [1.0, 2.0]) is None


def test_welch_returns_none_when_both_arms_constant() -> None:
    """Both arms constants → zero variance → no t-test possible."""
    assert welchs_t_test([5, 5, 5], [10, 10, 10]) is None


def test_welch_handles_unequal_variance() -> None:
    """Classic Welch case — one arm has much higher variance than the
    other. The test should still produce a sensible result (one zero-
    variance arm is fine; only BOTH zero-variance is undefined)."""
    # One zero-variance arm + one spread arm: SE is dominated by the
    # spread side; means are close → not significant.
    a = [10, 10, 10, 10, 10, 10, 10, 10]  # zero variance
    b = [5, 8, 12, 15, 7, 11, 14, 10]  # spread around 10.25
    result = welchs_t_test(a, b)
    assert result is not None
    assert not result.significant_at_05  # means ~equal, tiny effect

    # Two non-zero-variance arms with different spreads, similar means
    # → also not significant.
    a2 = [10, 10.1, 9.9, 10.2, 9.8, 10.0, 10.1, 9.9]
    b2 = [5, 8, 12, 15, 7, 11, 14, 10]
    result2 = welchs_t_test(a2, b2)
    assert result2 is not None
    assert not result2.significant_at_05


# --------------------------------------------------------------------------- #
# Stats: χ² + arm summarise                                                   #
# --------------------------------------------------------------------------- #


def test_chi_squared_detects_pass_rate_difference() -> None:
    """Arm A: 80/100 pass; arm B: 50/100 pass → p < 0.001."""
    chi_sq, p = chi_squared_pass_fail(80, 20, 50, 50)
    assert chi_sq > 10
    assert p < 0.001


def test_chi_squared_identical_pass_rates_high_p() -> None:
    chi_sq, p = chi_squared_pass_fail(50, 50, 50, 50)
    assert chi_sq < 1
    assert p > 0.5


def test_summarise_arm_aggregates_correctly() -> None:
    stats = summarise_arm("a", costs=[100, 200, 150], tokens=[10, 20, 15])
    assert stats.arm == "a"
    assert stats.n == 3
    assert stats.mean_cost_hcents == 150.0
    assert stats.mean_total_tokens == 15.0
    assert stats.median_total_tokens == 15.0
    assert stats.sample_costs == (100.0, 200.0, 150.0)


def test_summarise_arm_handles_empty_sample() -> None:
    stats = summarise_arm("b", costs=[], tokens=[])
    assert stats.n == 0
    assert stats.mean_cost_hcents == 0.0
    assert stats.sample_costs == ()
