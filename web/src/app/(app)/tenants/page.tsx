"use client";

/**
 * /tenants — list + create + delete tenants.
 * Phase 63 (Identity). Calls /v1/admin/tenants under the hood.
 *
 * Requires an API key with the ``admin:identity`` scope. 403s
 * surface as a clear toast pointing the operator at the CLI to
 * bootstrap such a key (``pronaos-cli key issue --scopes 'admin:identity'``).
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Plus, Trash2, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  createTenant,
  deleteTenant,
  listTenants,
} from "@/lib/api/client";
import type { Tenant } from "@/lib/api/schemas";

function fmtDate(epoch: number): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

export default function TenantsPage() {
  const [tenants, setTenants] = useState<Tenant[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Tenant | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const rows = await listTenants();
      setTenants(rows);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error(
          "This key lacks the admin:identity scope — bootstrap one via the CLI",
        );
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Tenants</h1>
          <p className="text-sm text-muted-foreground">
            Top-level organizations. Teams + API keys live underneath.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          New tenant
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All tenants</CardTitle>
          <CardDescription>
            {tenants === null
              ? "Loading…"
              : `${tenants.length} tenant${tenants.length === 1 ? "" : "s"}`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loadError ? (
            <p className="text-sm text-destructive" data-testid="tenants-load-error">
              {loadError}
            </p>
          ) : tenants === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading tenants…
            </div>
          ) : tenants.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No tenants yet. Click <em>New tenant</em> to create the first one.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="tenants-table">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-2 py-2">Name</th>
                    <th className="px-2 py-2">ID</th>
                    <th className="px-2 py-2">Created</th>
                    <th className="px-2 py-2 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {tenants.map((t) => (
                    <tr key={t.id} className="border-t" data-testid={`tenant-row-${t.id}`}>
                      <td className="px-2 py-2 font-medium">{t.name}</td>
                      <td className="px-2 py-2 font-mono text-xs text-muted-foreground">
                        {t.id}
                      </td>
                      <td className="px-2 py-2 text-xs text-muted-foreground">
                        {fmtDate(t.created_at)}
                      </td>
                      <td className="px-2 py-2 text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Delete ${t.name}`}
                          onClick={() => setPendingDelete(t)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Create modal */}
      <CreateTenantDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => void refresh()}
      />

      {/* Delete confirmation */}
      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete tenant?</DialogTitle>
            <DialogDescription>
              This permanently removes <strong>{pendingDelete?.name}</strong>{" "}
              and cascades to all its teams + API keys. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingDelete(null)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={async () => {
                if (!pendingDelete) return;
                setBusy(true);
                try {
                  await deleteTenant(pendingDelete.id);
                  toast.success(`Deleted ${pendingDelete.name}`);
                  setPendingDelete(null);
                  await refresh();
                } catch (err) {
                  toast.error(
                    err instanceof Error ? err.message : "Delete failed",
                  );
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Deleting…
                </>
              ) : (
                "Delete"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function CreateTenantDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Reset state whenever the dialog opens so previous attempts don't bleed in.
  useEffect(() => {
    if (open) {
      setName("");
      setSubmitting(false);
    }
  }, [open]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    try {
      const created = await createTenant(name.trim());
      toast.success(`Created tenant ${created.name}`);
      onCreated();
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Create failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New tenant</DialogTitle>
          <DialogDescription>
            Tenants are the top-level isolation boundary in Pronaos. Each tenant
            owns its teams, audit log, and webhook configuration.
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={onSubmit}>
          <div className="space-y-2">
            <Label htmlFor="tenant-name">Name</Label>
            <Input
              id="tenant-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="acme-corp"
              autoFocus
              required
            />
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || !name.trim()}>
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Creating…
                </>
              ) : (
                "Create"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
