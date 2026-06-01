/**
 * Zod schemas for Pronaos admin API responses.
 *
 * Each schema mirrors a Pydantic model on the backend. When the backend
 * changes its response shape, the schema here is the canary — request
 * parsing throws a ZodError that the UI surfaces as a clear toast,
 * not a silent type-coercion bug.
 *
 * Keep these in sync with src/pronaos/api/v1/*.py response_model classes.
 * The mocked-live verify (scripts/verify_ui_foundation.py) round-trips
 * these expectations against a live in-process gateway on every commit.
 */
import { z } from "zod";

/** GET /v1/healthz — backend lives at /healthz, not /health. */
export const HealthResponseSchema = z.object({
  status: z.string(),
  version: z.string().optional(),
});
export type HealthResponse = z.infer<typeof HealthResponseSchema>;

/**
 * GET /v1/admin/usage — paginated usage rows + aggregate totals.
 * Backend: ``UsageResponse`` in src/pronaos/api/v1/admin.py.
 *
 * Wire shape:
 *   { items: UsageItem[], totals: UsageTotals, limit: int, offset: int }
 */
export const UsageItemSchema = z.object({
  ts: z.string(),
  tenant_id: z.string(),
  team_id: z.string(),
  key_id: z.string(),
  provider: z.string(),
  model: z.string(),
  prompt_tokens: z.number().int().nonnegative(),
  completion_tokens: z.number().int().nonnegative(),
  cost_hcents: z.number().int().nonnegative(),
  request_id: z.string().nullable(),
  status: z.string(),
});
export type UsageItem = z.infer<typeof UsageItemSchema>;

export const UsageTotalsSchema = z.object({
  requests: z.number().int().nonnegative(),
  prompt_tokens: z.number().int().nonnegative(),
  completion_tokens: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative(),
  cost_hcents: z.number().int().nonnegative(),
});
export type UsageTotals = z.infer<typeof UsageTotalsSchema>;

export const UsageResponseSchema = z.object({
  items: z.array(UsageItemSchema),
  totals: UsageTotalsSchema,
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
});
export type UsageResponse = z.infer<typeof UsageResponseSchema>;

// =========================================================================== //
// Phase 63 — Identity (tenants / teams / API keys)                            //
// =========================================================================== //

export const TenantSchema = z.object({
  id: z.string(),
  name: z.string(),
  created_at: z.number().int(),
  webhook_url: z.string().nullable(),
  oidc_subject: z.string().nullable(),
});
export type Tenant = z.infer<typeof TenantSchema>;

export const TeamSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  name: z.string(),
  created_at: z.number().int(),
});
export type Team = z.infer<typeof TeamSchema>;

/** Key as returned by GET endpoints — secret is never present. */
export const ApiKeySchema = z.object({
  id: z.string(),
  team_id: z.string(),
  prefix: z.string(),
  label: z.string(),
  scopes: z.array(z.string()),
  status: z.enum(["active", "revoked"]),
  created_at: z.number().int(),
  revoked_at: z.number().int().nullable(),
  last_used_at: z.number().int().nullable(),
});
export type ApiKey = z.infer<typeof ApiKeySchema>;

/** Returned ONCE on POST /v1/admin/keys — contains the full secret.
 *  The caller must persist this immediately; the secret is never
 *  retrievable again. */
export const ApiKeyWithSecretSchema = ApiKeySchema.extend({
  api_key: z.string(),
  status: z.literal("active"),
}).omit({ revoked_at: true, last_used_at: true });
export type ApiKeyWithSecret = z.infer<typeof ApiKeyWithSecretSchema>;

// =========================================================================== //
// Phase 64 — Budgets + usage timeseries                                       //
// =========================================================================== //

export const BudgetSchema = z.object({
  team_id: z.string(),
  monthly_token_budget: z.number().int().nonnegative().nullable(),
  current_period_tokens: z.number().int().nonnegative(),
  monthly_cost_hcents_budget: z.number().int().nonnegative().nullable(),
  current_period_cost_hcents: z.number().int().nonnegative(),
  period_resets_at: z.number().int(),
});
export type Budget = z.infer<typeof BudgetSchema>;

