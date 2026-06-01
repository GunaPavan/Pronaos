"use client";

/**
 * /keys — list API keys + generate-once modal + revoke.
 * Phase 63 (Identity).
 *
 * Critical UX invariant: when a key is generated, its full secret is
 * shown EXACTLY ONCE in a "save this now" modal with a copy button.
 * Subsequent GETs from the backend return only the prefix — the
 * gateway stores an argon2 hash, so the secret cannot be recovered
 * after this response.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { CheckCircle2, Copy, Loader2, Plus, Trash2 } from "lucide-react";

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
  generateKey,
  listKeys,
  listTeams,
  revokeKey,
} from "@/lib/api/client";
import type { ApiKey, ApiKeyWithSecret, Team } from "@/lib/api/schemas";

const KNOWN_SCOPES = [
  "chat:write",
  "admin:usage",
  "admin:identity",
];

function fmtDate(epoch: number | null): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

export default function KeysPage() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [showRevoked, setShowRevoked] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [generated, setGenerated] = useState<ApiKeyWithSecret | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<ApiKey | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const [k, t] = await Promise.all([
        listKeys({ include_revoked: showRevoked }),
        listTeams(),
      ]);
      setKeys(k);
      setTeams(t);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:identity scope");
      }
    }
  }, [showRevoked]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const teamName = useCallback(
    (id: string): string => teams.find((t) => t.id === id)?.name ?? id,
    [teams],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">API keys</h1>
          <p className="text-sm text-muted-foreground">
            Bearer tokens. Each key belongs to a team and carries a fixed set
            of scopes. The full secret is shown exactly once at generation —
            after that, only the prefix is recoverable.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)} disabled={teams.length === 0}>
          <Plus className="h-4 w-4" />
          Generate key
        </Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-4">
            <div>
              <CardTitle>All keys</CardTitle>
              <CardDescription>
                {keys === null
                  ? "Loading…"
                  : `${keys.length} key${keys.length === 1 ? "" : "s"}`}
              </CardDescription>
            </div>
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={showRevoked}
                onChange={(e) => setShowRevoked(e.target.checked)}
                className="h-4 w-4 rounded border-input"
              />
              Show revoked
            </label>
          </div>
        </CardHeader>
        <CardContent>
          {loadError ? (
            <p className="text-sm text-destructive" data-testid="keys-load-error">
              {loadError}
            </p>
          ) : keys === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading keys…
            </div>
          ) : keys.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No keys to display.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="keys-table">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-2 py-2">Label</th>
                    <th className="px-2 py-2">Team</th>
                    <th className="px-2 py-2">Prefix</th>
                    <th className="px-2 py-2">Scopes</th>
                    <th className="px-2 py-2">Status</th>
                    <th className="px-2 py-2">Created</th>
                    <th className="px-2 py-2 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.id} className="border-t" data-testid={`key-row-${k.id}`}>
                      <td className="px-2 py-2 font-medium">
                        {k.label || <span className="text-muted-foreground">(no label)</span>}
                      </td>
                      <td className="px-2 py-2 text-xs">{teamName(k.team_id)}</td>
                      <td className="px-2 py-2 font-mono text-xs">{k.prefix}</td>
                      <td className="px-2 py-2 text-xs">
                        {k.scopes.map((s) => (
                          <span
                            key={s}
                            className="mr-1 inline-block rounded bg-muted px-1.5 py-0.5"
                          >
                            {s}
                          </span>
                        ))}
                      </td>
                      <td className="px-2 py-2 text-xs">
                        <span
                          className={
                            k.status === "active"
                              ? "rounded bg-green-100 px-1.5 py-0.5 text-green-700 dark:bg-green-900/40 dark:text-green-300"
                              : "rounded bg-muted px-1.5 py-0.5 text-muted-foreground"
                          }
                        >
                          {k.status}
                        </span>
                      </td>
                      <td className="px-2 py-2 text-xs text-muted-foreground">
                        {fmtDate(k.created_at)}
                      </td>
                      <td className="px-2 py-2 text-right">
                        {k.status === "active" ? (
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Revoke ${k.prefix}`}
                            onClick={() => setPendingRevoke(k)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <GenerateKeyDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        teams={teams}
        onGenerated={(key) => {
          setGenerated(key);
          void refresh();
        }}
      />

      {/* Show-once secret modal */}
      <Dialog
        open={generated !== null}
        onOpenChange={(open) => {
          if (!open) setGenerated(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-500" />
              <DialogTitle>Key created</DialogTitle>
            </div>
            <DialogDescription>
              This is the <strong>only</strong> time the secret will be shown.
              Copy it now — losing it means issuing a new one.
            </DialogDescription>
          </DialogHeader>
          {generated ? (
            <div className="space-y-3">
              <div className="rounded-md bg-muted p-3 font-mono text-xs break-all" data-testid="generated-secret">
                {generated.api_key}
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-muted-foreground">ID</span>
                  <p className="font-mono">{generated.id}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Prefix</span>
                  <p className="font-mono">{generated.prefix}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Label</span>
                  <p>{generated.label || "—"}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Scopes</span>
                  <p>{generated.scopes.join(", ")}</p>
                </div>
              </div>
              <Button
                className="w-full"
                onClick={() => {
                  void navigator.clipboard
                    .writeText(generated.api_key)
                    .then(() => toast.success("Copied to clipboard"))
                    .catch(() => toast.error("Could not access clipboard"));
                }}
              >
                <Copy className="h-4 w-4" />
                Copy key
              </Button>
            </div>
          ) : null}
          <DialogFooter>
            <Button onClick={() => setGenerated(null)}>
              I have saved this key
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Revoke confirmation */}
      <Dialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRevoke(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke key?</DialogTitle>
            <DialogDescription>
              Subsequent requests using this key will return 401. The audit
              trail is preserved (soft delete). This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingRevoke(null)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={async () => {
                if (!pendingRevoke) return;
                setBusy(true);
                try {
                  await revokeKey(pendingRevoke.id);
                  toast.success("Key revoked");
                  setPendingRevoke(null);
                  await refresh();
                } catch (err) {
                  toast.error(
                    err instanceof Error ? err.message : "Revoke failed",
                  );
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Revoking…
                </>
              ) : (
                "Revoke"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function GenerateKeyDialog({
  open,
  onOpenChange,
  teams,
  onGenerated,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  teams: Team[];
  onGenerated: (key: ApiKeyWithSecret) => void;
}) {
  const [teamId, setTeamId] = useState("");
  const [label, setLabel] = useState("");
  const [scopes, setScopes] = useState<string[]>(["chat:write"]);
  const [env, setEnv] = useState<"live" | "test">("live");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setTeamId(teams[0]?.id ?? "");
      setLabel("");
      setScopes(["chat:write"]);
      setEnv("live");
      setSubmitting(false);
    }
  }, [open, teams]);

  function toggleScope(s: string) {
    setScopes((cur) =>
      cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s],
    );
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!teamId || scopes.length === 0) return;
    setSubmitting(true);
    try {
      const result = await generateKey({
        team_id: teamId,
        label: label || undefined,
        scopes,
        env,
      });
      onGenerated(result);
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Generate failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Generate API key</DialogTitle>
          <DialogDescription>
            The full secret will be shown once after creation. Save it
            immediately — the gateway stores only an argon2 hash.
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={onSubmit}>
          <div className="space-y-2">
            <Label htmlFor="key-team">Team</Label>
            <select
              id="key-team"
              value={teamId}
              onChange={(e) => setTeamId(e.target.value)}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
              required
            >
              <option value="">Select a team…</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="key-label">Label (optional)</Label>
            <Input
              id="key-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="ci-bot"
            />
          </div>
          <div className="space-y-2">
            <Label>Scopes</Label>
            <div className="flex flex-wrap gap-2">
              {KNOWN_SCOPES.map((s) => (
                <label
                  key={s}
                  className="flex items-center gap-2 rounded-md border border-input px-2 py-1 text-xs"
                >
                  <input
                    type="checkbox"
                    checked={scopes.includes(s)}
                    onChange={() => toggleScope(s)}
                    className="h-3 w-3"
                  />
                  <span className="font-mono">{s}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="key-env">Environment</Label>
            <select
              id="key-env"
              value={env}
              onChange={(e) => setEnv(e.target.value as "live" | "test")}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="live">live</option>
              <option value="test">test</option>
            </select>
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
              disabled={submitting || !teamId || scopes.length === 0}
            >
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Generating…
                </>
              ) : (
                "Generate"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
