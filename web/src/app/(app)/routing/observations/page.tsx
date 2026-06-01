"use client";

/**
 * /routing/observations — prompt-cache hit rates + reasoning ratios per model
 * (Phase 66 gap fill).
 *
 * Calls:
 *   GET /v1/admin/team/{id}/prompt-cache-stats
 *   GET /v1/admin/team/{id}/reasoning-stats
 *
 * Both endpoints return rolling-window Redis snapshots.  If Redis is not
 * configured the ``stats`` array is empty — the page surfaces that gracefully.
 */
import { useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ApiError,
  getPromptCacheStats,
  getReasoningStats,
  listTeams,
} from "@/lib/api/client";
import type {
  PromptCacheStatsResponse,
  ReasoningStatsResponse,
  Team,
} from "@/lib/api/schemas";

/** Format hcents (hundredths-of-cents) → human-readable $-string. */
function formatHcents(h: number): string {
  const cents = h / 100;
  if (cents < 1) return `${h} hcents`;
  if (cents < 100) return `$${(cents / 100).toFixed(4)}`;
  return `$${(cents / 100).toFixed(2)}`;
}

function pct(rate: number) {
  return `${(rate * 100).toFixed(1)}%`;
}

export default function ObservationsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState<string>("");
  const [cacheStats, setCacheStats] = useState<PromptCacheStatsResponse | null>(null);
  const [reasoningStats, setReasoningStats] = useState<ReasoningStatsResponse | null>(null);
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
    setCacheStats(null);
    setReasoningStats(null);

    void (async () => {
      try {
        const [cs, rs] = await Promise.all([
          getPromptCacheStats(teamId),
          getReasoningStats(teamId),
        ]);
        setCacheStats(cs);
        setReasoningStats(rs);
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Routing Observations</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Prompt-cache hit rates and reasoning token ratios per model, from the
          Redis rolling-window observers.
        </p>
      </div>

      {/* Team picker */}
      <div className="flex items-center gap-3">
        <label htmlFor="obs-team-select" className="text-sm font-medium whitespace-nowrap">
          Team
        </label>
        <select
          id="obs-team-select"
          data-testid="obs-team-select"
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
        <p className="text-sm text-destructive" data-testid="obs-error">
          {error}
        </p>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {/* Prompt-cache stats */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Prompt-cache hit rates</CardTitle>
        </CardHeader>
        <CardContent>
          {cacheStats?.stats.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No prompt-cache observations yet. Configure Redis and send traffic
              to a prompt-cache-aware model to populate this table.
            </p>
          )}
          {cacheStats && cacheStats.stats.length > 0 && (
            <div className="overflow-x-auto">
              <table
                className="w-full text-sm"
                data-testid="obs-cache-table"
              >
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th className="text-left py-2 pr-4 font-medium">Model</th>
                    <th className="text-right py-2 pr-4 font-medium">Samples</th>
                    <th className="text-right py-2 pr-4 font-medium">Hit rate</th>
                    <th className="text-right py-2 pr-4 font-medium">Cached tokens</th>
                    <th className="text-right py-2 font-medium">Saved</th>
                  </tr>
                </thead>
                <tbody>
                  {cacheStats.stats.map((s) => (
                    <tr key={s.fqmn} className="border-b last:border-0">
                      <td className="py-2 pr-4 font-mono text-xs">{s.fqmn}</td>
                      <td className="py-2 pr-4 text-right tabular-nums">{s.n_samples}</td>
                      <td
                        className="py-2 pr-4 text-right tabular-nums"
                        data-testid={`obs-cache-hit-rate-${s.fqmn}`}
                      >
                        {pct(s.hit_rate)}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">
                        {s.cached_tokens.toLocaleString()}
                      </td>
                      <td className="py-2 text-right tabular-nums">
                        {formatHcents(s.saved_hcents)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {cacheStats && (
            <p className="text-xs text-muted-foreground mt-3">
              Min samples threshold: {cacheStats.min_samples ?? "20 (default)"} ·
              Min hit-rate threshold: {cacheStats.min_hit_rate != null ? pct(cacheStats.min_hit_rate) : "10% (default)"}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Reasoning stats */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Reasoning token ratios</CardTitle>
        </CardHeader>
        <CardContent>
          {reasoningStats?.stats.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No reasoning observations yet. Send traffic to a reasoning-capable
              model to populate this table.
            </p>
          )}
          {reasoningStats && reasoningStats.stats.length > 0 && (
            <div className="overflow-x-auto">
              <table
                className="w-full text-sm"
                data-testid="obs-reasoning-table"
              >
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th className="text-left py-2 pr-4 font-medium">Model</th>
                    <th className="text-right py-2 pr-4 font-medium">Samples</th>
                    <th className="text-right py-2 pr-4 font-medium">Reasoning ratio</th>
                    <th className="text-right py-2 pr-4 font-medium">Completion tok</th>
                    <th className="text-right py-2 font-medium">Reasoning tok</th>
                  </tr>
                </thead>
                <tbody>
                  {reasoningStats.stats.map((s) => (
                    <tr key={s.fqmn} className="border-b last:border-0">
                      <td className="py-2 pr-4 font-mono text-xs">{s.fqmn}</td>
                      <td className="py-2 pr-4 text-right tabular-nums">{s.n_samples}</td>
                      <td
                        className="py-2 pr-4 text-right tabular-nums"
                        data-testid={`obs-reasoning-ratio-${s.fqmn}`}
                      >
                        {s.ratio.toFixed(3)}×
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">
                        {s.completion_tokens.toLocaleString()}
                      </td>
                      <td className="py-2 text-right tabular-nums">
                        {s.reasoning_tokens.toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {reasoningStats && (
            <p className="text-xs text-muted-foreground mt-3">
              Min samples: {reasoningStats.min_samples ?? "20 (default)"} ·
              Max ratio: {reasoningStats.max_ratio != null ? `${reasoningStats.max_ratio.toFixed(2)}×` : "no cap (default)"}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
