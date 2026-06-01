/**
 * Typed fetch wrapper for Pronaos admin REST.
 *
 * Every UI call goes through ``api()``. Responsibilities:
 * - Attach the bearer token from the auth store
 * - Throw ``ApiError`` with the response body's ``detail`` payload on
 *   non-2xx so React Query / try-catch surfaces a clean error
 * - Run the response through a Zod schema if one is provided — this
 *   is the contract that protects the UI from silent backend shape
 *   changes
 *
 * Base URL handling: in dev the Next.js dev server proxies /v1/* to
 * the FastAPI process, so we just use relative paths. In prod we're
 * served under /admin/ but still proxy /v1/* through the same origin,
 * so relative paths still work.
 */
import { z } from "zod";

import {
  ABTestResponseSchema,
  ApiKeySchema,
  ApiKeyWithSecretSchema,
  AuditListResponseSchema,
  AuditVerifyResponseSchema,
  BatchInfoSchema,
  BatchListResponseSchema,
  BudgetSchema,
  GatewaySettingsSchema,
  WebhookConfigSchema,
  WebhookTestResultSchema,
  ChatCompletionResponseSchema,
  DoctorResponseSchema,
  HealthResponseSchema,
  ModelsResponseSchema,
  PromptCacheStatsResponseSchema,
  ProvidersResponseSchema,
  ReasoningStatsResponseSchema,
  ResetBreakerResponseSchema,
  RoutingConfigSchema,
  SecurityConfigSchema,
  TeamSchema,
  TenantSchema,
  TimeseriesResponseSchema,
  UsageResponseSchema,
  type ABTestResponse,
  type ApiKey,
  type ApiKeyWithSecret,
  type AuditListResponse,
  type AuditVerifyResponse,
  type BatchInfo,
  type BatchListResponse,
  type BatchStatus,
  type Budget,
  type GatewaySettings,
  type WebhookConfig,
  type WebhookTestResult,
  type ChatCompletionResponse,
  type DoctorResponse,
  type GuardrailPolicy,
  type HealthResponse,
  type ModelInfo,
  type PromptCacheStatsResponse,
  type ProviderInfo,
  type ReasoningStatsResponse,
  type ResetBreakerResponse,
  type RoutingConfig,
  type RoutingScoreEntry,
  type RoutingStrategy,
  type SecurityConfig,
  type Team,
  type Tenant,
  type TimeseriesResponse,
  type UsageResponse,
} from "./schemas";

export class ApiError extends Error {
  public readonly status: number;
  public readonly detail: unknown;
  public constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type RequestOptions<TSchema extends z.ZodTypeAny | undefined = undefined> = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Bearer token override. Defaults to the value in localStorage. */
  token?: string | null;
  /** Zod schema to validate the response against. */
  schema?: TSchema;
  /** Extra headers merged into the request. */
  headers?: Record<string, string>;
  /** Abort signal for cancellation. */
  signal?: AbortSignal;
};

const TOKEN_STORAGE_KEY = "pronaos.api_key";

/** Read the persisted bearer token. Returns null in non-browser contexts. */
export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

/** Persist (or clear) the bearer token. */
export function setStoredToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

/**
 * Core typed fetch.
 *
 * When ``opts.schema`` is provided, the resolved value is the parsed
 * schema output. Otherwise the parsed JSON body is returned untyped
 * (``unknown``) — callers can cast or zod-parse downstream.
 */
export async function api<TSchema extends z.ZodTypeAny>(
  path: string,
  opts: RequestOptions<TSchema> & { schema: TSchema },
): Promise<z.infer<TSchema>>;
export async function api(
  path: string,
  opts?: RequestOptions,
): Promise<unknown>;
export async function api<TSchema extends z.ZodTypeAny | undefined>(
  path: string,
  opts: RequestOptions<TSchema> = {},
): Promise<unknown> {
  const {
    method = "GET",
    body,
    token = getStoredToken(),
    schema,
    headers = {},
    signal,
  } = opts;

  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (body !== undefined) {
    finalHeaders["Content-Type"] = "application/json";
  }
  if (token) {
    finalHeaders["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(path, {
    method,
    headers: finalHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  // Body parsing: try JSON; fall back to text for non-JSON responses
  // (e.g. 502 from an upstream proxy).
  const text = await res.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }

  if (!res.ok) {
    const detail =
      typeof parsed === "object" && parsed !== null && "detail" in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    const msg =
      typeof detail === "string"
        ? detail
        : typeof detail === "object" &&
            detail !== null &&
            "hint" in detail &&
            typeof (detail as { hint: unknown }).hint === "string"
          ? (detail as { hint: string }).hint
          : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, detail);
  }

  if (schema) {
    return schema.parse(parsed);
  }
  return parsed;
}

