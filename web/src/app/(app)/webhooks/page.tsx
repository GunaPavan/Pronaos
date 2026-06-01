"use client";

/**
 * /webhooks — webhook console (Phase 70).
 *
 * Layout:
 *   - Tenant picker
 *   - Config card: URL input + secret input + Save button
 *   - "Send test ping" CTA + inline result display
 */
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RefreshCw,
  Save,
  Send,
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
  getWebhook,
  listTenants,
  testWebhook,
  updateWebhook,
} from "@/lib/api/client";
import type { Tenant, WebhookConfig, WebhookTestResult } from "@/lib/api/schemas";

export default function WebhooksPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantsErr, setTenantsErr] = useState<string | null>(null);
  const [selectedTenantId, setSelectedTenantId] = useState<string>("");
  const [config, setConfig] = useState<WebhookConfig | null>(null);
  const [configErr, setConfigErr] = useState<string | null>(null);

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

  const refresh = useCallback(async (tenantId: string) => {
    if (!tenantId) { setConfig(null); return; }
    setConfigErr(null);
    setConfig(null);
    try {
      setConfig(await getWebhook(tenantId));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setConfigErr(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, []);

  useEffect(() => { void refresh(selectedTenantId); }, [selectedTenantId, refresh]);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Webhooks</h1>
          <p className="text-sm text-muted-foreground">
            Per-tenant HMAC-signed webhook config. Events: quota.exhausted,
            circuit.tripped, audit.chain_broken.
          </p>
        </div>
        {selectedTenantId ? (
          <Button variant="outline" size="sm" onClick={() => void refresh(selectedTenantId)}>
            <RefreshCw className="h-4 w-4" /> Reload
          </Button>
        ) : null}
      </div>

      {tenantsErr ? (
        <Card><CardContent className="py-6"><p className="text-sm text-destructive">{tenantsErr}</p></CardContent></Card>
      ) : null}

      <Card>
        <CardHeader><CardTitle className="text-sm">Tenant</CardTitle></CardHeader>
        <CardContent>
          <select
            value={selectedTenantId}
            onChange={(e) => setSelectedTenantId(e.target.value)}
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-transparent px-3 text-sm"
            data-testid="tenant-select"
          >
            <option value="">Select a tenant…</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
        </CardContent>
      </Card>

      {configErr ? (
        <Card><CardContent className="py-6">
          <p className="text-sm text-destructive" data-testid="webhook-load-error">{configErr}</p>
        </CardContent></Card>
      ) : null}

      {selectedTenantId && config === null && !configErr ? (
        <Card><CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading webhook config…
        </CardContent></Card>
      ) : null}

      {config ? (
        <>
          <WebhookConfigEditor
            config={config}
            onSaved={setConfig}
          />
          <TestPingCard tenantId={config.tenant_id} hasWebhook={!!config.url} />
        </>
      ) : null}
    </div>
  );
}

// =========================================================================== //
// Config editor                                                               //
// =========================================================================== //

function WebhookConfigEditor({
  config,
  onSaved,
}: {
  config: WebhookConfig;
  onSaved: (next: WebhookConfig) => void;
}) {
  const [url, setUrl] = useState(config.url ?? "");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setUrl(config.url ?? "");
    setSecret("");
  }, [config.url]);

  async function save(): Promise<void> {
    setSaving(true);
    try {
      const next = await updateWebhook(config.tenant_id, {
        url: url.trim() || null,
        secret: secret.trim() || null,
      });
      onSaved(next);
      setSecret("");
      toast.success("Webhook config saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function clear(): Promise<void> {
    setSaving(true);
    try {
      const next = await updateWebhook(config.tenant_id, { url: null, secret: null });
      onSaved(next);
      setUrl("");
      setSecret("");
      toast.success("Webhook cleared");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Clear failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-sm">Configuration</CardTitle>
            <CardDescription>
              {config.url ? (
                <span className="flex items-center gap-1">
                  <CheckCircle2 className="h-3 w-3 text-green-600 dark:text-green-400" />
                  Configured
                  {config.url && (
                    <a
                      href={config.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="ml-1 font-mono text-[10px] hover:underline"
                    >
                      {config.url.length > 50 ? config.url.slice(0, 50) + "…" : config.url}
                      <ExternalLink className="ml-0.5 inline h-2.5 w-2.5" />
                    </a>
                  )}
                </span>
              ) : (
                <span className="flex items-center gap-1 text-muted-foreground">
                  <AlertCircle className="h-3 w-3" /> Not configured
                </span>
              )}
            </CardDescription>
          </div>
          {config.url ? (
            <Button
              variant="outline"
              size="sm"
              disabled={saving}
              onClick={() => void clear()}
              data-testid="clear-button"
            >
              Clear
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="webhook-url" className="text-xs">URL</Label>
          <Input
            id="webhook-url"
            type="url"
            placeholder="https://your-system.example.com/webhook"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            data-testid="url-input"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="webhook-secret" className="text-xs">
            Secret {config.secret_set ? <Badge variant="success" className="ml-1 text-[10px]">set</Badge> : null}
          </Label>
          <Input
            id="webhook-secret"
            type="password"
            placeholder={config.secret_set ? "(leave blank to keep existing)" : "min 16 chars — used for HMAC-SHA256"}
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            data-testid="secret-input"
          />
          <p className="text-[11px] text-muted-foreground">
            Secret is write-only. Leave blank to keep the existing secret when updating the URL.
            Both URL + secret are required to enable. Both null to disable.
          </p>
        </div>
        <div className="flex justify-end">
          <Button
            onClick={() => void save()}
            disabled={saving || (!url.trim() && !config.url)}
            data-testid="save-button"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// =========================================================================== //
// Test-ping card                                                              //
// =========================================================================== //

function TestPingCard({
  tenantId,
  hasWebhook,
}: {
  tenantId: string;
  hasWebhook: boolean;
}) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<WebhookTestResult | null>(null);

  async function run(): Promise<void> {
    setRunning(true);
    setResult(null);
    try {
      const r = await testWebhook(tenantId);
      setResult(r);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Test failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-sm">Test ping</CardTitle>
            <CardDescription>
              Fires a signed <code className="font-mono text-xs">webhook.test</code> event
              synchronously and shows the HTTP response.
            </CardDescription>
          </div>
          <Button
            variant="outline"
            disabled={running || !hasWebhook}
            onClick={() => void run()}
            data-testid="test-ping-button"
          >
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            Send test ping
          </Button>
        </div>
      </CardHeader>
      {result ? (
        <CardContent data-testid="ping-result">
          <div className="space-y-2 rounded-md border p-3">
            <div className="flex items-center gap-2">
              {result.error ? (
                <Badge variant="destructive">Error</Badge>
              ) : result.http_status && result.http_status < 300 ? (
                <Badge variant="success">HTTP {result.http_status}</Badge>
              ) : (
                <Badge variant="warning">HTTP {result.http_status ?? "?"}</Badge>
              )}
              <span className="font-mono text-xs text-muted-foreground">
                {result.delivery_id.slice(0, 12)}…
              </span>
              {result.signed ? (
                <Badge variant="outline" className="text-[10px]">HMAC signed</Badge>
              ) : null}
            </div>
            {result.error ? (
              <p className="text-xs text-destructive">{result.error}</p>
            ) : null}
            {result.response_body ? (
              <pre className="max-h-32 overflow-auto rounded bg-muted px-2 py-1 text-[10px]">
                {result.response_body}
              </pre>
            ) : null}
          </div>
        </CardContent>
      ) : null}
    </Card>
  );
}
