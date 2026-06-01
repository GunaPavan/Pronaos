"""Per-arm aggregation + statistical-significance reporting for A/B tests.

Phase 29.

The hot request path (``core/abtest.py``) does not pull scipy. This
module is for offline analysis — the ``pronaos-cli abtest report``
command reads ``usage_records`` for a team, groups by ``ab_arm``,
computes per-arm aggregates, and runs:

- **Welch's t-test** on continuous metrics (latency, cost per call).
  Welch's variant doesn't assume equal variance, which matches
  reality — the arms typically have different cost or latency
  profiles by construction.
- **Chi-squared (χ²)** on binary pass/fail. The eval harness writes
  per-call scores into ``usage_records.status`` indirectly via the
  eval CLI; for now we use cost-per-call as the simplest continuous
  outcome.

Reports include the **p-value, 95% confidence interval, and effect
size** (Cohen's d for t-tests) so operators can read both
significance and magnitude. A small p-value with a tiny effect size
is statistical-but-not-practical significance — surfacing both keeps
operators honest about what the test actually shows.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArmStats:
    """Per-arm aggregate over a sample of completed calls."""

    arm: str  # "a" or "b"
    n: int
    mean_cost_hcents: float
    mean_total_tokens: float
    median_total_tokens: float
    # Caller fills in latency separately when they have it; the
    # ``usage_records`` table doesn't carry latency (yet — Phase 30
    # candidate). For now the stats engine works on cost + tokens.
    sample_costs: tuple[float, ...]  # raw values for the t-test


@dataclass(frozen=True, slots=True)
class TTestResult:
    """Welch's t-test outcome.

    ``p_value`` is the two-sided probability under the null hypothesis
    of equal means. ``cohens_d`` is the standardised mean difference
    (a > b in sign convention).

    ``ci_low``/``ci_high`` form the 95% CI of the mean difference
    using Welch-Satterthwaite degrees of freedom. They include zero
    when the test is non-significant.
    """

    t_statistic: float
    p_value: float
    df: float
    cohens_d: float
    ci_low: float
    ci_high: float
    significant_at_05: bool


def welchs_t_test(a_samples: list[float], b_samples: list[float]) -> TTestResult | None:
    """Welch's two-sided t-test on the difference of means (a - b).

    Returns ``None`` when either sample has fewer than 2 observations
    (the test is undefined). Otherwise returns a full result including
    p-value, 95% CI of the difference, and Cohen's d effect size.

    Uses ``scipy.stats.ttest_ind`` with ``equal_var=False`` (the Welch
    variant) for the p-value and degrees-of-freedom. Welch handles
    unequal sample sizes and unequal variances — both common in
    real A/B-routing data where one arm gets more traffic and the
    cheap model has tighter cost variance.
    """
    n_a, n_b = len(a_samples), len(b_samples)
    if n_a < 2 or n_b < 2:
        return None

    mean_a = statistics.fmean(a_samples)
    mean_b = statistics.fmean(b_samples)
    var_a = statistics.variance(a_samples)
    var_b = statistics.variance(b_samples)

    if var_a == 0.0 and var_b == 0.0:
        # Both arms are constants — no t-test possible. Operator
        # should pick a different metric.
        return None

    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0.0:
        return None

    diff = mean_a - mean_b

    # Defer to scipy.stats for the p-value + df — it ships the
    # vetted reference implementation of Welch's t and we'd reinvent
    # bugs by computing it from scratch. scipy is already a
    # transitive dep (sentence-transformers needs it), so no new
    # surface to maintain.
    from scipy import stats

    t_stat, p_value = stats.ttest_ind(a_samples, b_samples, equal_var=False)
    # scipy returns numpy scalars; cast for type-clean public surface.
    t = float(t_stat)
    p = float(p_value)
    # Welch-Satterthwaite df (scipy doesn't expose it directly via
    # ttest_ind, so compute it ourselves — used only for reporting).
    df_num = (var_a / n_a + var_b / n_b) ** 2
    df_den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = df_num / df_den if df_den > 0 else float("inf")

    # Cohen's d using the pooled SD (Hedge's correction is overkill
    # for the typical n we'd see here).
    pooled_sd = math.sqrt((var_a + var_b) / 2)
    cohens_d = diff / pooled_sd if pooled_sd > 0 else 0.0

    # 95% CI of (mean_a - mean_b). Critical t for df is approximated
    # as 1.96 for large df; for small df we use scipy's exact value.
    t_crit = float(stats.t.ppf(0.975, df)) if df > 0 else 1.96
    ci_low = diff - t_crit * se
    ci_high = diff + t_crit * se

    return TTestResult(
        t_statistic=t,
        p_value=p,
        df=df,
        cohens_d=cohens_d,
        ci_low=ci_low,
        ci_high=ci_high,
        significant_at_05=p < 0.05,
    )


def chi_squared_pass_fail(
    a_passes: int, a_fails: int, b_passes: int, b_fails: int
) -> tuple[float, float]:
    """χ² test on a 2x2 contingency table of pass/fail per arm.

    Returns ``(chi_squared, p_value)``. Uses Yates' continuity
    correction for small samples (n < 100).
    """
    rows = [(a_passes, a_fails), (b_passes, b_fails)]
    cols = [(a_passes + b_passes), (a_fails + b_fails)]
    total = sum(cols)
    if total == 0:
        return (0.0, 1.0)

    row_totals = [r[0] + r[1] for r in rows]
    chi_sq = 0.0
    yates = total < 100
    for i, row in enumerate(rows):
        for j, observed in enumerate(row):
            expected = row_totals[i] * cols[j] / total
            if expected == 0:
                continue
            diff = abs(observed - expected)
            if yates:
                diff = max(0.0, diff - 0.5)
            chi_sq += (diff * diff) / expected

    # df for 2x2 is 1; p-value is 1 - CDF(chi_sq; df=1) = erfc(sqrt(chi_sq/2))
    p_value = math.erfc(math.sqrt(chi_sq / 2.0))
    return (chi_sq, p_value)


def summarise_arm(arm: str, costs: list[int], tokens: list[int]) -> ArmStats:
    """Build an ``ArmStats`` from per-call lists of cost (hcents) and tokens."""
    n = len(costs)
    if n == 0:
        return ArmStats(
            arm=arm,
            n=0,
            mean_cost_hcents=0.0,
            mean_total_tokens=0.0,
            median_total_tokens=0.0,
            sample_costs=(),
        )
    return ArmStats(
        arm=arm,
        n=n,
        mean_cost_hcents=statistics.fmean(costs),
        mean_total_tokens=statistics.fmean(tokens),
        median_total_tokens=float(statistics.median(tokens)),
        sample_costs=tuple(float(c) for c in costs),
    )
