"use client";

/**
 * /routing/ab-tests — A/B test management console (Phase 66 gap fill).
 *
 * Shows the active test config for a team alongside per-arm aggregates and
 * the Welch's t-test result (p-value, 95% CI, Cohen's d) computed server-side
 * from usage_records.ab_arm.
 *
 * Calls:
 *   GET /v1/admin/team/{id}/ab-test
 */
import { useEffect, useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ApiError,
  getAbTest,
  listTeams,
} from "@/lib/api/client";
import type {
  ABTestResponse,
  Team,
} from "@/lib/api/schemas";

function formatHcents(h: number): string {
  const cents = h / 100;
  if (cents < 1) return `${h} hcents`;
  return `$${(cents / 100).toFixed(4)}`;
}

export default function ABTestsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState<string>("");
  const [result, setResult] = useState<ABTestResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const data = await listTeams();
        setTeams(data);
        const first = data[0];
        if (first) setTeamId(first.id);
      } catch { /* ignore */ }
    })();
  }, []);

  useEffect(() => {
    if (!teamId) return;
    setLoading(true);
    setError(null);
    setResult(null);

    void (async () => {
      try {
        setResult(await getAbTest(teamId));
      } catch (err) {
        const msg = err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error ? err.message : "Unknown error";
        setError(msg);
      } finally {
        setLoading(false);
      }
    })();
  }, [teamId]);

  const ttest = result?.t_test ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">A/B Tests</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Active routing experiment config and statistical results per team.
          Use{" "}
          <code className="font-mono text-xs rounded bg-muted px-1 py-0.5">
            pronaos-cli abtest create
          </code>{" "}
          to start a new test.
        </p>
      </div>

      {/* Team picker */}
      <div className="flex items-center gap-3">
        <label htmlFor="abtest-team-select" className="text-sm font-medium whitespace-nowrap">
          Team
        </label>
        <select
          id="abtest-team-select"
          data-testid="abtest-team-select"
          value={teamId}
          onChange={(e) => setTeamId(e.target.value)}
          className="flex h-9 rounded-md border border-input bg-transparent px-2 text-sm"
        >
          {teams.length === 0 && <option value="">Loading…</option>}
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name} ({t.id.slice(0, 8)})
            </option>
          ))}
        </select>
      </div>

      {error && (
        <p className="text-sm text-destructive" data-testid="abtest-error">
          {error}
        </p>
      )}
      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {/* No active test */}
      {result && !result.test_id && (
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground" data-testid="abtest-none">
              No active A/B test on this team. Run{" "}
              <code className="font-mono text-xs rounded bg-muted px-1 py-0.5">
                pronaos-cli abtest create --team {teamId} --name "My test" --arm-a model-x --arm-b model-y
              </code>{" "}
              to start one.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Active test config */}
      {result?.test_id && (
        <>
          <Card data-testid="abtest-config-card">
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">
                  {result.test_name ?? "Unnamed test"}
                </CardTitle>
                <Badge variant="secondary">{result.test_id.slice(0, 8)}</Badge>
              </div>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm">
                <dt className="text-muted-foreground">Started</dt>
                <dd className="font-mono text-xs">{result.started_at ?? "—"}</dd>
                <dt className="text-muted-foreground">Arm A model</dt>
                <dd className="font-mono text-xs">{result.arm_a_model ?? "—"}</dd>
                <dt className="text-muted-foreground">Arm B model</dt>
                <dd className="font-mono text-xs">{result.arm_b_model ?? "—"}</dd>
              </dl>
            </CardContent>
          </Card>

          {/* Per-arm stats table */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Per-arm aggregates</CardTitle>
            </CardHeader>
            <CardContent>
              {!result.arm_a_stats && !result.arm_b_stats ? (
                <p className="text-sm text-muted-foreground">
                  No usage records with arm assignments yet.
                </p>
              ) : (
                <table className="w-full text-sm" data-testid="abtest-arms-table">
                  <thead>
                    <tr className="border-b text-muted-foreground">
                      <th className="text-left py-2 pr-4 font-medium">Arm</th>
                      <th className="text-right py-2 pr-4 font-medium">Samples (n)</th>
                      <th className="text-right py-2 pr-4 font-medium">Mean cost</th>
                      <th className="text-right py-2 pr-4 font-medium">Mean tokens</th>
                      <th className="text-right py-2 font-medium">Median tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[result.arm_a_stats, result.arm_b_stats].map((arm) =>
                      arm ? (
                        <tr key={arm.arm} className="border-b last:border-0">
                          <td className="py-2 pr-4 font-semibold uppercase">{arm.arm}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">{arm.n}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">
                            {formatHcents(arm.mean_cost_hcents)}
                          </td>
                          <td className="py-2 pr-4 text-right tabular-nums">
                            {arm.mean_total_tokens.toFixed(1)}
                          </td>
                          <td className="py-2 text-right tabular-nums">
                            {arm.median_total_tokens.toFixed(0)}
                          </td>
                        </tr>
                      ) : null,
                    )}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>

          {/* Welch's t-test result */}
          <Card data-testid="abtest-ttest-card">
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">Welch&apos;s t-test (cost-per-call)</CardTitle>
                {ttest && (
                  ttest.significant_at_05 ? (
                    <Badge variant="destructive" className="flex items-center gap-1">
                      <CheckCircle2 className="h-3 w-3" />
                      Significant (p&lt;0.05)
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="flex items-center gap-1">
                      <XCircle className="h-3 w-3" />
                      Not significant
                    </Badge>
                  )
                )}
              </div>
            </CardHeader>
            <CardContent>
              {!ttest && (
                <p className="text-sm text-muted-foreground">
                  Need at least 2 samples per arm to compute the t-test.
                </p>
              )}
              {ttest && (
                <dl className="grid grid-cols-2 md:grid-cols-3 gap-x-8 gap-y-3 text-sm">
                  <div>
                    <dt className="text-muted-foreground text-xs">p-value</dt>
                    <dd
                      className="font-mono font-semibold"
                      data-testid="abtest-pvalue"
                    >
                      {ttest.p_value.toFixed(4)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">t-statistic</dt>
                    <dd className="font-mono">{ttest.t_statistic.toFixed(3)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">Degrees of freedom</dt>
                    <dd className="font-mono">{ttest.df.toFixed(1)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">Cohen&apos;s d</dt>
                    <dd className="font-mono">{ttest.cohens_d.toFixed(3)}</dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-muted-foreground text-xs">95% CI of (mean A − mean B)</dt>
                    <dd className="font-mono" data-testid="abtest-ci">
                      [{formatHcents(ttest.ci_low)}, {formatHcents(ttest.ci_high)}]
                    </dd>
                  </div>
                </dl>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
