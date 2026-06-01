"use client";

/**
 * Dashboard landing — Phase 64.
 *
 * Replaces the Phase 62 connectivity tiles with a real FinOps
 * dashboard:
 *  - Summary tiles: total spend / total tokens / total calls for the
 *    last 30 days
 *  - Daily-spend line chart
 *  - Top-5-teams-by-spend table
 *
 * All three derive from `/v1/admin/usage/timeseries` + `/v1/admin/usage`.
 */
import { useEffect, useState } from "react";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { toast } from "sonner";
import { Activity, Coins, Hash, Loader2 } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ApiError,
  getUsage,
  getUsageTimeseries,
} from "@/lib/api/client";
import type { TimeseriesResponse, UsageResponse } from "@/lib/api/schemas";
import { formatBucket, formatHcents, formatTokens } from "@/lib/format";

const WINDOW_DAYS = 30;

function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

function startOfTomorrowIso(): string {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString();
}

export default function DashboardPage() {
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();

    void (async () => {
      try {
        const [ts, u] = await Promise.all([
          getUsageTimeseries(
            {
              start_ts: isoDaysAgo(WINDOW_DAYS),
              end_ts: startOfTomorrowIso(),
              bucket: "day",
            },
            { signal: ac.signal },
          ),
          getUsage(
            {
              start_ts: isoDaysAgo(WINDOW_DAYS),
            },
            { signal: ac.signal },
          ),
        ]);
        if (!cancelled) {
          setTimeseries(ts);
          setUsage(u);
        }
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError
            ? `HTTP ${err.status}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "Unknown error";
        setLoadError(message);
        if (err instanceof ApiError && err.status === 403) {
          toast.error("This key lacks the admin:usage scope");
        }
      }
    })();

    return () => {
      cancelled = true;
      ac.abort();
    };
  }, []);

  // Top teams by spend (last 30d).
  const topTeams = usage
    ? Object.entries(
        usage.items.reduce<Record<string, { spend: number; calls: number }>>(
          (acc, item) => {
            const cur = acc[item.team_id] ?? { spend: 0, calls: 0 };
            acc[item.team_id] = {
              spend: cur.spend + item.cost_hcents,
              calls: cur.calls + 1,
            };
            return acc;
          },
          {},
        ),
      )
        .sort((a, b) => b[1].spend - a[1].spend)
        .slice(0, 5)
    : [];

  // Format the time-series for recharts.
  const chartData = timeseries
    ? timeseries.points.map((p) => ({
        bucket: p.bucket,
        label: formatBucket(p.bucket, timeseries.bucket_size_seconds),
        cost: p.cost_hcents,
      }))
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Last {WINDOW_DAYS} days of spend across the tenant. Drill into{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
            /usage
          </code>{" "}
          for filters + per-call breakdown.
        </p>
      </div>

      {loadError ? (
        <Card>
          <CardContent className="py-6">
            <p
              className="text-sm text-destructive"
              data-testid="dashboard-load-error"
            >
              {loadError}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {/* Summary tiles */}
      <div className="grid gap-4 md:grid-cols-3">
        <SummaryTile
          icon={Coins}
          title="Spend"
          value={
            usage ? formatHcents(usage.totals.cost_hcents) : null
          }
          subtitle="Total this window"
          testId="tile-spend"
        />
        <SummaryTile
          icon={Hash}
          title="Tokens"
          value={
            usage
              ? formatTokens(usage.totals.prompt_tokens + usage.totals.completion_tokens)
              : null
          }
          subtitle={
            usage
              ? `${formatTokens(usage.totals.prompt_tokens)} in / ${formatTokens(usage.totals.completion_tokens)} out`
              : ""
          }
          testId="tile-tokens"
        />
        <SummaryTile
          icon={Activity}
          title="Calls"
          value={usage ? usage.totals.requests.toLocaleString() : null}
          subtitle="API calls completed"
          testId="tile-calls"
        />
      </div>

      {/* Spend chart */}
      <Card>
        <CardHeader>
          <CardTitle>Daily spend</CardTitle>
          <CardDescription>
            Last {WINDOW_DAYS} days. Includes every team in the tenant.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {timeseries === null ? (
            <div className="flex h-64 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading chart…
            </div>
          ) : chartData.every((p) => p.cost === 0) ? (
            <div
              className="flex h-64 items-center justify-center text-sm text-muted-foreground"
              data-testid="chart-empty"
            >
              No spend in the last {WINDOW_DAYS} days.
            </div>
          ) : (
            <div className="h-64" data-testid="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 5, right: 16, bottom: 0, left: 0 }}>
                  <XAxis
                    dataKey="label"
                    fontSize={12}
                    stroke="currentColor"
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    fontSize={12}
                    stroke="currentColor"
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(v) => formatHcents(Number(v))}
                    width={60}
                  />
                  <Tooltip
                    formatter={(v) => formatHcents(Number(v))}
                    contentStyle={{
                      background: "hsl(var(--background))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                  />
                  <Line
                    type="monotone"
                    dataKey="cost"
                    stroke="hsl(var(--primary))"
                    strokeWidth={2}
                    dot={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Top teams */}
      <Card>
        <CardHeader>
          <CardTitle>Top teams by spend</CardTitle>
          <CardDescription>
            From the most recent {usage?.items.length ?? 0} usage records.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usage === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : topTeams.length === 0 ? (
            <p className="text-sm text-muted-foreground">No usage records yet.</p>
          ) : (
            <table className="w-full text-sm" data-testid="top-teams-table">
              <thead className="text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-2 py-2">Team</th>
                  <th className="px-2 py-2 text-right">Spend</th>
                  <th className="px-2 py-2 text-right">Calls</th>
                </tr>
              </thead>
              <tbody>
                {topTeams.map(([teamId, stats]) => (
                  <tr key={teamId} className="border-t">
                    <td className="px-2 py-2 font-mono text-xs">{teamId}</td>
                    <td className="px-2 py-2 text-right font-medium">
                      {formatHcents(stats.spend)}
                    </td>
                    <td className="px-2 py-2 text-right text-muted-foreground">
                      {stats.calls.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SummaryTile({
  icon: Icon,
  title,
  value,
  subtitle,
  testId,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  value: string | null;
  subtitle: string;
  testId: string;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-sm font-medium">{title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <p className="text-2xl font-semibold tabular-nums" data-testid={testId}>
          {value ?? "—"}
        </p>
        <p className="text-xs text-muted-foreground">{subtitle}</p>
      </CardContent>
    </Card>
  );
}