export const TimeseriesPointSchema = z.object({
  bucket: z.number().int(),
  requests: z.number().int().nonnegative(),
  prompt_tokens: z.number().int().nonnegative(),
  completion_tokens: z.number().int().nonnegative(),
  cost_hcents: z.number().int().nonnegative(),
});
export type TimeseriesPoint = z.infer<typeof TimeseriesPointSchema>;

export const TimeseriesResponseSchema = z.object({
  bucket_size_seconds: z.number().int().positive(),
  points: z.array(TimeseriesPointSchema),
});
export type TimeseriesResponse = z.infer<typeof TimeseriesResponseSchema>;

// =========================================================================== //
// Phase 65 — Models catalog + chat playground                                 //
// =========================================================================== //

/**
 * One row from GET /v1/admin/models. Mirrors ``ModelInfo`` in
 * src/pronaos/api/v1/models.py. The endpoint always sorts so that
 * (allowed && provider_configured) rows come first, then allowed-but-
 * not-configured, then disallowed — UIs can iterate in order and dim
 * later rows without re-sorting.
 */
export const ModelInfoSchema = z.object({
  fqmn: z.string(),
  provider: z.string(),
  input_hcents_per_mtok: z.number().int().nonnegative(),
  output_hcents_per_mtok: z.number().int().nonnegative(),
  supports_tools: z.boolean(),
  supports_streaming: z.boolean(),
  supports_vision: z.boolean(),
  max_context_tokens: z.number().int().positive(),
  provider_configured: z.boolean(),
  allowed: z.boolean(),
});
export type ModelInfo = z.infer<typeof ModelInfoSchema>;

export const ModelsResponseSchema = z.object({
  items: z.array(ModelInfoSchema),
});
export type ModelsResponse = z.infer<typeof ModelsResponseSchema>;

/**
 * Subset of OpenAI-shape ChatCompletion the playground reads. The
 * gateway emits more fields (system_fingerprint, etc.) — these are
 * the ones the playground actually needs.
 */
export const ChatCompletionChoiceSchema = z.object({
  index: z.number().int().nonnegative(),
  message: z.object({
    role: z.string(),
    content: z.string().nullable().optional(),
    reasoning_content: z.string().nullable().optional(),
  }),
  finish_reason: z.string().nullable().optional(),
});
export type ChatCompletionChoice = z.infer<typeof ChatCompletionChoiceSchema>;

export const ChatCompletionUsageSchema = z.object({
  prompt_tokens: z.number().int().nonnegative(),
  completion_tokens: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative(),
});
export type ChatCompletionUsage = z.infer<typeof ChatCompletionUsageSchema>;

export const ChatCompletionResponseSchema = z.object({
  id: z.string().optional(),
  object: z.string().optional(),
  created: z.number().int().optional(),
  model: z.string(),
  choices: z.array(ChatCompletionChoiceSchema),
  usage: ChatCompletionUsageSchema.optional(),
});
export type ChatCompletionResponse = z.infer<typeof ChatCompletionResponseSchema>;

// =========================================================================== //
// Phase 66 — Routing console (composed config)                                //
// =========================================================================== //

/**
 * Inner shape of a score entry inside ``quality_scores`` or
 * ``tool_use_scores``. The router requires ``score`` to be present;
 * the rest is metadata the eval harness writes for auditability.
 */
export const RoutingScoreEntrySchema = z.object({
  score: z.number(),
  n_samples: z.number().int().nonnegative().optional(),
  source_eval_id: z.string().optional(),
  ts: z.string().optional(),
});
export type RoutingScoreEntry = z.infer<typeof RoutingScoreEntrySchema>;

/**
 * The seven RoutingStrategy values exposed by the gateway. Mirrors
 * ``RoutingStrategy`` in src/pronaos/core/scorer.py.
 */
