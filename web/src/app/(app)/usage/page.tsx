"use client";

/**
 * /usage — drilldown view with window + team filters + per-call table.
 * Phase 64.
 *
 * The dashboard at / shows the 30-day rollup; this page lets the
 * operator narrow by team and adjust window (24h / 7d / 30d), plus
 * scroll through the most-recent call list.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { toast } from "sonner";
import { Loader2, Wallet } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  getUsage,
  getUsageTimeseries,
  listTeams,
} from "@/lib/api/client";
import type {
  Team,
  TimeseriesResponse,
  UsageResponse,
} from "@/lib/api/schemas";
import { formatBucket, formatHcents, formatTokens } from "@/lib/format";

type Window = "24h" | "7d" | "30d";

function windowStartIso(w: Window): string {
  const ms = w === "24h" ? 86_400_000 : w === "7d" ? 7 * 86_400_000 : 30 * 86_400_000;
  return new Date(Date.now() - ms).toISOString();
}
function windowEndIso(): string {
  const d = new Date();
  d.setUTCMinutes(d.getUTCMinutes() + 1, 0, 0); // round forward to the next minute
  return d.toISOString();
}
function windowBucket(w: Window): "hour" | "day" {
  return w === "24h" ? "hour" : "day";
}

export default function UsagePage() {
  const [windowSel, setWindowSel] = useState<Window>("7d");
  const [teamFilter, setTeamFilter] = useState<string>("");
  const [teams, setTeams] = useState<Team[]>([]);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    setTimeseries(null);
    setUsage(null);
    try {
      const start = windowStartIso(windowSel);
      const end = windowEndIso();
      const [ts, u] = await Promise.all([
        getUsageTimeseries({
          start_ts: start,
          end_ts: end,
          bucket: windowBucket(windowSel),
          team_id: teamFilter || undefined,
        }),
        getUsage({
          start_ts: start,
          team_id: teamFilter || undefined,
        }),
      ]);
      setTimeseries(ts);
      setUsage(u);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, [windowSel, teamFilter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Load teams once (for the dropdown). Failure is non-fatal — the
  // page degrades to "filter by team id only" but stays usable.
  useEffect(() => {
    void (async () => {
      try {
        setTeams(await listTeams());
      } catch {
        /* tolerated — admin:identity not required to view usage */
      }
    })();
  }, []);

  const chartData = useMemo(
    () =>
      timeseries
        ? timeseries.points.map((p) => ({
            label: formatBucket(p.bucket, timeseries.bucket_size_seconds),
            cost: p.cost_hcents,
            requests: p.requests,
          }))
        : [],
    [timeseries],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Usage</h1>
          <p className="text-sm text-muted-foreground">
            Filter by team + window. Manage caps under{" "}
            <Link
              href="/usage/budgets"
              className="underline underline-offset-2 hover:text-foreground"
            >
              Budgets
            </Link>
            .
          </p>
        </div>
        <Button variant="outline" asChild>
          <Link href="/usage/budgets">
            <Wallet className="h-4 w-4" />
            Budgets
          </Link>
        </Button>
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 py-4">
          <div className="space-y-1">
            <Label htmlFor="window" className="text-xs">
              Window
            </Label>
            <select
              id="window"
              value={windowSel}
              onChange={(e) => setWindowSel(e.target.value as Window)}
              className="flex h-9 rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="team" className="text-xs">
              Team
            </Label>
            <select
              id="team"
              value={teamFilter}
              onChange={(e) => setTeamFilter(e.target.value)}
              className="flex h-9 rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="">All teams</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        </CardContent>
      </Card>

      {loadError ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="usage-load-error">
              {loadError}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {/* Chart */}
      <Card>
        <CardHeader>
          <CardTitle>Spend over time</CardTitle>
          <CardDescription>
            Bucket: {timeseries?.bucket_size_seconds === 3600 ? "1 hour" : "1 day"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {timeseries === null ? (
            <div className="flex h-64 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading chart…
            </div>
          ) : (
            <div className="h-64" data-testid="usage-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 5, right: 16, bottom: 0, left: 0 }}>
                  <XAxis dataKey="label" fontSize={12} stroke="currentColor" tickLine={false} axisLine={false} />
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
                  <Bar dataKey="cost" fill="hsl(var(--primary))" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Per-call table */}
      <Card>
        <CardHeader>
          <CardTitle>Recent calls</CardTitle>
          <CardDescription>
            {usage
              ? `${usage.totals.requests.toLocaleString()} total, showing the most recent ${usage.items.length}`
              : "Loading…"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usage === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : usage.items.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="usage-empty">
              No usage records in this window.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="usage-table">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-2 py-2">Time</th>
                    <th className="px-2 py-2">Team</th>
                    <th className="px-2 py-2">Provider</th>
                    <th className="px-2 py-2">Model</th>
                    <th className="px-2 py-2 text-right">Tokens</th>
                    <th className="px-2 py-2 text-right">Cost</th>
                    <th className="px-2 py-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.items.map((it) => (
                    <tr key={it.ts + it.team_id + it.request_id} className="border-t">
                      <td className="px-2 py-2 text-xs text-muted-foreground">
                        {new Date(it.ts).toLocaleString()}
                      </td>
                      <td className="px-2 py-2 font-mono text-xs">{it.team_id.slice(0, 8)}</td>
                      <td className="px-2 py-2 text-xs">{it.provider}</td>
                      <td className="px-2 py-2 text-xs">{it.model}</td>
                      <td className="px-2 py-2 text-right tabular-nums">
                        {formatTokens(it.prompt_tokens + it.completion_tokens)}
                      </td>
                      <td className="px-2 py-2 text-right tabular-nums font-medium">
                        {formatHcents(it.cost_hcents)}
                      </td>
                      <td className="px-2 py-2 text-xs">{it.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
