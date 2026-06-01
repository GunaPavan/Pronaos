"use client";

/**
 * /guardrails/audit — hash-chained audit log viewer + verify CTA.
 * Phase 67.
 *
 * Layout:
 *   - Top: tenant picker (audit chains are per-tenant) + verify button
 *   - Verdict card: pass/fail banner with break details when failed
 *   - Records table: paginated audit_records with chain pointers
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  Loader2,
  ShieldCheck,
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
import {
  ApiError,
  listAuditRecords,
  listTenants,
  verifyAuditChain,
} from "@/lib/api/client";
import type {
  AuditListResponse,
  AuditVerifyResponse,
  Tenant,
} from "@/lib/api/schemas";

const PAGE_SIZE = 25;

export default function AuditPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantsErr, setTenantsErr] = useState<string | null>(null);
  const [selectedTenantId, setSelectedTenantId] = useState<string>("");
  const [records, setRecords] = useState<AuditListResponse | null>(null);
  const [recordsErr, setRecordsErr] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [verifying, setVerifying] = useState(false);
  const [verdict, setVerdict] = useState<AuditVerifyResponse | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const ts = await listTenants();
        setTenants(ts);
        if (ts[0]) setSelectedTenantId(ts[0].id);
      } catch (err) {
        setTenantsErr(err instanceof Error ? err.message : "Unknown error");
      }
    })();
  }, []);

  const refresh = useCallback(
    async (tenantId: string, offsetVal: number) => {
      if (!tenantId) {
        setRecords(null);
        return;
      }
      setRecordsErr(null);
      try {
        setRecords(
          await listAuditRecords(tenantId, { limit: PAGE_SIZE, offset: offsetVal }),
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error";
        setRecordsErr(msg);
        if (err instanceof ApiError && err.status === 403) {
          toast.error("This key lacks the admin:usage scope");
        }
      }
    },
    [],
  );

  useEffect(() => {
    void refresh(selectedTenantId, offset);
  }, [selectedTenantId, offset, refresh]);

  // Reset paging + verdict when the tenant changes.
  useEffect(() => {
    setOffset(0);
    setVerdict(null);
  }, [selectedTenantId]);

  async function runVerify(): Promise<void> {
    if (!selectedTenantId) return;
    setVerifying(true);
    setVerdict(null);
    try {
      const v = await verifyAuditChain(selectedTenantId);
      setVerdict(v);
      if (v.is_intact) toast.success("Chain intact");
      else toast.error(`Chain broken: ${v.breaks.length} break(s) detected`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Verify failed");
    } finally {
      setVerifying(false);
    }
  }

  const totalPages = records ? Math.ceil(records.total / PAGE_SIZE) : 0;
  const currentPage = records ? Math.floor(records.offset / PAGE_SIZE) + 1 : 1;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/guardrails"
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="h-3 w-3" /> Guardrails
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">Audit log</h1>
          <p className="text-sm text-muted-foreground">
            Hash-chained per-tenant. Each record links to the previous via
            <code className="mx-1 font-mono">prev_hash</code> /{" "}
            <code className="font-mono">this_hash</code>. The verifier walks the
            whole chain and reports tampered records.
          </p>
        </div>
        <Button
          onClick={() => {
            void runVerify();
          }}
          disabled={verifying || !selectedTenantId}
          data-testid="verify-button"
        >
          {verifying ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <ShieldCheck className="h-4 w-4" />
          )}
          Verify chain
        </Button>
      </div>

      {tenantsErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive">{tenantsErr}</p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Tenant</CardTitle>
        </CardHeader>
        <CardContent>
          <select
            value={selectedTenantId}
            onChange={(e) => setSelectedTenantId(e.target.value)}
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-transparent px-3 text-sm"
            data-testid="tenant-select"
          >
            <option value="">Select a tenant…</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </CardContent>
      </Card>

      {verdict ? <VerdictCard verdict={verdict} /> : null}

      {recordsErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="audit-load-error">
              {recordsErr}
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Records</CardTitle>
          <CardDescription>
            {records
              ? `${records.total} record${records.total === 1 ? "" : "s"} for this tenant. Oldest-first; prev_hash on row N matches this_hash on row N-1.`
              : "Loading…"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {records === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : records.items.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="audit-empty">
              No audit records yet. The first chat completion for this tenant
              will append the genesis record.
            </p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-xs" data-testid="audit-table">
                  <thead className="text-left text-[10px] uppercase text-muted-foreground">
                    <tr>
                      <th className="px-2 py-2">Time</th>
                      <th className="px-2 py-2">Team</th>
                      <th className="px-2 py-2">Provider/Model</th>
                      <th className="px-2 py-2">Request</th>
                      <th className="px-2 py-2">prev_hash</th>
                      <th className="px-2 py-2">this_hash</th>
                    </tr>
                  </thead>
                  <tbody>
                    {records.items.map((r) => (
                      <tr key={r.id} className="border-t">
                        <td className="px-2 py-2 text-[11px] text-muted-foreground">
                          {new Date(r.ts).toLocaleString()}
                        </td>
                        <td className="px-2 py-2 font-mono">
                          {r.team_id.slice(0, 8)}
                        </td>
                        <td className="px-2 py-2 font-mono">
                          {r.provider}/{r.model}
                        </td>
                        <td className="px-2 py-2 font-mono text-[10px]">
                          {r.request_id ?? "—"}
                        </td>
                        <td className="px-2 py-2 font-mono text-[10px]">
                          {r.prev_hash ? r.prev_hash.slice(0, 12) + "…" : "(genesis)"}
                        </td>
                        <td className="px-2 py-2 font-mono text-[10px]">
                          {r.this_hash.slice(0, 12)}…
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-3 flex items-center justify-between text-xs">
                <span className="text-muted-foreground">
                  Page {currentPage} of {Math.max(1, totalPages)}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Prev
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + PAGE_SIZE >= records.total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function VerdictCard({ verdict }: { verdict: AuditVerifyResponse }) {
  return (
    <Card data-testid="verdict-card">
      <CardHeader>
        <div className="flex items-center gap-2">
          {verdict.is_intact ? (
            <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-400" />
          ) : (
            <AlertTriangle className="h-5 w-5 text-destructive" />
          )}
          <CardTitle className="text-sm">
            {verdict.is_intact ? "Chain intact" : "Chain broken"}
          </CardTitle>
        </div>
        <CardDescription>
          {verdict.verified_records} of {verdict.total_records} records verified.
          {verdict.is_intact
            ? " No tamper points detected."
            : ` ${verdict.breaks.length} break point${verdict.breaks.length === 1 ? "" : "s"} detected.`}
        </CardDescription>
      </CardHeader>
      {!verdict.is_intact && verdict.breaks.length > 0 ? (
        <CardContent>
          <table className="w-full text-xs" data-testid="breaks-table">
            <thead className="text-left text-[10px] uppercase text-muted-foreground">
              <tr>
                <th className="px-2 py-2">Record</th>
                <th className="px-2 py-2">Time</th>
                <th className="px-2 py-2">Reason</th>
                <th className="px-2 py-2">Expected</th>
                <th className="px-2 py-2">Actual</th>
              </tr>
            </thead>
            <tbody>
              {verdict.breaks.map((b) => (
                <tr key={b.record_id} className="border-t">
                  <td className="px-2 py-2 font-mono">
                    {b.record_id.slice(0, 12)}…
                  </td>
                  <td className="px-2 py-2 text-[11px] text-muted-foreground">
                    {new Date(b.ts_iso).toLocaleString()}
                  </td>
                  <td className="px-2 py-2">
                    <Badge variant="destructive">{b.reason}</Badge>
                  </td>
                  <td className="px-2 py-2 font-mono text-[10px]">
                    {b.expected_hash
                      ? b.expected_hash.slice(0, 12) + "…"
                      : "(empty)"}
                  </td>
                  <td className="px-2 py-2 font-mono text-[10px]">
                    {b.actual_hash
                      ? b.actual_hash.slice(0, 12) + "…"
                      : "(empty)"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      ) : null}
    </Card>
  );
}