export const RoutingStrategyEnum = z.enum([
  "cheapest",
  "fastest",
  "balanced",
  "quality-aware-cheapest",
  "tool-use-aware-cheapest",
  "prompt-cache-aware-cheapest",
  "reasoning-aware-cheapest",
]);
export type RoutingStrategy = z.infer<typeof RoutingStrategyEnum>;

export const ROUTING_STRATEGIES: RoutingStrategy[] = [
  "cheapest",
  "fastest",
  "balanced",
  "quality-aware-cheapest",
  "tool-use-aware-cheapest",
  "prompt-cache-aware-cheapest",
  "reasoning-aware-cheapest",
];

/**
 * Composed routing config returned by GET /v1/admin/routing/{team_id}.
 * Mirrors ``RoutingConfigResponse`` in src/pronaos/api/v1/routing.py.
 */
export const RoutingConfigSchema = z.object({
  team_id: z.string(),
  routing_strategy: RoutingStrategyEnum.nullable(),
  allowed_models: z.array(z.string()).nullable(),
  quality_threshold: z.number().min(0).max(1).nullable(),
  quality_scores: z.record(z.string(), RoutingScoreEntrySchema).nullable(),
  tool_use_threshold: z.number().min(0).max(1).nullable(),
  tool_use_scores: z.record(z.string(), RoutingScoreEntrySchema).nullable(),
  prompt_cache_min_samples: z.number().int().nonnegative().nullable(),
  prompt_cache_min_hit_rate: z.number().min(0).max(1).nullable(),
  reasoning_aware_min_samples: z.number().int().nonnegative().nullable(),
  reasoning_aware_max_ratio: z.number().min(0).nullable(),
});
export type RoutingConfig = z.infer<typeof RoutingConfigSchema>;

// =========================================================================== //
// Phase 67 — Security + Audit                                                 //
// =========================================================================== //

export const GuardrailActionEnum = z.enum([
  "block",
  "redact",
  "tokenize",
  "log_only",
]);
export type GuardrailAction = z.infer<typeof GuardrailActionEnum>;

/**
 * The guardrail_policy JSON is intentionally permissive on the
 * backend (the engine owns its shape; we don't constrain
 * Presidio / Llama Guard sub-blocks at the API layer). Mirror that
 * here: the well-known top-level keys are typed; everything else
 * passes through.
 */
export const GuardrailPolicySchema = z
  .object({
    disabled_rules: z.array(z.string()).optional(),
    rule_actions: z.record(z.string(), GuardrailActionEnum).optional(),
  })
  .catchall(z.unknown());
export type GuardrailPolicy = z.infer<typeof GuardrailPolicySchema>;

export const SecurityConfigSchema = z.object({
  team_id: z.string(),
  guardrail_policy: GuardrailPolicySchema.nullable(),
  pii_tokenization_enabled: z.boolean(),
  pii_token_ttl_seconds: z.number().int().nonnegative().nullable(),
  known_rule_ids: z.array(z.string()),
  valid_actions: z.array(z.string()),
});
export type SecurityConfig = z.infer<typeof SecurityConfigSchema>;

export const AuditRecordItemSchema = z.object({
  id: z.string(),
  ts: z.string(),
  tenant_id: z.string(),
  team_id: z.string(),
  key_id: z.string(),
  provider: z.string(),
  model: z.string(),
  request_hash: z.string(),
  response_hash: z.string(),
  prev_hash: z.string(),
  this_hash: z.string(),
  request_id: z.string().nullable(),
});
export type AuditRecordItem = z.infer<typeof AuditRecordItemSchema>;

export const AuditListResponseSchema = z.object({
  items: z.array(AuditRecordItemSchema),
  total: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
});
export type AuditListResponse = z.infer<typeof AuditListResponseSchema>;

export const ChainBreakItemSchema = z.object({
  record_id: z.string(),
  ts_iso: z.string(),
  reason: z.string(),
  expected_hash: z.string(),
  actual_hash: z.string(),
});
export type ChainBreakItem = z.infer<typeof ChainBreakItemSchema>;

