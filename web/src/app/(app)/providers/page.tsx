"use client";

/**
 * /providers — reliability console (Phase 68).
 *
 * One row per provider: configured? + model count + p50 latency +
 * live circuit-breaker state. Per-row "Reset breaker" CTA when the
 * breaker is OPEN or HALF_OPEN.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Stethoscope,
  Zap,
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
  listProviders,
  resetBreaker,
} from "@/lib/api/client";
import type { CircuitState, ProviderInfo } from "@/lib/api/schemas";

function stateBadge(state: CircuitState) {
  switch (state) {
    case "closed":
      return <Badge variant="success">closed</Badge>;
    case "half_open":
      return <Badge variant="warning">half-open</Badge>;
    case "open":
      return <Badge variant="destructive">open</Badge>;
  }
}

export default function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [resetting, setResetting] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      setProviders(await listProviders());
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setLoadError(msg);
      if (err instanceof ApiError && err.status === 403) {
        toast.error("This key lacks the admin:usage scope");
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onReset(name: string): Promise<void> {
    setResetting(name);
    try {
      await resetBreaker(name);
      toast.success(`${name} breaker reset to closed`);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setResetting(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Providers</h1>
          <p className="text-sm text-muted-foreground">
            Configured providers + live circuit-breaker state. Run the
            gateway-wide health check on{" "}
            <Link
              href="/doctor"
              className="underline underline-offset-2 hover:text-foreground"
            >
              /doctor
            </Link>
            .
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw className="h-4 w-4" />
            Reload
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link href="/doctor">
              <Stethoscope className="h-4 w-4" />
              Doctor
            </Link>
          </Button>
        </div>
      </div>

      {loadError ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive" data-testid="providers-load-error">
              {loadError}
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Catalog</CardTitle>
          <CardDescription>
            {providers
              ? `${providers.length} provider${providers.length === 1 ? "" : "s"} known. Configured rows sort first.`
              : "Loading…"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {providers === null ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="providers-table">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-2 py-2">Provider</th>
                    <th className="px-2 py-2">Configured</th>
                    <th className="px-2 py-2 text-right">Models</th>
                    <th className="px-2 py-2 text-right">p50 latency</th>
                    <th className="px-2 py-2">Circuit</th>
                    <th className="px-2 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {providers.map((p) => (
                    <tr
                      key={p.name}
                      className="border-t align-top"
                      data-testid={`provider-${p.name}`}
                    >
                      <td className="px-2 py-3">
                        <div className="font-mono text-xs font-medium">{p.name}</div>
                        <p className="mt-1 text-[11px] text-muted-foreground">
                          {p.notes || ""}
                        </p>
                      </td>
                      <td className="px-2 py-3">
                        {p.configured ? (
                          <span className="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-400">
                            <CheckCircle2 className="h-3 w-3" /> yes
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                            <AlertCircle className="h-3 w-3" /> no
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-3 text-right tabular-nums">
                        {p.model_count}
                      </td>
                      <td className="px-2 py-3 text-right tabular-nums">
                        {p.typical_p50_ms == null ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <span className="inline-flex items-center justify-end gap-1">
                            <Zap className="h-3 w-3 text-muted-foreground" />
                            {p.typical_p50_ms} ms
                          </span>
                        )}
                      </td>
                      <td
                        className="px-2 py-3"
                        data-testid={`circuit-${p.name}`}
                      >
                        {stateBadge(p.circuit_state)}
                      </td>
                      <td className="px-2 py-3">
                        {p.circuit_state !== "closed" ? (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={resetting === p.name}
                            onClick={() => void onReset(p.name)}
                            data-testid={`reset-${p.name}`}
                          >
                            {resetting === p.name ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : null}
                            Reset breaker
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
    </div>
  );
}
