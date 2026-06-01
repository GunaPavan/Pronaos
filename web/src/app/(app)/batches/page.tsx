"use client";

/**
 * /batches — async batch console (Phase 69).
 *
 * Lists all batches across teams. Filters by team + status.
 * Per-row click navigates to /batches/[id] for detail.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Loader2, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
  listAdminBatches,
  listTeams,
} from "@/lib/api/client";
import type { BatchListResponse, BatchStatus, Team } from "@/lib/api/schemas";
import { BATCH_STATUSES } from "@/lib/api/schemas";
// Note: batch rows show request counts but not per-batch cost (that
// comes from usage_records); the column is kept for future extension.

function statusBadge(status: string) {
  switch (status) {
    case "completed":
      return <Badge variant="success">completed</Badge>;
    case "in_progress":
    case "finalizing":
      return <Badge variant="warning">{status}</Badge>;
    case "failed":
    case "expired":
    case "cancelled":
      return <Badge variant="destructive">{status}</Badge>;
    default:
      return <Badge variant="outline">{status}</Badge>;
  }
}

const PAGE_SIZE = 25;

export default function BatchesPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamFilter, setTeamFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<BatchStatus | "">("");
  const [batches, setBatches] = useState<BatchListResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    void (async () => {
      try {
        setTeams(await listTeams());
      } catch {
        /* non-fatal — filter just shows IDs */
      }
    })();
  }, []);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      setBatches(
        await listAdminBatches({
          team_id: teamFilter || undefined,
          status: (statusFilter as BatchStatus) || undefined,
          limit: PAGE_SIZE,
          offset,
        }),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, [teamFilter, statusFilter, offset]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Reset paging on filter change.
  useEffect(() => {
    setOffset(0);
  }, [teamFilter, statusFilter]);

  const totalPages = batches ? Math.ceil(batches.total / PAGE_SIZE) : 0;
  const currentPage = batches ? Math.floor(batches.offset / PAGE_SIZE) + 1 : 1;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Batches</h1>
          <p className="text-sm text-muted-foreground">
            Async batch jobs at 50% pricing (Phase 59/60). Click a row for
            detail + cancel.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void refresh()}>
          <RefreshCw className="h-4 w-4" />
          Reload
        </Button>
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 py-4">
          <div className="space-y-1">
            <Label htmlFor="team-filter" className="text-xs">Team</Label>
            <select
              id="team-filter"
              value={teamFilter}
              onChange={(e) => setTeamFilter(e.target.value)}
              className="flex h-9 rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="">All teams</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="status-filter" className="text-xs">Status</Label>
            <select
              id="status-filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as BatchStatus | "")}
              className="flex h-9 rounded-md border border-input bg-transparent px-3 text-sm"
              data-testid="status-filter"
            >
              <option value="">All statuses</option>
              {BATCH_STATUSES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        </CardContent>
      </Card>

      {loadError ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="batches-load-error">
              {loadError}
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Jobs</CardTitle>
          <CardDescription>
            {batches
              ? `${batches.total} batch${batches.total === 1 ? "" : "es"} total. Newest first.`
              : "Loading…"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {batches === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : batches.items.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="batches-empty">
              No batches in this view. Submit a batch via the SDK or CLI to
              see it here.
            </p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="batches-table">
                  <thead className="text-left text-xs uppercase text-muted-foreground">
                    <tr>
                      <th className="px-2 py-2">ID</th>
                      <th className="px-2 py-2">Provider</th>
                      <th className="px-2 py-2">Status</th>
                      <th className="px-2 py-2 text-right">Requests</th>
                      <th className="px-2 py-2 text-right">Cost</th>
                      <th className="px-2 py-2">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batches.items.map((b) => (
                      <tr
                        key={b.id}
                        className="cursor-pointer border-t hover:bg-accent"
                        onClick={() => {
                          window.location.href = `/batches/${b.id}`;
                        }}
                        data-testid={`batch-row-${b.id}`}
                      >
                        <td className="px-2 py-2 font-mono text-xs">
                          <Link
                            href={`/batches/${b.id}`}
                            className="hover:underline"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {b.id.slice(0, 20)}…
                          </Link>
                        </td>
                        <td className="px-2 py-2 text-xs">{b.provider}</td>
                        <td className="px-2 py-2">{statusBadge(b.status)}</td>
                        <td className="px-2 py-2 text-right tabular-nums">
                          {b.request_counts.completed}/{b.request_counts.total}
                        </td>
                        <td className="px-2 py-2 text-right tabular-nums">
                          —
                        </td>
                        <td className="px-2 py-2 text-xs text-muted-foreground">
                          {new Date(b.created_at * 1000).toLocaleString()}
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
                    disabled={offset + PAGE_SIZE >= batches.total}
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
