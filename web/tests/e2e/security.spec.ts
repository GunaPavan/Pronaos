import { expect, test } from "@playwright/test";

/**
 * Phase 67 security console + audit viewer — Playwright e2e.
 *
 * Covers
 * ------
 * - /guardrails loads the team's security config, renders rule rows,
 *   PII toggle, TTL input.
 * - Changing a rule action PUTs the new policy with PATCH semantics
 *   (only guardrail_policy in the body).
 * - 403 on /v1/admin/security surfaces the standard scope-missing
 *   error.
 * - /guardrails/audit loads the records, surfaces verify pass + fail.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const TEAMS_BODY = [
  { id: "te1", tenant_id: "tn1", name: "eng", created_at: 1700000000 },
];

const TENANTS_BODY = [
  {
    id: "tn1",
    name: "acme",
    created_at: 1700000000,
    webhook_url: null,
    oidc_subject: null,
  },
];

function baseSecurityConfig() {
  return {
    team_id: "te1",
    guardrail_policy: null as Record<string, unknown> | null,
    pii_tokenization_enabled: false,
    pii_token_ttl_seconds: null as number | null,
    known_rule_ids: [
      "injection",
      "llama_guard",
      "pii.email",
      "pii.ipv4",
      "pii.phone",
      "pii.ssn",
      "presidio",
    ],
    valid_actions: ["block", "log_only", "redact", "tokenize"],
  };
}

// --------------------------------------------------------------------------- //
// Guardrails page                                                             //
// --------------------------------------------------------------------------- //

test("guardrails page loads config + renders rule rows", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/security\/te1/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(baseSecurityConfig()),
    }),
  );

  await page.goto("/guardrails");
  await expect(page.getByRole("heading", { name: "Guardrails" })).toBeVisible();
  // Rules table populated.
  await expect(page.getByTestId("rules-table")).toContainText("pii.email");
  await expect(page.getByTestId("rules-table")).toContainText("injection");
  await expect(page.getByTestId("rules-table")).toContainText("llama_guard");
});

test("changing a rule action PUTs the new policy", async ({ page, context }) => {
  let state = baseSecurityConfig();
  const seenPuts: Array<Record<string, unknown>> = [];

  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/security\/te1/, async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }
    if (req.method() === "PUT") {
      const patch = JSON.parse(req.postData() ?? "{}") as Record<string, unknown>;
      seenPuts.push(patch);
      state = { ...state, ...patch } as typeof state;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/guardrails");
  await expect(page.getByTestId("rules-table")).toBeVisible();

  // Change pii.email action from default (redact) to block.
  await page.getByTestId("action-pii.email").selectOption("block");

  await expect
    .poll(() => seenPuts.length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  // The PATCH body should ONLY contain guardrail_policy (the field we
  // changed); pii_tokenization_enabled + pii_token_ttl_seconds stay
  // omitted so the backend treats them as unchanged.
  expect(Object.keys(seenPuts[0] ?? {})).toEqual(["guardrail_policy"]);
});

test("guardrails surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/security\/te1/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { hint: "missing required scope: admin:usage" },
      }),
    }),
  );

  await page.goto("/guardrails");
  await expect(page.getByTestId("security-load-error")).toContainText(
    /admin:usage/i,
  );
});

// --------------------------------------------------------------------------- //
// Audit page                                                                  //
// --------------------------------------------------------------------------- //

const SEED_RECORDS = [
  {
    id: "r1",
    ts: "2026-05-30T00:00:00Z",
    tenant_id: "tn1",
    team_id: "te1",
    key_id: "k1",
    provider: "groq",
    model: "llama-3.1-8b-instant",
    request_hash: "a".repeat(64),
    response_hash: "b".repeat(64),
    prev_hash: "",
    this_hash: "c".repeat(64),
    request_id: "req_1",
  },
  {
    id: "r2",
    ts: "2026-05-30T00:01:00Z",
    tenant_id: "tn1",
    team_id: "te1",
    key_id: "k1",
    provider: "groq",
    model: "llama-3.1-8b-instant",
    request_hash: "d".repeat(64),
    response_hash: "e".repeat(64),
    prev_hash: "c".repeat(64),
    this_hash: "f".repeat(64),
    request_id: "req_2",
  },
];

test("audit page renders records + verify-pass verdict", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/tenants/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TENANTS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/audit\/tn1\/verify/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "tn1",
        is_intact: true,
        total_records: 2,
        verified_records: 2,
        breaks: [],
      }),
    }),
  );
  await context.route(/\/v1\/admin\/audit\/tn1(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: SEED_RECORDS,
        total: 2,
        limit: 25,
        offset: 0,
      }),
    }),
  );

  await page.goto("/guardrails/audit");
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("audit-table")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("audit-table")).toContainText("req_1");
  await expect(page.getByTestId("audit-table")).toContainText("req_2");

  await page.getByTestId("verify-button").click();
  await expect(page.getByTestId("verdict-card")).toContainText(
    /chain intact/i,
  );
});

test("audit page surfaces a chain break with the tampered record id", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/tenants/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TENANTS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/audit\/tn1\/verify/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "tn1",
        is_intact: false,
        total_records: 2,
        verified_records: 1,
        breaks: [
          {
            record_id: "r2",
            ts_iso: "2026-05-30T00:01:00Z",
            reason: "hash_mismatch",
            expected_hash: "X".repeat(64),
            actual_hash: "f".repeat(64),
          },
        ],
      }),
    }),
  );
  await context.route(/\/v1\/admin\/audit\/tn1(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: SEED_RECORDS,
        total: 2,
        limit: 25,
        offset: 0,
      }),
    }),
  );

  await page.goto("/guardrails/audit");
  // Wait for the page to finish its initial render (tenant select + verify button).
  await expect(page.getByTestId("verify-button")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("verify-button").click();
  await expect(page.getByTestId("verdict-card")).toContainText(/chain broken/i, { timeout: 10_000 });
  await expect(page.getByTestId("breaks-table")).toContainText("hash_mismatch");
});