export const AuditVerifyResponseSchema = z.object({
  tenant_id: z.string(),
  is_intact: z.boolean(),
  total_records: z.number().int().nonnegative(),
  verified_records: z.number().int().nonnegative(),
  breaks: z.array(ChainBreakItemSchema),
});
export type AuditVerifyResponse = z.infer<typeof AuditVerifyResponseSchema>;

// =========================================================================== //
// Phase 68 — Reliability console + doctor                                     //
// =========================================================================== //

// =========================================================================== //
// Phase 71 — Gateway settings                                                 //
// =========================================================================== //

export const GatewaySettingsSchema = z.object({
  redis_configured: z.boolean(),
  semantic_cache_enabled: z.boolean(),
  anthropic_configured: z.boolean(),
  groq_configured: z.boolean(),
  openai_configured: z.boolean(),
  bedrock_configured: z.boolean(),
  vertex_configured: z.boolean(),
  mcp_enabled: z.boolean(),
  presidio_enabled: z.boolean(),
  singleflight_distributed: z.boolean(),
  oidc_configured: z.boolean(),
  oidc_issuer: z.string().nullable(),
  database_scheme: z.string().nullable(),
});
export type GatewaySettings = z.infer<typeof GatewaySettingsSchema>;

// =========================================================================== //
// Phase 70 — Webhooks console                                                 //
// =========================================================================== //

export const WebhookConfigSchema = z.object({
  tenant_id: z.string(),
  url: z.string().nullable(),
  secret_set: z.boolean(),
});
export type WebhookConfig = z.infer<typeof WebhookConfigSchema>;

export const WebhookTestResultSchema = z.object({
  tenant_id: z.string(),
  http_status: z.number().int().nullable(),
  response_body: z.string().nullable(),
  error: z.string().nullable(),
  signed: z.boolean(),
  delivery_id: z.string(),
});
export type WebhookTestResult = z.infer<typeof WebhookTestResultSchema>;

// =========================================================================== //
// Phase 69 — Batches console                                                  //
// =========================================================================== //

export const BATCH_STATUSES = [
  "validating",
  "in_progress",
  "finalizing",
  "completed",
  "failed",
  "expired",
  "cancelled",
] as const;
export type BatchStatus = (typeof BATCH_STATUSES)[number];

export const BatchInfoSchema = z.object({
  id: z.string(),
  object: z.string(),
  provider: z.string(),
  provider_batch_id: z.string().nullable(),
  status: z.string(),
  endpoint: z.string(),
  completion_window: z.string(),
  request_counts: z.object({
    total: z.number().int().nonnegative(),
    completed: z.number().int().nonnegative(),
    failed: z.number().int().nonnegative(),
  }),
  created_at: z.number().int(),
  in_progress_at: z.number().int().nullable(),
  completed_at: z.number().int().nullable(),
  error_message: z.string().nullable(),
});
export type BatchInfo = z.infer<typeof BatchInfoSchema>;

export const BatchListResponseSchema = z.object({
  items: z.array(BatchInfoSchema),
  total: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
});
export type BatchListResponse = z.infer<typeof BatchListResponseSchema>;

export const CircuitStateEnum = z.enum(["closed", "open", "half_open"]);
export type CircuitState = z.infer<typeof CircuitStateEnum>;

export const ProviderInfoSchema = z.object({
  name: z.string(),
  configured: z.boolean(),
  model_count: z.number().int().nonnegative(),
  typical_p50_ms: z.number().int().positive().nullable(),
  circuit_state: CircuitStateEnum,
  notes: z.string(),
});
export type ProviderInfo = z.infer<typeof ProviderInfoSchema>;

export const ProvidersResponseSchema = z.object({
  items: z.array(ProviderInfoSchema),
});
export type ProvidersResponse = z.infer<typeof ProvidersResponseSchema>;

export const ResetBreakerResponseSchema = z.object({
  name: z.string(),
  circuit_state: CircuitStateEnum,
});
export type ResetBreakerResponse = z.infer<typeof ResetBreakerResponseSchema>;

