import { expect, test } from "@playwright/test";

/**
 * Phase 68 reliability + doctor — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /providers renders one row per provider with circuit-state badge.
 * - Reset-breaker click fires POST /v1/admin/providers/{name}/reset-breaker
 *   and re-fetches the list.
 * - /doctor runs the report on mount, populates summary tiles + grouped
 *   gate cards, surfaces an overall verdict that flips with a FAIL gate.
 * - 403 from /v1/admin/doctor surfaces clearly.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const PROVIDERS_BODY = {
  items: [
    {
      name: "groq",
      configured: true,
      model_count: 5,
      typical_p50_ms: 250,
      circuit_state: "closed" as const,
      notes: "Free tier; fast inference; open-weight models.",
    },
    {
      name: "anthropic",
      configured: false,
      model_count: 3,
      typical_p50_ms: null,
      circuit_state: "open" as const,
      notes: "Native Anthropic adapter (Claude family).",
    },
  ],
};

// --------------------------------------------------------------------------- //
// Providers                                                                   //
// --------------------------------------------------------------------------- //

test("providers page lists rows with circuit-state badges", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/providers$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROVIDERS_BODY),
    }),
  );

  await page.goto("/providers");
  await expect(page.getByRole("heading", { name: "Providers" })).toBeVisible();
  await expect(page.getByTestId("providers-table")).toContainText("groq");
  await expect(page.getByTestId("providers-table")).toContainText("anthropic");
  await expect(page.getByTestId("circuit-groq")).toContainText("closed");
  await expect(page.getByTestId("circuit-anthropic")).toContainText("open");
});

test("reset-breaker click fires POST + refreshes the list", async ({
  page,
  context,
}) => {
  // Start with anthropic OPEN; after reset, it's CLOSED.
  let state = JSON.parse(JSON.stringify(PROVIDERS_BODY)) as typeof PROVIDERS_BODY;
  const seenResets: string[] = [];

  await context.route(/\/v1\/admin\/providers$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(state),
    }),
  );
  await context.route(
    /\/v1\/admin\/providers\/([^/]+)\/reset-breaker/,
    async (route) => {
      const url = new URL(route.request().url());
      const name = url.pathname.split("/").at(-2) ?? "";
      seenResets.push(name);
      // Update the in-memory state so the follow-up GET reflects it.
      state = {
        ...state,
        items: state.items.map((p) =>
          p.name === name ? { ...p, circuit_state: "closed" as const } : p,
        ),
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ name, circuit_state: "closed" }),
      });
    },
  );

  await page.goto("/providers");
  await expect(page.getByTestId("circuit-anthropic")).toContainText("open");

  await page.getByTestId("reset-anthropic").click();

  await expect.poll(() => seenResets[0], { timeout: 5_000 }).toBe("anthropic");
  // After the refresh, anthropic is closed and the reset button is gone.
  await expect(page.getByTestId("circuit-anthropic")).toContainText("closed");
});

// --------------------------------------------------------------------------- //
// Doctor                                                                      //
// --------------------------------------------------------------------------- //

const HEALTHY_REPORT = {
  gates: [
    { name: "config.secret_key", verdict: "PASS", detail: "set (64 chars)" },
    { name: "config.database_url", verdict: "PASS", detail: "scheme=sqlite" },
    { name: "db.connect", verdict: "PASS", detail: "" },
    { name: "auth.tenant_count", verdict: "PASS", detail: "1 tenant" },
    { name: "redis.ping", verdict: "SKIP", detail: "PRONAOS_REDIS_URL unset" },
  ],
  summary: { total: 5, passed: 4, failed: 0, warn: 0, skip: 1 },
  has_fail: false,
  has_warn: false,
};

const FAILING_REPORT = {
  gates: [
    { name: "config.secret_key", verdict: "PASS", detail: "set (64 chars)" },
    {
      name: "db.connect",
      verdict: "FAIL",
      detail: "could not connect: ECONNREFUSED",
    },
    { name: "auth.tenant_count", verdict: "WARN", detail: "no tenants seeded" },
  ],
  summary: { total: 3, passed: 1, failed: 1, warn: 1, skip: 0 },
  has_fail: true,
  has_warn: true,
};

test("doctor page renders summary tiles + grouped gates on healthy report", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/doctor/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(HEALTHY_REPORT),
    }),
  );

  await page.goto("/doctor");
  await expect(page.getByRole("heading", { name: "Doctor" })).toBeVisible();
  await expect(page.getByTestId("summary-pass")).toHaveText("4");
  await expect(page.getByTestId("summary-fail")).toHaveText("0");
  await expect(page.getByTestId("summary-warn")).toHaveText("0");
  await expect(page.getByTestId("summary-skip")).toHaveText("1");
  await expect(page.getByTestId("overall-verdict")).toContainText(
    /all gates passing/i,
  );
  // Two distinct group cards.
  await expect(page.getByTestId("group-config")).toBeVisible();
  await expect(page.getByTestId("group-db")).toBeVisible();
});

test("doctor page surfaces FAIL banner when a gate fails", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/doctor/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(FAILING_REPORT),
    }),
  );

  await page.goto("/doctor");
  await expect(page.getByTestId("summary-fail")).toHaveText("1");
  await expect(page.getByTestId("overall-verdict")).toContainText(
    /gates failing/i,
  );
  await expect(page.getByTestId("gate-db.connect")).toContainText("FAIL");
  await expect(page.getByTestId("gate-db.connect")).toContainText(
    "ECONNREFUSED",
  );
});

test("doctor page surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/doctor/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { hint: "missing required scope: admin:usage" },
      }),
    }),
  );

  await page.goto("/doctor");
  await expect(page.getByTestId("doctor-load-error")).toContainText(
    /admin:usage/i,
  );
});