// ------------------------------------------------------------------ //
// Typed endpoint helpers — adds discoverability + IDE autocomplete.    //
// ------------------------------------------------------------------ //

/** GET /v1/healthz — Pronaos's actual liveness endpoint. */
export async function getHealth(opts?: RequestOptions): Promise<HealthResponse> {
  return api("/v1/healthz", { ...opts, schema: HealthResponseSchema });
}

/** GET /v1/admin/usage with optional team/tenant/window filters. */
export async function getUsage(
  params: {
    team_id?: string;
    tenant_id?: string;
    start_ts?: string;
    end_ts?: string;
  } = {},
  opts?: RequestOptions,
): Promise<UsageResponse> {
  const qs = new URLSearchParams();
  if (params.team_id) qs.set("team_id", params.team_id);
  if (params.tenant_id) qs.set("tenant_id", params.tenant_id);
  if (params.start_ts) qs.set("start_ts", params.start_ts);
  if (params.end_ts) qs.set("end_ts", params.end_ts);
  const path = qs.size ? `/v1/admin/usage?${qs}` : "/v1/admin/usage";
  return api(path, { ...opts, schema: UsageResponseSchema });
}

// =========================================================================== //
// Phase 63 — Identity (tenants / teams / keys)                                //
// =========================================================================== //

// ---- Tenants -------------------------------------------------------------- //

export async function listTenants(
  params: { q?: string; limit?: number } = {},
  opts?: RequestOptions,
): Promise<Tenant[]> {
  const qs = new URLSearchParams();
  if (params.q) qs.set("q", params.q);
  if (params.limit) qs.set("limit", String(params.limit));
  const path = qs.size ? `/v1/admin/tenants?${qs}` : "/v1/admin/tenants";
  return api(path, { ...opts, schema: z.array(TenantSchema) });
}

export async function createTenant(
  name: string,
  opts?: RequestOptions,
): Promise<Tenant> {
  return api("/v1/admin/tenants", {
    ...opts,
    method: "POST",
    body: { name },
    schema: TenantSchema,
  });
}

export async function updateTenant(
  id: string,
  patch: { name?: string; oidc_subject?: string | null },
  opts?: RequestOptions,
): Promise<Tenant> {
  return api(`/v1/admin/tenants/${encodeURIComponent(id)}`, {
    ...opts,
    method: "PATCH",
    body: patch,
    schema: TenantSchema,
  });
}

export async function deleteTenant(
  id: string,
  opts?: RequestOptions,
): Promise<void> {
  await api(`/v1/admin/tenants/${encodeURIComponent(id)}`, {
    ...opts,
    method: "DELETE",
  });
}

// ---- Teams ---------------------------------------------------------------- //

export async function listTeams(
  params: { tenant_id?: string; limit?: number } = {},
  opts?: RequestOptions,
): Promise<Team[]> {
  const qs = new URLSearchParams();
  if (params.tenant_id) qs.set("tenant_id", params.tenant_id);
  if (params.limit) qs.set("limit", String(params.limit));
  const path = qs.size ? `/v1/admin/teams?${qs}` : "/v1/admin/teams";
  return api(path, { ...opts, schema: z.array(TeamSchema) });
}

export async function createTeam(
  body: { tenant_id: string; name: string },
  opts?: RequestOptions,
): Promise<Team> {
  return api("/v1/admin/teams", {
    ...opts,
    method: "POST",
    body,
    schema: TeamSchema,
  });
}

export async function deleteTeam(id: string, opts?: RequestOptions): Promise<void> {
  await api(`/v1/admin/teams/${encodeURIComponent(id)}`, {
    ...opts,
    method: "DELETE",
  });
}

// ---- API keys ------------------------------------------------------------- //

export async function listKeys(
  params: { team_id?: string; include_revoked?: boolean } = {},
  opts?: RequestOptions,
): Promise<ApiKey[]> {
  const qs = new URLSearchParams();
  if (params.team_id) qs.set("team_id", params.team_id);
  if (params.include_revoked) qs.set("include_revoked", "true");
  const path = qs.size ? `/v1/admin/keys?${qs}` : "/v1/admin/keys";
  return api(path, { ...opts, schema: z.array(ApiKeySchema) });
}

