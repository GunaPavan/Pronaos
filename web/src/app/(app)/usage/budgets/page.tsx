"use client";

/**
 * /usage/budgets — per-team budget editor with progress bars.
 * Phase 64.
 *
 * Picks a team, fetches its budget config + current-period state,
 * lets the operator edit the token + cost caps. Progress bars show
 * how close the team is to each cap; days-until-reset countdown
 * tells the operator when the counters roll over.
 */
import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ChevronLeft, Loader2, Save } from "lucide-react";

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
import { Progress } from "@/components/ui/progress";
import {
  ApiError,
  getBudget,
  listTeams,
  updateBudget,
} from "@/lib/api/client";
import type { Budget, Team } from "@/lib/api/schemas";
import {
  budgetPct,
  daysUntil,
  formatHcents,
  formatTokens,
} from "@/lib/format";

export default function BudgetsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamsErr, setTeamsErr] = useState<string | null>(null);
  const [selectedTeamId, setSelectedTeamId] = useState<string>("");
  const [budget, setBudget] = useState<Budget | null>(null);
  const [budgetErr, setBudgetErr] = useState<string | null>(null);

  // Fetch teams up-front.
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

  // Fetch the selected team's budget whenever the selection changes.
  const refreshBudget = useCallback(async (teamId: string) => {
    setBudget(null);
    setBudgetErr(null);
    if (!teamId) return;
    try {
      setBudget(await getBudget(teamId));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setBudgetErr(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, []);

  useEffect(() => {
    void refreshBudget(selectedTeamId);
  }, [selectedTeamId, refreshBudget]);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/usage"
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="h-3 w-3" /> Usage
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">Budgets</h1>
          <p className="text-sm text-muted-foreground">
            Per-team monthly caps. ``null`` is unlimited; counters reset on the
            first calendar-month UTC boundary after `period_resets_at`.
          </p>
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
          <CardTitle>Select a team</CardTitle>
          <CardDescription>
            {teams.length === 0
              ? "No teams visible — create one under /teams first."
              : `${teams.length} team${teams.length === 1 ? "" : "s"} in scope`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <select
            value={selectedTeamId}
            onChange={(e) => setSelectedTeamId(e.target.value)}
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-transparent px-3 text-sm"
            data-testid="budget-team-select"
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

      {budgetErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="budget-load-error">
              {budgetErr}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {selectedTeamId && budget === null && !budgetErr ? (
        <Card>
          <CardContent className="py-6 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading budget…
          </CardContent>
        </Card>
      ) : null}

      {budget ? (
        <BudgetEditor
          budget={budget}
          onSaved={(next) => setBudget(next)}
        />
      ) : null}
    </div>
  );
}

function BudgetEditor({
  budget,
  onSaved,
}: {
  budget: Budget;
  onSaved: (next: Budget) => void;
}) {
  const [tokenCap, setTokenCap] = useState<string>(
    budget.monthly_token_budget == null ? "" : String(budget.monthly_token_budget),
  );
  const [costCap, setCostCap] = useState<string>(
    budget.monthly_cost_hcents_budget == null
      ? ""
      : String(budget.monthly_cost_hcents_budget),
  );
  const [saving, setSaving] = useState(false);

  // Reset form state when the underlying budget changes (e.g. team-switch).
  useEffect(() => {
    setTokenCap(
      budget.monthly_token_budget == null ? "" : String(budget.monthly_token_budget),
    );
    setCostCap(
      budget.monthly_cost_hcents_budget == null
        ? ""
        : String(budget.monthly_cost_hcents_budget),
    );
  }, [budget]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSaving(true);
    try {
      const next = await updateBudget(budget.team_id, {
        monthly_token_budget: tokenCap.trim() === "" ? null : Number(tokenCap),
        monthly_cost_hcents_budget:
          costCap.trim() === "" ? null : Number(costCap),
      });
      onSaved(next);
      toast.success("Budget updated");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Update failed");
    } finally {
      setSaving(false);
    }
  }

  const tokenPct = budgetPct(
    budget.current_period_tokens,
    budget.monthly_token_budget,
  );
  const costPct = budgetPct(
    budget.current_period_cost_hcents,
    budget.monthly_cost_hcents_budget,
  );
  const daysLeft = daysUntil(budget.period_resets_at);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Current period</CardTitle>
            <CardDescription>
              {daysLeft === 0
                ? "Resets today"
                : `Resets in ${daysLeft} day${daysLeft === 1 ? "" : "s"}`}
            </CardDescription>
          </div>
          <ResetBadge pct={Math.max(tokenPct, costPct)} />
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Token meter */}
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Tokens</span>
            <span
              className="font-mono text-xs text-muted-foreground"
              data-testid="token-progress"
            >
              {formatTokens(budget.current_period_tokens)} /{" "}
              {budget.monthly_token_budget == null
                ? "unlimited"
                : formatTokens(budget.monthly_token_budget)}
            </span>
          </div>
          <Progress
            value={budget.monthly_token_budget == null ? 0 : tokenPct}
            data-testid="token-progress-bar"
          />
        </div>

        {/* Cost meter */}
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Cost</span>
            <span
              className="font-mono text-xs text-muted-foreground"
              data-testid="cost-progress"
            >
              {formatHcents(budget.current_period_cost_hcents)} /{" "}
              {budget.monthly_cost_hcents_budget == null
                ? "unlimited"
                : formatHcents(budget.monthly_cost_hcents_budget)}
            </span>
          </div>
          <Progress
            value={budget.monthly_cost_hcents_budget == null ? 0 : costPct}
            data-testid="cost-progress-bar"
          />
        </div>

        {/* Edit form */}
        <form className="space-y-4 border-t pt-6" onSubmit={onSubmit}>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="token-cap">Monthly token cap</Label>
              <Input
                id="token-cap"
                type="number"
                inputMode="numeric"
                min={0}
                value={tokenCap}
                onChange={(e) => setTokenCap(e.target.value)}
                placeholder="leave empty for unlimited"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cost-cap">Monthly cost cap (hundredths-of-a-cent)</Label>
              <Input
                id="cost-cap"
                type="number"
                inputMode="numeric"
                min={0}
                value={costCap}
                onChange={(e) => setCostCap(e.target.value)}
                placeholder="leave empty for unlimited"
              />
              {costCap.trim() !== "" ? (
                <p className="text-xs text-muted-foreground">
                  ≈ {formatHcents(Number(costCap) || 0)}
                </p>
              ) : null}
            </div>
          </div>
          <div className="flex justify-end">
            <Button type="submit" disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save className="h-4 w-4" />
                  Save
                </>
              )}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function ResetBadge({ pct }: { pct: number }) {
  if (pct >= 100) return <Badge variant="destructive">Over cap</Badge>;
  if (pct >= 80) return <Badge variant="warning">Near cap</Badge>;
  if (pct > 0) return <Badge variant="success">Healthy</Badge>;
  return <Badge variant="secondary">No cap set</Badge>;
}
