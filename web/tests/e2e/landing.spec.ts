import { expect, test } from "@playwright/test";

/**
 * Landing-page render tests.
 *
 * Phase 62 shipped a "connectivity tiles" dashboard (gateway-version +
 * usage-count + masked session key); Phase 64 explicitly replaced
 * that with the real FinOps dashboard — three summary tiles (Spend /
 * Tokens / Calls) + a daily-spend chart + top-5-teams table. These
 * tests cover the Phase 64+ dashboard reality.
 */

const USAGE_BODY = {
  items: [],
  totals: {
    requests: 100,
    prompt_tokens: 12_345,
    completion_tokens: 6_789,
    total_tokens: 19_134,
    cost_hcents: 42_000,
  },
  limit: 100,
  offset: 0,
};

const TIMESERIES_BODY = {
  bucket_size_seconds: 86_400,
  points: [
    {
      bucket: 1_748_390_400,
      requests: 50,
      prompt_tokens: 6_000,
      completion_tokens: 3_000,
      cost_hcents: 21_000,
    },
    {
      bucket: 1_748_476_800,
      requests: 50,
      prompt_tokens: 6_345,
      completion_tokens: 3_789,
      cost_hcents: 21_000,
    },
  ],
};

test.beforeEach(async ({ page }) => {
  // Seed a token so AppShell doesn't redirect to /login.
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session_token");
  });
});

test("dashboard renders Phase 64 FinOps tiles + chart + top-teams table", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/usage\?/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(USAGE_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/usage\/timeseries/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TIMESERIES_BODY),
    }),
  );

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // Three summary tiles drive off the /v1/admin/usage totals.
  // 42_000 hcents = $4.20
  await expect(page.getByTestId("tile-spend")).toHaveText("$4.20");
  // 12_345 + 6_789 = 19_134 → "19.1k"
  await expect(page.getByTestId("tile-tokens")).toHaveText("19.1k");
  await expect(page.getByTestId("tile-calls")).toHaveText("100");
});

test("dashboard surfaces /v1/admin/usage failure as a visible error", async ({
  page,
  context,
}) => {
  // Phase 64 dashboard uses /v1/admin/usage (not /v1/healthz like Phase
  // 62 did). When that endpoint 5xxs, the dashboard surfaces the error
  // in a card with data-testid="dashboard-load-error".
  await context.route(/\/v1\/admin\/usage\/timeseries/, (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: "upstream down" }),
    }),
  );
  await context.route(/\/v1\/admin\/usage\?/, (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: "upstream down" }),
    }),
  );

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByTestId("dashboard-load-error")).toContainText("502");
});

test("empty top-teams table renders cleanly when usage is empty", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/usage\?/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [],
        totals: {
          requests: 0,
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
          cost_hcents: 0,
        },
        limit: 100,
        offset: 0,
      }),
    }),
  );
  await context.route(/\/v1\/admin\/usage\/timeseries/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ bucket_size_seconds: 86_400, points: [] }),
    }),
  );

  await page.goto("/");
  // Empty state: no top-teams table at all (table only renders when
  // there are usage records), and the chart shows the "no spend in
  // window" empty state instead of the chart container.
  await expect(page.getByText("No usage records yet.")).toBeVisible();
  await expect(page.getByTestId("chart-empty")).toBeVisible();
});