export async function generateKey(
  body: {
    team_id: string;
    label?: string;
    scopes?: string[];
    env?: "live" | "test";
  },
  opts?: RequestOptions,
): Promise<ApiKeyWithSecret> {
  return api("/v1/admin/keys", {
    ...opts,
    method: "POST",
    body,
    schema: ApiKeyWithSecretSchema,
  });
}

export async function revokeKey(
  id: string,
  opts?: RequestOptions,
): Promise<void> {
  await api(`/v1/admin/keys/${encodeURIComponent(id)}`, {
    ...opts,
    method: "DELETE",
  });
}

// =========================================================================== //
// Phase 64 — Budgets + timeseries                                             //
// =========================================================================== //

export async function getBudget(
  teamId: string,
  opts?: RequestOptions,
): Promise<Budget> {
  return api(`/v1/admin/budgets/${encodeURIComponent(teamId)}`, {
    ...opts,
    schema: BudgetSchema,
  });
}

export async function updateBudget(
  teamId: string,
  patch: {
    monthly_token_budget?: number | null;
    monthly_cost_hcents_budget?: number | null;
  },
  opts?: RequestOptions,
): Promise<Budget> {
  return api(`/v1/admin/budgets/${encodeURIComponent(teamId)}`, {
    ...opts,
    method: "PUT",
    body: patch,
    schema: BudgetSchema,
  });
}

export async function getUsageTimeseries(
  params: {
    start_ts: string;
    end_ts: string;
    bucket?: "hour" | "day";
    team_id?: string;
  },
  opts?: RequestOptions,
): Promise<TimeseriesResponse> {
  const qs = new URLSearchParams();
  qs.set("start_ts", params.start_ts);
  qs.set("end_ts", params.end_ts);
  if (params.bucket) qs.set("bucket", params.bucket);
  if (params.team_id) qs.set("team_id", params.team_id);
  return api(`/v1/admin/usage/timeseries?${qs}`, {
    ...opts,
    schema: TimeseriesResponseSchema,
  });
}

// =========================================================================== //
// Phase 65 — Models catalog + chat playground                                 //
// =========================================================================== //

export async function listModels(opts?: RequestOptions): Promise<ModelInfo[]> {
  const body = await api("/v1/admin/models", {
    ...opts,
    schema: ModelsResponseSchema,
  });
  return body.items;
}

export type ChatRole = "system" | "user" | "assistant";

export interface PlaygroundMessage {
  role: ChatRole;
  content: string;
}

export interface ChatRequestBody {
  model: string;
  messages: PlaygroundMessage[];
  stream?: boolean;
  temperature?: number;
  max_tokens?: number;
}

/**
 * One streamed delta yielded by ``streamChatCompletion``.
 *
 * The playground only reads ``delta`` (the new content fragment) and
 * ``finish_reason`` (when set, the stream is over). ``raw`` carries
 * the full OpenAI-shape chunk so debug callers can inspect tool_calls
 * or reasoning_content if they want.
 */
export interface ChatStreamDelta {
  delta: string;
  finish_reason: string | null;
  raw: unknown;
}

/**
 * Iterate the SSE chunks of a streaming /v1/chat/completions response.
 *
 * Why a custom path instead of the api() wrapper: SSE responses are
 * ``text/event-stream``, not JSON, so the wrapper's text→JSON parse
 * would fail. We also need the raw ``Response`` object so the caller
 * can read response headers (X-Pronaos-Cache, -Cost-Hcents, etc.) on
 * stream completion.
 *
 * Returns ``{response, stream}``: ``response`` is the raw Response so
 * the caller can read headers immediately; ``stream`` is an async
 * iterable yielding deltas until the upstream emits ``[DONE]``.
 *
 * Throws ``ApiError`` on non-2xx status before any chunks are yielded.
 */
export async function streamChatCompletion(
  body: ChatRequestBody,
  opts: { token?: string | null; signal?: AbortSignal } = {},
): Promise<{
  response: Response;
  stream: AsyncIterable<ChatStreamDelta>;
}> {
  const token = opts.token ?? getStoredToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch("/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify({ ...body, stream: true }),
    signal: opts.signal,
  });

  if (!response.ok) {
    const text = await response.text();
    let detail: unknown = text;
    try {
      detail = JSON.parse(text);
    } catch {
      /* keep raw text */
    }
    const message =
      typeof detail === "object" &&
      detail !== null &&
      "detail" in detail &&
      typeof (detail as { detail: unknown }).detail === "string"
        ? (detail as { detail: string }).detail
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, message, detail);
  }

  const stream = parseSseStream(response);
  return { response, stream };
}

