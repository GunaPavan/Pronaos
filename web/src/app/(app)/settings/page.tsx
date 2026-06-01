"use client";

/**
 * /settings — gateway config viewer + per-tenant OIDC editor (Phase 71).
 *
 * Two sections:
 *   1. Gateway config cards — which optional features are active.
 *      Read-only; operators change these via environment variables.
 *   2. Per-tenant OIDC subject editor — set/clear the oidc_subject
 *      that links a tenant to an SSO identity (Phase 26).
 */
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Save,
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  getGatewaySettings,
  listTenants,
  updateTenant,
} from "@/lib/api/client";
import type { GatewaySettings, Tenant } from "@/lib/api/schemas";

type FeatureEntry = {
  key: keyof GatewaySettings;
  label: string;
  description: string;
};

const FEATURES: FeatureEntry[] = [
  { key: "redis_configured", label: "Redis", description: "Required for rate-limiting, singleflight, and agent-turn budgets." },
  { key: "semantic_cache_enabled", label: "Semantic cache", description: "Qdrant-backed L2 cache (Phase 7). Requires Redis + Qdrant." },
  { key: "anthropic_configured", label: "Anthropic", description: "Native Anthropic adapter (Claude family)." },
  { key: "groq_configured", label: "Groq", description: "OpenAI-compat adapter via Groq inference API." },
  { key: "openai_configured", label: "OpenAI", description: "OpenAI-compat adapter." },
  { key: "bedrock_configured", label: "AWS Bedrock", description: "SigV4-signed native Bedrock adapter (Phase 42)." },
  { key: "vertex_configured", label: "Google Vertex AI", description: "GCP service-account JWT auth (Phase 53)." },
  { key: "mcp_enabled", label: "MCP server", description: "Native MCP SSE + stdio server (Phase 48/50)." },
  { key: "presidio_enabled", label: "Presidio ML PII", description: "Presidio-powered ML PII detector (Phase 22). Requires presidio-analyzer + spaCy." },
  { key: "singleflight_distributed", label: "Distributed singleflight", description: "Redis-coordinated singleflight (Phase 36). Requires Redis." },
  { key: "oidc_configured", label: "OIDC/SSO", description: "OIDC admin auth (Phase 26). Set PRONAOS_OIDC_ISSUER to enable." },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<GatewaySettings | null>(null);
  const [settingsErr, setSettingsErr] = useState<string | null>(null);
  const [tenants, setTenants] = useState<Tenant[]>([]);

  const load = useCallback(async () => {
    setSettingsErr(null);
    try {
      const [s, ts] = await Promise.all([getGatewaySettings(), listTenants()]);
      setSettings(s);
      setTenants(ts);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setSettingsErr(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
          <p className="text-sm text-muted-foreground">
            Gateway runtime config (read-only) + per-tenant OIDC subject
            editor. Change env vars + restart to reconfigure features.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void load()}>
          <RefreshCw className="h-4 w-4" /> Reload
        </Button>
      </div>

      {settingsErr ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="settings-load-error">{settingsErr}</p>
          </CardContent>
        </Card>
      ) : null}

      {settings === null && !settingsErr ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </CardContent>
        </Card>
      ) : null}

      {settings ? (
        <>
          <GatewayConfigSection settings={settings} />
          <OidcSection tenants={tenants} />
        </>
      ) : null}
    </div>
  );
}

// =========================================================================== //
// Gateway config                                                              //
// =========================================================================== //

function GatewayConfigSection({ settings }: { settings: GatewaySettings }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Gateway configuration</CardTitle>
        <CardDescription>
          {settings.database_scheme
            ? `Database: ${settings.database_scheme}. `
            : ""}
          Change these via environment variables + restart.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 md:grid-cols-2" data-testid="config-grid">
          {FEATURES.map(({ key, label, description }) => {
            const active = Boolean(settings[key]);
            return (
              <div key={key} className="flex items-start gap-3 rounded-md border p-3">
                {active ? (
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-green-600 dark:text-green-400" />
                ) : (
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <div className="min-w-0 space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium">{label}</span>
                    <Badge
                      variant={active ? "success" : "outline"}
                      className="text-[10px]"
                      data-testid={`feature-${key}`}
                    >
                      {active ? "enabled" : "disabled"}
                    </Badge>
                    {key === "oidc_configured" && settings.oidc_issuer ? (
                      <span className="font-mono text-[10px] text-muted-foreground truncate max-w-[120px]">
                        {settings.oidc_issuer}
                      </span>
                    ) : null}
                  </div>
                  <p className="text-[11px] text-muted-foreground">{description}</p>
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// OIDC subject editor                                                         //
// =========================================================================== //

function OidcSection({ tenants }: { tenants: Tenant[] }) {
  const [selectedId, setSelectedId] = useState<string>(tenants[0]?.id ?? "");
  const [subject, setSubject] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [currentSubject, setCurrentSubject] = useState<string | null>(null);

  useEffect(() => {
    if (tenants[0]) setSelectedId(tenants[0].id);
  }, [tenants]);

  useEffect(() => {
    const t = tenants.find((t) => t.id === selectedId);
    const s = t?.oidc_subject ?? null;
    setCurrentSubject(s);
    setSubject(s ?? "");
  }, [selectedId, tenants]);

  async function save(): Promise<void> {
    if (!selectedId) return;
    setSaving(true);
    try {
      const updated = await updateTenant(selectedId, {
        oidc_subject: subject.trim() || null,
      });
      setCurrentSubject(updated.oidc_subject ?? null);
      setSubject(updated.oidc_subject ?? "");
      toast.success("OIDC subject saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">OIDC / SSO binding</CardTitle>
        <CardDescription>
          Set the per-tenant OIDC subject claim that maps an SSO identity to
          this tenant. Requires <code className="font-mono">PRONAOS_OIDC_ISSUER</code> to
          be configured at the gateway level.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="oidc-tenant" className="text-xs">Tenant</Label>
          <select
            id="oidc-tenant"
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-transparent px-3 text-sm"
            data-testid="oidc-tenant-select"
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="oidc-subject" className="text-xs">
            OIDC subject
            {currentSubject ? (
              <Badge variant="success" className="ml-2 text-[10px]">set</Badge>
            ) : (
              <Badge variant="outline" className="ml-2 text-[10px]">not set</Badge>
            )}
          </Label>
          <Input
            id="oidc-subject"
            type="text"
            placeholder="e.g. auth0|abc123 — leave empty to clear"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            data-testid="oidc-subject-input"
          />
          <p className="text-[11px] text-muted-foreground">
            Empty input clears the OIDC binding. The exact value must match the
            <code className="mx-1 font-mono">sub</code> claim from your IdP.
          </p>
        </div>
        <div className="flex justify-end">
          <Button
            onClick={() => void save()}
            disabled={saving || !selectedId}
            data-testid="oidc-save-button"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
