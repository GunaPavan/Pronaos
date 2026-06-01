"use client";

/**
 * /routing — composed routing console (Phase 66).
 *
 * Layout:
 *   - Top: team picker + saved-state badge
 *   - Strategy section: 7 radio cards explaining each strategy
 *   - Allowlist section: checkbox each model; commit on Save
 *   - Quality + tool-use scores: editable tables (numeric input per row)
 *   - Thresholds: 6 numeric inputs (quality, tool-use, prompt-cache,
 *     reasoning) with explanatory captions
 *
 * Every section saves through the SAME composed
 * ``PUT /v1/admin/routing/{team_id}`` endpoint with PATCH semantics —
 * only the field you change goes on the wire. Everything else is
 * preserved server-side.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, Loader2, RefreshCw, Save } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  getRouting,
  listModels,
  listTeams,
  updateRouting,
} from "@/lib/api/client";
import type {
  ModelInfo,
  RoutingConfig,
  RoutingScoreEntry,
  RoutingStrategy,
  Team,
} from "@/lib/api/schemas";
import { ROUTING_STRATEGIES } from "@/lib/api/schemas";

const STRATEGY_DESCRIPTIONS: Record<RoutingStrategy, string> = {
  cheapest: "Pick the model with the lowest input+output price.",
  fastest: "Pick by typical-p50 latency from the catalog.",
  balanced: "Weighted blend of cost and latency.",
  "quality-aware-cheapest":
    "Drop models below the quality threshold using stored eval scores, then pick the cheapest survivor.",
  "tool-use-aware-cheapest":
    "When the request carries tools, drop models below the tool-use threshold, then pick the cheapest survivor. Tool-less requests fall back to plain cheapest.",
  "prompt-cache-aware-cheapest":
    "Discount each candidate's input rate by its observed prompt-cache hit rate before picking the cheapest. Only meaningful on Anthropic (0.10x) and OpenAI (0.50x).",
  "reasoning-aware-cheapest":
    "Multiply each candidate's output rate by (1 + observed reasoning ratio) before picking the cheapest. Optionally exclude models exceeding max-ratio.",
};

export default function RoutingPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamsErr, setTeamsErr] = useState<string | null>(null);
  const [selectedTeamId, setSelectedTeamId] = useState<string>("");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [config, setConfig] = useState<RoutingConfig | null>(null);
  const [configErr, setConfigErr] = useState<string | null>(null);

  // Load teams + models once.
  useEffect(() => {
    void (async () => {
      try {
        const [ts, ms] = await Promise.all([listTeams(), listModels()]);
        setTeams(ts);
        setModels(ms);
        if (ts[0]) setSelectedTeamId(ts[0].id);
      } catch (err) {
        setTeamsErr(err instanceof Error ? err.message : "Unknown error");
      }
    })();
  }, []);

  const refresh = useCallback(async (teamId: string) => {
    if (!teamId) {
      setConfig(null);
      return;
    }
    setConfigErr(null);
    setConfig(null);
    try {
      setConfig(await getRouting(teamId));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setConfigErr(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, []);

  useEffect(() => {
    void refresh(selectedTeamId);
  }, [selectedTeamId, refresh]);

  const applyPatch = useCallback(
    async (patch: Parameters<typeof updateRouting>[1]) => {
      if (!selectedTeamId) return;
      try {
        const next = await updateRouting(selectedTeamId, patch);
        setConfig(next);
        toast.success("Routing config saved");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Save failed");
        throw err;
      }
    },
    [selectedTeamId],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Routing</h1>
          <p className="text-sm text-muted-foreground">
            Per-team strategy, scores, allowlist, and thresholds. Reads use{" "}
            <code className="font-mono">admin:usage</code>; writes use{" "}
            <code className="font-mono">admin:identity</code>.
          </p>
        </div>
        {selectedTeamId ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void refresh(selectedTeamId);
            }}
            data-testid="refresh-button"
          >
            <RefreshCw className="h-4 w-4" />
            Reload
          </Button>
        ) : null}
      </div>

      {teamsErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive">{teamsErr}</p>
          </CardContent>
        </Card>
      ) : null}

      {/* Team picker */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Team</CardTitle>
          <CardDescription>
            {teams.length === 0
              ? "No teams visible — create one under /teams first."
              : `Editing ${teams.length} team${teams.length === 1 ? "" : "s"} in scope`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <select
            value={selectedTeamId}
            onChange={(e) => setSelectedTeamId(e.target.value)}
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-transparent px-3 text-sm"
            data-testid="team-select"
          >
            <option value="">Select a team…</option>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </CardContent>
      </Card>

      {configErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="routing-load-error">
              {configErr}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {selectedTeamId && config === null && !configErr ? (
        <Card>
          <CardContent className="py-6 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading routing config…
          </CardContent>
        </Card>
      ) : null}

      {config ? (
        <>
          <StrategySection config={config} onChange={applyPatch} />
          <AllowlistSection
            config={config}
            models={models}
            onChange={applyPatch}
          />
          <ScoresSection config={config} onChange={applyPatch} />
          <ThresholdsSection config={config} onChange={applyPatch} />
        </>
      ) : null}
    </div>
  );
}

