"use client";

/**
 * /guardrails — per-team guardrail policy + PII tokenization editor.
 * Phase 67.
 *
 * Layout:
 *   - Top: team picker + link to /guardrails/audit
 *   - Guardrail rules section: one row per known_rule_id with an
 *     action selector + "disabled" toggle
 *   - PII tokenization section: master enable toggle + TTL input
 *
 * Every section writes through PUT /v1/admin/security/{team_id} with
 * PATCH semantics; only the modified fields go on the wire.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Eye, Loader2, RefreshCw, Save, Shield } from "lucide-react";

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
  getSecurity,
  listTeams,
  updateSecurity,
} from "@/lib/api/client";
import type {
  GuardrailAction,
  GuardrailPolicy,
  SecurityConfig,
  Team,
} from "@/lib/api/schemas";

const RULE_DEFAULT_ACTION: Record<string, GuardrailAction> = {
  "pii.email": "redact",
  "pii.phone": "redact",
  "pii.ssn": "redact",
  "pii.ipv4": "redact",
  injection: "log_only",
  presidio: "redact",
  llama_guard: "block",
};

const RULE_DESCRIPTIONS: Record<string, string> = {
  "pii.email": "Regex email-address detector. Default action: redact.",
  "pii.phone":
    "Regex phone-number detector (US-leaning). Default action: redact.",
  "pii.ssn": "Regex US Social Security Number detector. Default action: redact.",
  "pii.ipv4": "Regex IPv4 address detector. Default action: redact.",
  injection:
    "Heuristic prompt-injection scanner. Default action: log_only (high false-positive cost on legitimate prompts that discuss prompt injection theory).",
  presidio:
    "Optional ML PII classifier (Phase 22). Only fires when PRESIDIO_ENABLED + the dependency is installed.",
  llama_guard:
    "Optional ML jailbreak classifier (Phase 44 — Llama PromptGuard 2). Per-team enable inside the policy sub-block.",
};

export default function GuardrailsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamsErr, setTeamsErr] = useState<string | null>(null);
  const [selectedTeamId, setSelectedTeamId] = useState<string>("");
  const [config, setConfig] = useState<SecurityConfig | null>(null);
  const [configErr, setConfigErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const ts = await listTeams();
        setTeams(ts);
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
      setConfig(await getSecurity(teamId));
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
    async (patch: Parameters<typeof updateSecurity>[1]) => {
      if (!selectedTeamId) return;
      try {
        const next = await updateSecurity(selectedTeamId, patch);
        setConfig(next);
        toast.success("Security config saved");
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
          <h1 className="text-2xl font-semibold tracking-tight">Guardrails</h1>
          <p className="text-sm text-muted-foreground">
            Per-team rule actions + PII tokenization. Hash-chained activity log
            lives under{" "}
            <Link
              href="/guardrails/audit"
              className="underline underline-offset-2 hover:text-foreground"
            >
              Audit
            </Link>
            .
          </p>
        </div>
        <div className="flex gap-2">
          {selectedTeamId ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void refresh(selectedTeamId);
              }}
            >
              <RefreshCw className="h-4 w-4" />
              Reload
            </Button>
          ) : null}
          <Button variant="outline" size="sm" asChild>
            <Link href="/guardrails/audit">
              <Eye className="h-4 w-4" />
              Audit
            </Link>
          </Button>
        </div>
      </div>

      {teamsErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive">{teamsErr}</p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Team</CardTitle>
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
            <p
              className="text-sm text-destructive"
              data-testid="security-load-error"
            >
              {configErr}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {selectedTeamId && config === null && !configErr ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading security config…
          </CardContent>
        </Card>
      ) : null}

      {config ? (
        <>
          <RulesSection config={config} onChange={applyPatch} />
          <PiiSection config={config} onChange={applyPatch} />
        </>
      ) : null}
    </div>
  );
}

// =========================================================================== //
// Rules                                                                       //
// =========================================================================== //

function RulesSection({
  config,
  onChange,
}: {
  config: SecurityConfig;
  onChange: (patch: { guardrail_policy: GuardrailPolicy | null }) => Promise<void>;
}) {
  // Build a working copy from the policy (or empty defaults).
  const policy = config.guardrail_policy ?? {};
  const disabledSet = new Set(policy.disabled_rules ?? []);
  const actions = (policy.rule_actions ?? {}) as Record<string, GuardrailAction>;

  async function setRuleAction(rule: string, action: GuardrailAction): Promise<void> {
    const nextActions = { ...actions, [rule]: action };
    await onChange({
      guardrail_policy: {
        ...policy,
        rule_actions: nextActions,
      },
    });
  }

  async function toggleDisabled(rule: string): Promise<void> {
    const next = new Set(disabledSet);
    if (next.has(rule)) next.delete(rule);
    else next.add(rule);
    await onChange({
      guardrail_policy: {
        ...policy,
        disabled_rules: Array.from(next).sort(),
      },
    });
  }

  async function resetPolicy(): Promise<void> {
    await onChange({ guardrail_policy: null });
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="flex items-start gap-2">
            <Shield className="mt-1 h-4 w-4 text-muted-foreground" />
            <div>
              <CardTitle className="text-sm">Rules</CardTitle>
              <CardDescription>
                Per-team overrides. Null policy = engine defaults
                ({config.known_rule_ids.length} rules known).
              </CardDescription>
            </div>
          </div>
          {config.guardrail_policy !== null ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void resetPolicy();
              }}
              data-testid="reset-policy"
            >
              Reset to defaults
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm" data-testid="rules-table">
          <thead className="text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-2 py-2">Rule</th>
              <th className="px-2 py-2">Action</th>
              <th className="px-2 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {config.known_rule_ids.map((rule) => {
              const isDisabled = disabledSet.has(rule);
              const current =
                actions[rule] ?? RULE_DEFAULT_ACTION[rule] ?? "log_only";
              return (
                <tr key={rule} className="border-t align-top">
                  <td className="px-2 py-3">
                    <div className="font-mono text-xs">{rule}</div>
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      {RULE_DESCRIPTIONS[rule] ?? ""}
                    </p>
                  </td>
                  <td className="px-2 py-3">
                    <select
                      value={current}
                      disabled={isDisabled}
                      onChange={(e) => {
                        void setRuleAction(rule, e.target.value as GuardrailAction);
                      }}
                      className="flex h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                      data-testid={`action-${rule}`}
                    >
                      {config.valid_actions.map((a) => (
                        <option key={a} value={a}>
                          {a}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-2 py-3">
                    <label className="inline-flex cursor-pointer items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        checked={!isDisabled}
                        onChange={() => {
                          void toggleDisabled(rule);
                        }}
                        className="accent-primary"
                        data-testid={`enabled-${rule}`}
                      />
                      {isDisabled ? (
                        <Badge variant="outline">disabled</Badge>
                      ) : (
                        <Badge variant="success">enabled</Badge>
                      )}
                    </label>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// PII tokenization                                                            //
// =========================================================================== //

function PiiSection({
  config,
  onChange,
}: {
  config: SecurityConfig;
  onChange: (patch: {
    pii_tokenization_enabled?: boolean;
    pii_token_ttl_seconds?: number | null;
  }) => Promise<void>;
}) {
  const [ttl, setTtl] = useState<string>(
    config.pii_token_ttl_seconds == null
      ? ""
      : String(config.pii_token_ttl_seconds),
  );
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setTtl(
      config.pii_token_ttl_seconds == null
        ? ""
        : String(config.pii_token_ttl_seconds),
    );
  }, [config.pii_token_ttl_seconds]);

  async function toggleEnabled(): Promise<void> {
    await onChange({ pii_tokenization_enabled: !config.pii_tokenization_enabled });
  }

  async function saveTtl(): Promise<void> {
    setSaving(true);
    try {
      const value = ttl.trim() === "" ? null : Number(ttl);
      if (value != null && (Number.isNaN(value) || value < 0)) {
        toast.error("TTL must be a non-negative integer");
        return;
      }
      await onChange({ pii_token_ttl_seconds: value });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">PII tokenization</CardTitle>
        <CardDescription>
          Reversible PII tokenization (Phase 38). Master switch + TTL on the
          per-tenant token map (Redis).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex items-center justify-between rounded-md border p-3">
          <div>
            <p className="text-sm font-medium">Enabled</p>
            <p className="text-[11px] text-muted-foreground">
              When off, ``TOKENIZE`` actions on rules are ignored — the engine
              falls back to the rule's action (typically redact).
            </p>
          </div>
          <label className="relative inline-flex h-5 w-9 cursor-pointer items-center">
            <input
              type="checkbox"
              checked={config.pii_tokenization_enabled}
              onChange={() => {
                void toggleEnabled();
              }}
              className="peer sr-only"
              data-testid="pii-enabled-toggle"
            />
            <span className="h-5 w-9 rounded-full bg-muted transition peer-checked:bg-primary" />
            <span className="absolute left-0.5 h-4 w-4 rounded-full bg-background shadow transition peer-checked:translate-x-4" />
          </label>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="pii-ttl" className="text-xs">
            Token TTL (seconds)
          </Label>
          <div className="flex gap-2">
            <Input
              id="pii-ttl"
              type="number"
              min={0}
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              placeholder="(engine default)"
              data-testid="pii-ttl-input"
            />
            <Button
              onClick={() => {
                void saveTtl();
              }}
              disabled={saving}
              data-testid="pii-ttl-save"
            >
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Save className="h-4 w-4" />
              )}
              Save
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Empty = use the engine default (typically 7 days). Set to 0 for
            immediate eviction after the call completes.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
