"use client";

/**
 * /doctor — operator health check (Phase 68).
 *
 * Surfaces the same 14-gate health check the `pronaos-cli doctor`
 * command runs (Phase 61). One click to (re-)run, summary tiles
 * up top, grouped gate results below — colour-coded by verdict.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  Loader2,
  MinusCircle,
  RefreshCw,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, getDoctorReport } from "@/lib/api/client";
import type { DoctorGate, DoctorResponse, DoctorVerdict } from "@/lib/api/schemas";

function verdictIcon(verdict: DoctorVerdict): React.ReactNode {
  switch (verdict) {
    case "PASS":
      return <CheckCircle2 className="h-4 w-4 text-green-600 dark:text-green-400" />;
    case "FAIL":
      return <AlertCircle className="h-4 w-4 text-destructive" />;
    case "WARN":
      return <AlertTriangle className="h-4 w-4 text-yellow-600 dark:text-yellow-400" />;
    case "SKIP":
      return <MinusCircle className="h-4 w-4 text-muted-foreground" />;
  }
}

function verdictBadge(verdict: DoctorVerdict): React.ReactNode {
  switch (verdict) {
    case "PASS":
      return <Badge variant="success">PASS</Badge>;
    case "FAIL":
      return <Badge variant="destructive">FAIL</Badge>;
    case "WARN":
      return <Badge variant="warning">WARN</Badge>;
    case "SKIP":
      return <Badge variant="outline">SKIP</Badge>;
  }
}

/**
 * Bucket a gate by its dotted prefix (config.*, db.*, auth.*, ...).
 * Groups make the report readable; ordering preserved within each.
 */
function groupGates(gates: DoctorGate[]): Array<[string, DoctorGate[]]> {
  const groups = new Map<string, DoctorGate[]>();
  for (const g of gates) {
    const prefix = g.name.split(".")[0] ?? "other";
    const list = groups.get(prefix) ?? [];
    list.push(g);
    groups.set(prefix, list);
  }
  return Array.from(groups.entries());
}

export default function DoctorPage() {
  const [report, setReport] = useState<DoctorResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await getDoctorReport());
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void run();
  }, [run]);

  const groups = report ? groupGates(report.gates) : [];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/providers"
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="h-3 w-3" /> Providers
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">Doctor</h1>
          <p className="text-sm text-muted-foreground">
            14-gate operator health check. Same gates as{" "}
            <code className="font-mono">pronaos-cli doctor</code>; surfaced
            here so non-shell operators get the same diagnostic.
          </p>
        </div>
        <Button
          onClick={() => void run()}
          disabled={loading}
          data-testid="run-button"
        >
          {loading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <RefreshCw className="h-4 w-4" />
          )}
          Run health check
        </Button>
      </div>

      {error ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="doctor-load-error">
              {error}
            </p>
          </CardContent>
        </Card>
      ) : null}

      {report === null && !error ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Running health check…
          </CardContent>
        </Card>
      ) : null}

      {report ? (
        <>
          {/* Summary tiles */}
          <div className="grid gap-4 md:grid-cols-4">
            <SummaryTile
              icon={CheckCircle2}
              label="Pass"
              value={report.summary.passed}
              testId="summary-pass"
              tint="success"
            />
            <SummaryTile
              icon={AlertCircle}
              label="Fail"
              value={report.summary.failed}
              testId="summary-fail"
              tint={report.summary.failed > 0 ? "destructive" : "muted"}
            />
            <SummaryTile
              icon={AlertTriangle}
              label="Warn"
              value={report.summary.warn}
              testId="summary-warn"
              tint={report.summary.warn > 0 ? "warning" : "muted"}
            />
            <SummaryTile
              icon={MinusCircle}
              label="Skip"
              value={report.summary.skip}
              testId="summary-skip"
              tint="muted"
            />
          </div>

          {/* Top-level verdict banner */}
          <Card data-testid="overall-verdict">
            <CardHeader>
              <div className="flex items-center gap-2">
                {report.has_fail ? (
                  <AlertCircle className="h-5 w-5 text-destructive" />
                ) : report.has_warn ? (
                  <AlertTriangle className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />
                ) : (
                  <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-400" />
                )}
                <CardTitle className="text-sm">
                  {report.has_fail
                    ? "One or more gates failing"
                    : report.has_warn
                      ? "Healthy — warnings present"
                      : "All gates passing"}
                </CardTitle>
              </div>
              <CardDescription>
                {report.summary.total} gate{report.summary.total === 1 ? "" : "s"}{" "}
                checked.
              </CardDescription>
            </CardHeader>
          </Card>

          {/* Grouped gate results */}
          {groups.map(([groupName, gates]) => (
            <Card key={groupName} data-testid={`group-${groupName}`}>
              <CardHeader>
                <CardTitle className="text-sm font-mono">
                  {groupName}
                </CardTitle>
                <CardDescription>
                  {gates.length} gate{gates.length === 1 ? "" : "s"}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <table className="w-full text-sm">
                  <tbody>
                    {gates.map((g) => (
                      <tr
                        key={g.name}
                        className="border-t align-top"
                        data-testid={`gate-${g.name}`}
                      >
                        <td className="w-8 py-2 pr-2">
                          {verdictIcon(g.verdict)}
                        </td>
                        <td className="py-2 pr-2 font-mono text-xs">
                          {g.name}
                        </td>
                        <td className="py-2 pr-2">{verdictBadge(g.verdict)}</td>
                        <td className="py-2 text-xs text-muted-foreground">
                          {g.detail || "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          ))}
        </>
      ) : null}
    </div>
  );
}

function SummaryTile({
  icon: Icon,
  label,
  value,
  testId,
  tint,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number;
  testId: string;
  tint: "success" | "destructive" | "warning" | "muted";
}) {
  const tintCls = {
    success: "text-green-600 dark:text-green-400",
    destructive: "text-destructive",
    warning: "text-yellow-600 dark:text-yellow-400",
    muted: "text-muted-foreground",
  }[tint];
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Icon className={`h-4 w-4 ${tintCls}`} />
          <CardTitle className="text-sm font-medium">{label}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <p
          className={`text-2xl font-semibold tabular-nums ${tintCls}`}
          data-testid={testId}
        >
          {value}
        </p>
      </CardContent>
    </Card>
  );
}