// =========================================================================== //
// Strategy — 7 radio cards                                                    //
// =========================================================================== //

function StrategySection({
  config,
  onChange,
}: {
  config: RoutingConfig;
  onChange: (patch: { routing_strategy: RoutingStrategy | null }) => Promise<void>;
}) {
  const current = config.routing_strategy ?? "cheapest";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Strategy</CardTitle>
        <CardDescription>
          {config.routing_strategy === null
            ? "Default (cheapest)."
            : `Active: ${config.routing_strategy}`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid gap-2 md:grid-cols-2" data-testid="strategy-grid">
          {ROUTING_STRATEGIES.map((s) => {
            const active = s === current;
            return (
              <button
                key={s}
                type="button"
                onClick={() => {
                  if (active) return;
                  void onChange({ routing_strategy: s });
                }}
                data-testid={`strategy-${s}`}
                data-active={active ? "true" : "false"}
                className={
                  active
                    ? "flex flex-col gap-1 rounded-md border-2 border-primary bg-primary/5 p-3 text-left"
                    : "flex flex-col gap-1 rounded-md border p-3 text-left transition-colors hover:bg-accent"
                }
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs">{s}</span>
                  {active ? (
                    <CheckCircle2 className="h-4 w-4 text-primary" />
                  ) : null}
                </div>
                <p className="text-xs text-muted-foreground">
                  {STRATEGY_DESCRIPTIONS[s]}
                </p>
              </button>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// Allowlist                                                                   //
// =========================================================================== //

function AllowlistSection({
  config,
  models,
  onChange,
}: {
  config: RoutingConfig;
  models: ModelInfo[];
  onChange: (patch: { allowed_models: string[] | null }) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Set<string>>(
    () => new Set(config.allowed_models ?? []),
  );
  const [saving, setSaving] = useState(false);
  const allowlistMode = config.allowed_models !== null;

  useEffect(() => {
    setDraft(new Set(config.allowed_models ?? []));
  }, [config.allowed_models]);

  function toggle(fqmn: string): void {
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(fqmn)) next.delete(fqmn);
      else next.add(fqmn);
      return next;
    });
  }

  async function save(): Promise<void> {
    setSaving(true);
    try {
      await onChange({ allowed_models: Array.from(draft).sort() });
    } finally {
      setSaving(false);
    }
  }

  async function clearAllowlist(): Promise<void> {
    setSaving(true);
    try {
      await onChange({ allowed_models: null });
    } finally {
      setSaving(false);
    }
  }

  const dirty = useMemo(() => {
    const current = new Set(config.allowed_models ?? []);
    if (current.size !== draft.size) return true;
    for (const x of draft) if (!current.has(x)) return true;
    return false;
  }, [config.allowed_models, draft]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-sm">Allowed models</CardTitle>
            <CardDescription>
              {allowlistMode
                ? `Whitelist active — ${draft.size} of ${models.length} models allowed.`
                : "No allowlist — every catalog model is allowed."}
            </CardDescription>
          </div>
          <div className="flex gap-2">
            {allowlistMode ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void clearAllowlist()}
                disabled={saving}
              >
                Remove allowlist
              </Button>
            ) : null}
            <Button
              size="sm"
              onClick={() => void save()}
              disabled={!dirty || saving}
              data-testid="allowlist-save"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {models.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Loading models from /v1/admin/models…
          </p>
        ) : (
          <div className="grid max-h-96 grid-cols-1 gap-1 overflow-y-auto pr-1 md:grid-cols-2">
            {models.map((m) => (
              <label
                key={m.fqmn}
                className="flex items-center gap-2 rounded px-2 py-1 text-xs hover:bg-accent"
              >
                <input
                  type="checkbox"
                  checked={draft.has(m.fqmn)}
                  onChange={() => toggle(m.fqmn)}
                  className="accent-primary"
                  data-testid={`allowlist-checkbox-${m.fqmn}`}
                />
                <span className="font-mono">{m.fqmn}</span>
                {!m.provider_configured ? (
                  <Badge variant="outline" className="text-[10px]">
                    unconfigured
                  </Badge>
                ) : null}
              </label>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// Scores tables                                                               //
// =========================================================================== //

function ScoresSection({
  config,
  onChange,
}: {
  config: RoutingConfig;
  onChange: (patch: {
    quality_scores?: Record<string, RoutingScoreEntry> | null;
    tool_use_scores?: Record<string, RoutingScoreEntry> | null;
  }) => Promise<void>;
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <ScoreTable
        title="Quality scores"
        helpText="Used by quality-aware-cheapest. fqmn → 0.0..1.0."
        scores={config.quality_scores}
        onSave={(scores) => onChange({ quality_scores: scores })}
        testIdPrefix="quality"
      />
      <ScoreTable
        title="Tool-use scores"
        helpText="Used by tool-use-aware-cheapest. fqmn → 0.0..1.0."
        scores={config.tool_use_scores}
        onSave={(scores) => onChange({ tool_use_scores: scores })}
        testIdPrefix="tool-use"
      />
    </div>
  );
}

function ScoreTable({
  title,
  helpText,
  scores,
  onSave,
  testIdPrefix,
}: {
  title: string;
  helpText: string;
  scores: Record<string, RoutingScoreEntry> | null;
  onSave: (next: Record<string, RoutingScoreEntry> | null) => Promise<void>;
  testIdPrefix: string;
}) {
  const [rows, setRows] = useState<Array<{ fqmn: string; score: string }>>(() =>
    scores
      ? Object.entries(scores).map(([fqmn, entry]) => ({
          fqmn,
          score: String(entry.score),
        }))
      : [],
  );
  const [newFqmn, setNewFqmn] = useState("");
  const [newScore, setNewScore] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setRows(
      scores
        ? Object.entries(scores).map(([fqmn, entry]) => ({
            fqmn,
            score: String(entry.score),
          }))
        : [],
    );
  }, [scores]);

  function updateRow(idx: number, value: string): void {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, score: value } : r)));
  }

  function removeRow(idx: number): void {
    setRows((prev) => prev.filter((_, i) => i !== idx));
  }

  function addRow(): void {
    const fqmn = newFqmn.trim();
    const score = newScore.trim();
    if (!fqmn || !score) return;
    if (rows.some((r) => r.fqmn === fqmn)) {
      toast.error(`${fqmn} already in table`);
      return;
    }
    setRows((prev) => [...prev, { fqmn, score }]);
    setNewFqmn("");
    setNewScore("");
  }

  async function save(): Promise<void> {
    setSaving(true);
    try {
      if (rows.length === 0) {
        await onSave(null);
        return;
      }
      const out: Record<string, RoutingScoreEntry> = {};
      for (const row of rows) {
        const score = Number(row.score);
        if (Number.isNaN(score)) {
          toast.error(`Invalid score for ${row.fqmn}`);
          return;
        }
        // Preserve metadata (n_samples / source_eval_id / ts) from the
        // original entry where present.
        const prior = scores?.[row.fqmn];
        out[row.fqmn] = { ...(prior ?? {}), score };
      }
      await onSave(out);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-sm">{title}</CardTitle>
            <CardDescription className="text-xs">{helpText}</CardDescription>
          </div>
          <Button
            size="sm"
            onClick={() => void save()}
            disabled={saving}
            data-testid={`${testIdPrefix}-save`}
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {rows.length === 0 ? (
          <p className="text-xs text-muted-foreground">No scores stored.</p>
        ) : (
          <table className="w-full text-xs" data-testid={`${testIdPrefix}-table`}>
            <thead className="text-left text-[10px] uppercase text-muted-foreground">
              <tr>
                <th className="py-1">Model</th>
                <th className="py-1 text-right">Score</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => (
                <tr key={row.fqmn} className="border-t">
                  <td className="py-1 font-mono">{row.fqmn}</td>
                  <td className="py-1 text-right">
                    <Input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={row.score}
                      onChange={(e) => updateRow(idx, e.target.value)}
                      className="h-7 w-20 text-right text-xs"
                    />
                  </td>
                  <td className="py-1 pl-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => removeRow(idx)}
                      className="h-7 px-2 text-[10px]"
                    >
                      Remove
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="flex gap-2 border-t pt-3">
          <Input
            type="text"
            placeholder="provider/model"
            value={newFqmn}
            onChange={(e) => setNewFqmn(e.target.value)}
            className="h-8 text-xs font-mono"
            data-testid={`${testIdPrefix}-new-fqmn`}
          />
          <Input
            type="number"
            min={0}
            max={1}
            step={0.05}
            placeholder="0.0"
            value={newScore}
            onChange={(e) => setNewScore(e.target.value)}
            className="h-8 w-24 text-xs"
            data-testid={`${testIdPrefix}-new-score`}
          />
          <Button
            size="sm"
            variant="outline"
            onClick={addRow}
            disabled={!newFqmn.trim() || !newScore.trim()}
          >
            Add
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// Thresholds                                                                  //
// =========================================================================== //

function ThresholdsSection({
  config,
  onChange,
}: {
  config: RoutingConfig;
  onChange: (patch: Record<string, number | null>) => Promise<void>;
}) {
  const [draft, setDraft] = useState({
    quality_threshold: config.quality_threshold,
    tool_use_threshold: config.tool_use_threshold,
    prompt_cache_min_samples: config.prompt_cache_min_samples,
    prompt_cache_min_hit_rate: config.prompt_cache_min_hit_rate,
    reasoning_aware_min_samples: config.reasoning_aware_min_samples,
    reasoning_aware_max_ratio: config.reasoning_aware_max_ratio,
  });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft({
      quality_threshold: config.quality_threshold,
      tool_use_threshold: config.tool_use_threshold,
      prompt_cache_min_samples: config.prompt_cache_min_samples,
      prompt_cache_min_hit_rate: config.prompt_cache_min_hit_rate,
      reasoning_aware_min_samples: config.reasoning_aware_min_samples,
      reasoning_aware_max_ratio: config.reasoning_aware_max_ratio,
    });
  }, [config]);

  function parseField(value: string): number | null {
    const t = value.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isNaN(n) ? null : n;
  }

  async function save(): Promise<void> {
    setSaving(true);
    try {
      await onChange({
        quality_threshold: draft.quality_threshold,
        tool_use_threshold: draft.tool_use_threshold,
        prompt_cache_min_samples: draft.prompt_cache_min_samples,
        prompt_cache_min_hit_rate: draft.prompt_cache_min_hit_rate,
        reasoning_aware_min_samples: draft.reasoning_aware_min_samples,
        reasoning_aware_max_ratio: draft.reasoning_aware_max_ratio,
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-sm">Thresholds</CardTitle>
            <CardDescription className="text-xs">
              Empty = use the gateway default for the active strategy.
            </CardDescription>
          </div>
          <Button
            size="sm"
            onClick={() => void save()}
            disabled={saving}
            data-testid="thresholds-save"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        <ThresholdField
          id="quality_threshold"
          label="Quality threshold"
          help="0..1. Drop models below this score (default 0.7)."
          step={0.05}
          min={0}
          max={1}
          value={draft.quality_threshold}
          onChange={(v) => setDraft({ ...draft, quality_threshold: v })}
        />
        <ThresholdField
          id="tool_use_threshold"
          label="Tool-use threshold"
          help="0..1. Drop models below this tool-use score (default 0.9)."
          step={0.05}
          min={0}
          max={1}
          value={draft.tool_use_threshold}
          onChange={(v) => setDraft({ ...draft, tool_use_threshold: v })}
        />
        <ThresholdField
          id="prompt_cache_min_samples"
          label="Prompt-cache min samples"
          help="Trust the hit-rate observation only after N samples (default 20)."
          step={1}
          min={0}
          value={draft.prompt_cache_min_samples}
          onChange={(v) => setDraft({ ...draft, prompt_cache_min_samples: v })}
        />
        <ThresholdField
          id="prompt_cache_min_hit_rate"
          label="Prompt-cache min hit rate"
          help="0..1. Below this, no discount applied (default 0.1)."
          step={0.05}
          min={0}
          max={1}
          value={draft.prompt_cache_min_hit_rate}
          onChange={(v) => setDraft({ ...draft, prompt_cache_min_hit_rate: v })}
        />
        <ThresholdField
          id="reasoning_aware_min_samples"
          label="Reasoning min samples"
          help="Trust the reasoning-ratio observation only after N samples (default 20)."
          step={1}
          min={0}
          value={draft.reasoning_aware_min_samples}
          onChange={(v) => setDraft({ ...draft, reasoning_aware_min_samples: v })}
        />
        <ThresholdField
          id="reasoning_aware_max_ratio"
          label="Reasoning max ratio"
          help="Exclude models exceeding this observed ratio (no default)."
          step={0.05}
          min={0}
          value={draft.reasoning_aware_max_ratio}
          onChange={(v) => setDraft({ ...draft, reasoning_aware_max_ratio: v })}
        />
      </CardContent>
    </Card>
  );
}

function ThresholdField({
  id,
  label,
  help,
  step,
  min,
  max,
  value,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  step: number;
  min: number;
  max?: number;
  value: number | null;
  onChange: (next: number | null) => void;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="text-xs">
        {label}
      </Label>
      <Input
        id={id}
        type="number"
        step={step}
        min={min}
        max={max}
        value={value === null ? "" : value}
        onChange={(e) => {
          const t = e.target.value.trim();
          if (t === "") onChange(null);
          else {
            const n = Number(t);
            onChange(Number.isNaN(n) ? null : n);
          }
        }}
        placeholder="(default)"
        data-testid={`threshold-${id}`}
      />
      <p className="text-[11px] text-muted-foreground">{help}</p>
    </div>
  );
}