/**
 * Parse a streaming response body as OpenAI-shape SSE events,
 * yielding one ``ChatStreamDelta`` per data: chunk.
 *
 * Handles:
 * - ``data: [DONE]`` sentinel → stop iteration.
 * - JSON parse errors → skip that chunk (don't crash the stream).
 * - Chunks with ``choices[0].delta.content`` → yield as ``delta``.
 * - Terminal chunks with ``choices[0].finish_reason`` → yield with
 *   the reason set.
 *
 * Buffers across reads so a chunk split across two TCP frames still
 * parses correctly.
 */
async function* parseSseStream(
  response: Response,
): AsyncIterable<ChatStreamDelta> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE separates events by blank lines (\n\n). Process every
      // complete event in the buffer; leave any trailing partial
      // event for the next read.
      let separatorIdx: number;
      while ((separatorIdx = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, separatorIdx);
        buffer = buffer.slice(separatorIdx + 2);
        for (const line of rawEvent.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") return;
          if (!payload) continue;
          let parsed: unknown;
          try {
            parsed = JSON.parse(payload);
          } catch {
            continue;
          }
          const choice = (parsed as { choices?: Array<unknown> }).choices?.[0];
          if (!choice || typeof choice !== "object") continue;
          const c = choice as {
            delta?: { content?: unknown };
            finish_reason?: unknown;
          };
          const deltaContent =
            typeof c.delta?.content === "string" ? c.delta.content : "";
          const finishReason =
            typeof c.finish_reason === "string" ? c.finish_reason : null;
          if (deltaContent || finishReason) {
            yield { delta: deltaContent, finish_reason: finishReason, raw: parsed };
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// =========================================================================== //
// Phase 65 gap fill — Embeddings + Rerank playground helpers                 //
// =========================================================================== //

/** POST /v1/embeddings — returns the full response + raw Response for headers. */
export async function createEmbeddings(
  body: {
    model: string;
    input: string | string[];
    dimensions?: number;
    encoding_format?: "float" | "base64";
  },
  opts: { token?: string | null; signal?: AbortSignal } = {},
): Promise<{ data: unknown; response: Response }> {
  const token = opts.token ?? getStoredToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch("/v1/embeddings", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal: opts.signal,
  });

  const text = await response.text();
  let parsed: unknown;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }

  if (!response.ok) {
    const detail =
      typeof parsed === "object" && parsed !== null && "detail" in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    const msg = typeof detail === "string" ? detail : `HTTP ${response.status}`;
    throw new ApiError(response.status, msg, detail);
  }
  return { data: parsed, response };
}

/** POST /v1/rerank — returns the full response + raw Response for headers. */
export async function createRerank(
  body: {
    model: string;
    query: string;
    documents: string[];
    top_n?: number;
    return_documents?: boolean;
  },
  opts: { token?: string | null; signal?: AbortSignal } = {},
): Promise<{ data: unknown; response: Response }> {
  const token = opts.token ?? getStoredToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch("/v1/rerank", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal: opts.signal,
  });

  const text = await response.text();
  let parsed: unknown;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }

  if (!response.ok) {
    const detail =
      typeof parsed === "object" && parsed !== null && "detail" in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    const msg = typeof detail === "string" ? detail : `HTTP ${response.status}`;
    throw new ApiError(response.status, msg, detail);
  }
  return { data: parsed, response };
}

// =========================================================================== //
// Phase 71 — Gateway settings                                                 //
// =========================================================================== //

export async function getGatewaySettings(opts?: RequestOptions): Promise<GatewaySettings> {
  return api("/v1/admin/settings", { ...opts, schema: GatewaySettingsSchema });
}

// =========================================================================== //
// Phase 70 — Webhooks console                                                 //
// =========================================================================== //

export async function getWebhook(
  tenantId: string,
  opts?: RequestOptions,
): Promise<WebhookConfig> {
  return api(`/v1/admin/webhooks/${encodeURIComponent(tenantId)}`, {
    ...opts,
    schema: WebhookConfigSchema,
  });
}

