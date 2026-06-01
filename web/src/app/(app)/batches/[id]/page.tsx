"use client";

/**
 * /batches/[id] — per-batch detail page (Phase 69).
 *
 * Shows status, request counts, cost, timeline, cancel CTA.
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  ChevronLeft,
  Loader2,
  XCircle,
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
  cancelAdminBatch,
  getAdminBatch,
} from "@/lib/api/client";
import type { BatchInfo } from "@/lib/api/schemas";
import { formatHcents, formatTokens } from "@/lib/format";

const TERMINAL = new Set(["completed", "failed", "expired", "cancelled"]);

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

export default function BatchDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;
  const [batch, setBatch] = useState<BatchInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setBatch(await getAdminBatch(id));
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error";
        setLoadError(msg);
      }
    })();
  }, [id]);

  async function cancel(): Promise<void> {
    setCancelling(true);
    try {
      const updated = await cancelAdminBatch(id);
      setBatch(updated);
      toast.success("Batch cancelled");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Cancel failed");
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/batches"
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="h-3 w-3" /> Batches
          </Link>
          <h1 className="font-mono text-lg font-semibold tracking-tight break-all">
            {id}
          </h1>
        </div>
        {batch && !TERMINAL.has(batch.status) ? (
          <Button
            variant="destructive"
            size="sm"
            disabled={cancelling}
            onClick={() => void cancel()}
            data-testid="cancel-button"
          >
            {cancelling ? <Loader2 className="h-4 w-4 animate-spin" /> : <XCircle className="h-4 w-4" />}
            Cancel batch
          </Button>
        ) : null}
      </div>

      {loadError ? (
        <Card>
          <CardContent className="py-6">
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" />
              {loadError}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {batch === null && !loadError ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading batch…
          </CardContent>
        </Card>
      ) : null}

      {batch ? (
        <>
          {/* Status card */}
          <Card data-testid="batch-status-card">
            <CardHeader>
              <div className="flex items-center gap-3">
                {statusBadge(batch.status)}
                <CardTitle className="text-sm">
                  {batch.provider} — {batch.endpoint}
                </CardTitle>
              </div>
              <CardDescription>
                Completion window: {batch.completion_window}.
                {batch.provider_batch_id
                  ? ` Upstream ID: ${batch.provider_batch_id}`
                  : ""}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 md:grid-cols-3">
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-xs text-muted-foreground">Requests</p>
                  <p className="text-xl font-semibold tabular-nums">
                    {batch.request_counts.completed}
                    <span className="text-sm text-muted-foreground">
                      /{batch.request_counts.total}
                    </span>
                  </p>
                  {batch.request_counts.failed > 0 ? (
                    <p className="text-xs text-destructive">
                      {batch.request_counts.failed} failed
                    </p>
                  ) : null}
                </div>
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-xs text-muted-foreground">Created</p>
                  <p className="text-sm tabular-nums">
                    {new Date(batch.created_at * 1000).toLocaleString()}
                  </p>
                  {batch.completed_at ? (
                    <p className="text-xs text-muted-foreground">
                      Completed{" "}
                      {new Date(batch.completed_at * 1000).toLocaleString()}
                    </p>
                  ) : null}
                </div>
                <div className="space-y-1 rounded-md border p-3">
                  <p className="text-xs text-muted-foreground">Status</p>
                  <div>{statusBadge(batch.status)}</div>
                  {batch.error_message ? (
                    <p className="text-xs text-destructive">
                      {batch.error_message}
                    </p>
                  ) : null}
                </div>
              </div>
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}