export const DoctorVerdictEnum = z.enum(["PASS", "FAIL", "WARN", "SKIP"]);
export type DoctorVerdict = z.infer<typeof DoctorVerdictEnum>;

export const DoctorGateSchema = z.object({
  name: z.string(),
  verdict: DoctorVerdictEnum,
  detail: z.string(),
});
export type DoctorGate = z.infer<typeof DoctorGateSchema>;

export const DoctorSummarySchema = z.object({
  total: z.number().int().nonnegative(),
  passed: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  warn: z.number().int().nonnegative(),
  skip: z.number().int().nonnegative(),
});
export type DoctorSummary = z.infer<typeof DoctorSummarySchema>;

export const DoctorResponseSchema = z.object({
  gates: z.array(DoctorGateSchema),
  summary: DoctorSummarySchema,
  has_fail: z.boolean(),
  has_warn: z.boolean(),
});
export type DoctorResponse = z.infer<typeof DoctorResponseSchema>;

// --------------------------------------------------------------------------- //
// Phase 66 gap fill — routing observations (prompt-cache + reasoning)         //
// --------------------------------------------------------------------------- //

export const PromptCacheStatEntrySchema = z.object({
  fqmn: z.string(),
  n_samples: z.number().int().nonnegative(),
  prompt_tokens: z.number().int().nonnegative(),
  cached_tokens: z.number().int().nonnegative(),
  saved_hcents: z.number().int().nonnegative(),
  hit_rate: z.number().min(0).max(1),
});
export type PromptCacheStatEntry = z.infer<typeof PromptCacheStatEntrySchema>;

export const PromptCacheStatsResponseSchema = z.object({
  team_id: z.string(),
  min_samples: z.number().int().nonnegative().nullable(),
  min_hit_rate: z.number().min(0).max(1).nullable(),
  stats: z.array(PromptCacheStatEntrySchema),
});
export type PromptCacheStatsResponse = z.infer<typeof PromptCacheStatsResponseSchema>;

export const ReasoningStatEntrySchema = z.object({
  fqmn: z.string(),
  n_samples: z.number().int().nonnegative(),
  completion_tokens: z.number().int().nonnegative(),
  reasoning_tokens: z.number().int().nonnegative(),
  ratio: z.number().min(0),
});
export type ReasoningStatEntry = z.infer<typeof ReasoningStatEntrySchema>;

export const ReasoningStatsResponseSchema = z.object({
  team_id: z.string(),
  min_samples: z.number().int().nonnegative().nullable(),
  max_ratio: z.number().min(0).nullable(),
  stats: z.array(ReasoningStatEntrySchema),
});
export type ReasoningStatsResponse = z.infer<typeof ReasoningStatsResponseSchema>;

// --------------------------------------------------------------------------- //
// Phase 66 gap fill — A/B test stats                                          //
// --------------------------------------------------------------------------- //

export const ABTestArmStatsSchema = z.object({
  arm: z.string(),
  n: z.number().int().nonnegative(),
  mean_cost_hcents: z.number(),
  mean_total_tokens: z.number(),
  median_total_tokens: z.number(),
});
export type ABTestArmStats = z.infer<typeof ABTestArmStatsSchema>;

export const ABTestTTestResultSchema = z.object({
  t_statistic: z.number(),
  p_value: z.number().min(0).max(1),
  df: z.number(),
  cohens_d: z.number(),
  ci_low: z.number(),
  ci_high: z.number(),
  significant_at_05: z.boolean(),
});
export type ABTestTTestResult = z.infer<typeof ABTestTTestResultSchema>;

export const ABTestResponseSchema = z.object({
  team_id: z.string(),
  test_id: z.string().nullable(),
  test_name: z.string().nullable(),
  started_at: z.string().nullable(),
  arm_a_model: z.string().nullable(),
  arm_b_model: z.string().nullable(),
  arm_a_stats: ABTestArmStatsSchema.nullable(),
  arm_b_stats: ABTestArmStatsSchema.nullable(),
  t_test: ABTestTTestResultSchema.nullable(),
});
export type ABTestResponse = z.infer<typeof ABTestResponseSchema>;