export async function updateWebhook(
  tenantId: string,
  body: { url: string | null; secret: string | null },
  opts?: RequestOptions,
): Promise<WebhookConfig> {
  return api(`/v1/admin/webhooks/${encodeURIComponent(tenantId)}`, {
    ...opts,
    method: "PUT",
    body,
    schema: WebhookConfigSchema,
  });
}

export async function testWebhook(
  tenantId: string,
  opts?: RequestOptions,
): Promise<WebhookTestResult> {
  return api(`/v1/admin/webhooks/${encodeURIComponent(tenantId)}/test`, {
    ...opts,
    method: "POST",
    schema: WebhookTestResultSchema,
  });
}

// =========================================================================== //
// Phase 69 — Batches console                                                  //
// =========================================================================== //

export async function listAdminBatches(
  params: {
    team_id?: string;
    tenant_id?: string;
    status?: BatchStatus;
    limit?: number;
    offset?: number;
  } = {},
  opts?: RequestOptions,
): Promise<BatchListResponse> {
  const qs = new URLSearchParams();
  if (params.team_id) qs.set("team_id", params.team_id);
  if (params.tenant_id) qs.set("tenant_id", params.tenant_id);
  if (params.status) qs.set("status", params.status);
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.offset != null) qs.set("offset", String(params.offset));
  const tail = qs.size ? `?${qs}` : "";
  return api(`/v1/admin/batches${tail}`, { ...opts, schema: BatchListResponseSchema });
}

export async function getAdminBatch(
  batchId: string,
  opts?: RequestOptions,
): Promise<BatchInfo> {
  return api(`/v1/admin/batches/${encodeURIComponent(batchId)}`, {
    ...opts,
    schema: BatchInfoSchema,
  });
}

export async function cancelAdminBatch(
  batchId: string,
  opts?: RequestOptions,
): Promise<BatchInfo> {
  return api(`/v1/admin/batches/${encodeURIComponent(batchId)}/cancel`, {
    ...opts,
    method: "POST",
    schema: BatchInfoSchema,
  });
}

// =========================================================================== //
// Phase 66 gap fill — routing observations + A/B test stats                   //
// =========================================================================== //

export async function getPromptCacheStats(
  teamId: string,
  opts?: RequestOptions,
): Promise<PromptCacheStatsResponse> {
  return api(`/v1/admin/team/${encodeURIComponent(teamId)}/prompt-cache-stats`, {
    ...opts,
    schema: PromptCacheStatsResponseSchema,
  });
}

export async function getReasoningStats(
  teamId: string,
  opts?: RequestOptions,
): Promise<ReasoningStatsResponse> {
  return api(`/v1/admin/team/${encodeURIComponent(teamId)}/reasoning-stats`, {
    ...opts,
    schema: ReasoningStatsResponseSchema,
  });
}

export async function getAbTest(
  teamId: string,
  opts?: RequestOptions,
): Promise<ABTestResponse> {
  return api(`/v1/admin/team/${encodeURIComponent(teamId)}/ab-test`, {
    ...opts,
    schema: ABTestResponseSchema,
  });
}

// =========================================================================== //
// Phase 68 — Reliability + doctor                                             //
// =========================================================================== //

export async function listProviders(opts?: RequestOptions): Promise<ProviderInfo[]> {
  const body = await api("/v1/admin/providers", {
    ...opts,
    schema: ProvidersResponseSchema,
  });
  return body.items;
}

export async function resetBreaker(
  name: string,
  opts?: RequestOptions,
): Promise<ResetBreakerResponse> {
  return api(`/v1/admin/providers/${encodeURIComponent(name)}/reset-breaker`, {
    ...opts,
    method: "POST",
    schema: ResetBreakerResponseSchema,
  });
}

export async function getDoctorReport(opts?: RequestOptions): Promise<DoctorResponse> {
  return api("/v1/admin/doctor", {
    ...opts,
    schema: DoctorResponseSchema,
  });
}

// =========================================================================== //
// Phase 67 — Security + Audit                                                 //
// =========================================================================== //

export async function getSecurity(
  teamId: string,
  opts?: RequestOptions,
): Promise<SecurityConfig> {
  return api(`/v1/admin/security/${encodeURIComponent(teamId)}`, {
    ...opts,
    schema: SecurityConfigSchema,
  });
}

