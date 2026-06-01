"use client";

/**
 * /teams — list teams across all tenants, with optional tenant filter
 * + create-team modal. Phase 63 (Identity).
 *
 * Editing per-team budgets / routing strategy / etc. lands in later
 * phases (64 onward); Phase 63 ships the bare CRUD.
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
  createTeam,
  deleteTeam,
  listTeams,
  listTenants,
} from "@/lib/api/client";
import type { Team, Tenant } from "@/lib/api/schemas";

function fmtDate(epoch: number): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

export default function TeamsPage() {
  const [teams, setTeams] = useState<Team[] | null>(null);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [filterTenantId, setFilterTenantId] = useState<string>("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Team | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const [t, ts] = await Promise.all([
        listTeams(filterTenantId ? { tenant_id: filterTenantId } : {}),
        listTenants(),
      ]);
      setTeams(t);
      setTenants(ts);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:identity scope");
      }
    }
  }, [filterTenantId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const tenantName = useCallback(
    (id: string): string => {
      const t = tenants.find((x) => x.id === id);
      return t?.name ?? id;
    },
    [tenants],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Teams</h1>
          <p className="text-sm text-muted-foreground">
            Teams group API keys + per-team policies (budgets, routing,
            guardrails). Each team belongs to exactly one tenant.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)} disabled={tenants.length === 0}>
          <Plus className="h-4 w-4" />
          New team
        </Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-4">
            <div>
              <CardTitle>All teams</CardTitle>
              <CardDescription>
                {teams === null
                  ? "Loading…"
                  : `${teams.length} team${teams.length === 1 ? "" : "s"}`}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <Label htmlFor="filter-tenant" className="text-xs">
                Filter by tenant
              </Label>
              <select
                id="filter-tenant"
                value={filterTenantId}
                onChange={(e) => setFilterTenantId(e.target.value)}
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
              >
                <option value="">All tenants</option>
                {tenants.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {loadError ? (
            <p className="text-sm text-destructive" data-testid="teams-load-error">
              {loadError}
            </p>
          ) : teams === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading teams…
            </div>
          ) : teams.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No teams match the current filter.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="teams-table">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-2 py-2">Name</th>
                    <th className="px-2 py-2">Tenant</th>
                    <th className="px-2 py-2">ID</th>
                    <th className="px-2 py-2">Created</th>
                    <th className="px-2 py-2 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {teams.map((t) => (
                    <tr key={t.id} className="border-t" data-testid={`team-row-${t.id}`}>
                      <td className="px-2 py-2 font-medium">{t.name}</td>
                      <td className="px-2 py-2 text-xs">{tenantName(t.tenant_id)}</td>
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

      <CreateTeamDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        tenants={tenants}
        defaultTenantId={filterTenantId}
        onCreated={() => void refresh()}
      />

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete team?</DialogTitle>
            <DialogDescription>
              This permanently removes <strong>{pendingDelete?.name}</strong>{" "}
              and cascades to all its API keys. This cannot be undone.
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
                  await deleteTeam(pendingDelete.id);
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

function CreateTeamDialog({
  open,
  onOpenChange,
  tenants,
  defaultTenantId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  tenants: Tenant[];
  defaultTenantId: string;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setTenantId(defaultTenantId || tenants[0]?.id || "");
      setSubmitting(false);
    }
  }, [open, defaultTenantId, tenants]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!name.trim() || !tenantId) return;
    setSubmitting(true);
    try {
      const created = await createTeam({ tenant_id: tenantId, name: name.trim() });
      toast.success(`Created team ${created.name}`);
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
          <DialogTitle>New team</DialogTitle>
          <DialogDescription>
            Teams group API keys + per-team policies under a tenant.
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={onSubmit}>
          <div className="space-y-2">
            <Label htmlFor="team-tenant">Tenant</Label>
            <select
              id="team-tenant"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
              required
            >
              <option value="">Select a tenant…</option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="team-name">Name</Label>
            <Input
              id="team-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="engineering"
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
            <Button
              type="submit"
              disabled={submitting || !name.trim() || !tenantId}
            >
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