export async function updateSecurity(
  teamId: string,
  patch: {
    guardrail_policy?: GuardrailPolicy | null;
    pii_tokenization_enabled?: boolean | null;
    pii_token_ttl_seconds?: number | null;
  },
  opts?: RequestOptions,
): Promise<SecurityConfig> {
  // Same undefined-stripping pattern as updateRouting so the PATCH
  // semantics survive the wire.
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch)) {
    if (v !== undefined) body[k] = v;
  }
  return api(`/v1/admin/security/${encodeURIComponent(teamId)}`, {
    ...opts,
    method: "PUT",
    body,
    schema: SecurityConfigSchema,
  });
}

export async function listAuditRecords(
  tenantId: string,
  params: { team_id?: string; limit?: number; offset?: number } = {},
  opts?: RequestOptions,
): Promise<AuditListResponse> {
  const qs = new URLSearchParams();
  if (params.team_id) qs.set("team_id", params.team_id);
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.offset != null) qs.set("offset", String(params.offset));
  const tail = qs.size ? `?${qs}` : "";
  return api(`/v1/admin/audit/${encodeURIComponent(tenantId)}${tail}`, {
    ...opts,
    schema: AuditListResponseSchema,
  });
}

export async function verifyAuditChain(
  tenantId: string,
  opts?: RequestOptions,
): Promise<AuditVerifyResponse> {
  return api(`/v1/admin/audit/${encodeURIComponent(tenantId)}/verify`, {
    ...opts,
    method: "POST",
    schema: AuditVerifyResponseSchema,
  });
}

// =========================================================================== //
// Phase 66 — Routing console                                                  //
// =========================================================================== //

export async function getRouting(
  teamId: string,
  opts?: RequestOptions,
): Promise<RoutingConfig> {
  return api(`/v1/admin/routing/${encodeURIComponent(teamId)}`, {
    ...opts,
    schema: RoutingConfigSchema,
  });
}

/**
 * PATCH-style update for the team's routing config.
 *
 * Per the backend's ``model_fields_set`` semantics: a field omitted
 * from this object is unchanged. A field set explicitly to ``null``
 * clears the column back to NULL (gateway default kicks in).
 *
 * The callable signature uses ``undefined`` for "omitted" because
 * that's how TS clients commonly express it; we strip ``undefined``
 * before serialising so JSON.stringify doesn't accidentally include
 * the key.
 */
export async function updateRouting(
  teamId: string,
  patch: {
    routing_strategy?: RoutingStrategy | null;
    allowed_models?: string[] | null;
    quality_threshold?: number | null;
    quality_scores?: Record<string, RoutingScoreEntry> | null;
    tool_use_threshold?: number | null;
    tool_use_scores?: Record<string, RoutingScoreEntry> | null;
    prompt_cache_min_samples?: number | null;
    prompt_cache_min_hit_rate?: number | null;
    reasoning_aware_min_samples?: number | null;
    reasoning_aware_max_ratio?: number | null;
  },
  opts?: RequestOptions,
): Promise<RoutingConfig> {
  // Strip undefined keys so they're not serialised. The backend uses
  // model_fields_set to distinguish "omitted" from "null"; sending
  // ``{routing_strategy: undefined}`` becomes "routing_strategy":undefined
  // → silently dropped by JSON.stringify, BUT only at the top level.
  // Doing it here explicitly makes the wire payload obvious.
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch)) {
    if (v !== undefined) body[k] = v;
  }
  return api(`/v1/admin/routing/${encodeURIComponent(teamId)}`, {
    ...opts,
    method: "PUT",
    body,
    schema: RoutingConfigSchema,
  });
}

/**
 * Non-streaming chat completion. Used when the playground's stream
 * toggle is off — the path is simpler (one round-trip, full JSON
 * body) but loses the live-tokens feedback.
 */
export async function chatCompletion(
  body: ChatRequestBody,
  opts?: RequestOptions,
): Promise<{ data: ChatCompletionResponse; response: Response }> {
  const token = opts?.token ?? getStoredToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch("/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify({ ...body, stream: false }),
    signal: opts?.signal,
  });

  const text = await response.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }

  if (!response.ok) {
    const detail =
      typeof parsed === "object" && parsed !== null && "detail" in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    const msg = typeof detail === "string" ? detail : `HTTP ${response.status}`;
    throw new ApiError(response.status, msg, detail);
  }

  return { data: ChatCompletionResponseSchema.parse(parsed), response };
}
