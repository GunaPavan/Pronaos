import { expect, test } from "@playwright/test";

/**
 * Phase 64 FinOps flows — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - Dashboard summary tiles populate from /v1/admin/usage totals
 * - /usage chart + table render; team filter triggers a refetch with
 *   the chosen team_id
 * - /usage/budgets editor loads + PUT persists the cap (round-trips
 *   back into the meter)
 * - /usage surfaces a 403 error state when the key lacks admin:usage
 */

test.beforeEach(async ({ page }) => {
  // Seed a token so AppShell doesn't redirect to /login.
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const usageBody = {
  items: [
    {
      ts: "2026-05-30T00:00:00Z",
      tenant_id: "tn1",
      team_id: "te1",
      key_id: "k1",
      provider: "groq",
      model: "llama3-8b",
      prompt_tokens: 1200,
      completion_tokens: 800,
      cost_hcents: 1500,
      status: "ok",
      request_id: "r1",
    },
    {
      ts: "2026-05-30T01:00:00Z",
      tenant_id: "tn1",
      team_id: "te2",
      key_id: "k2",
      provider: "groq",
      model: "llama3-70b",
      prompt_tokens: 400,
      completion_tokens: 600,
      cost_hcents: 9000,
      status: "ok",
      request_id: "r2",
    },
  ],
  totals: {
    requests: 2,
    prompt_tokens: 1600,
    completion_tokens: 1400,
    total_tokens: 3000,
    cost_hcents: 10500,
  },
  limit: 100,
  offset: 0,
};

const timeseriesBody = {
  bucket_size_seconds: 86_400,
  points: [
    {
      bucket: 1_748_390_400,
      requests: 1,
      prompt_tokens: 1200,
      completion_tokens: 800,
      cost_hcents: 1500,
    },
    {
      bucket: 1_748_476_800,
      requests: 1,
      prompt_tokens: 400,
      completion_tokens: 600,
      cost_hcents: 9000,
    },
  ],
};

// --------------------------------------------------------------------------- //
// Dashboard                                                                   //
// --------------------------------------------------------------------------- //

test("dashboard populates summary tiles + top-teams table from /v1/admin/usage", async ({
  page,
  context,
}) => {
  // Use regex matchers: globs treat `?` as a single-char wildcard, so
  // `**/v1/admin/usage?**` would also match `/v1/admin/usage/timeseries`.
  await context.route(/\/v1\/admin\/usage\?/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(usageBody),
    }),
  );
  await context.route(/\/v1\/admin\/usage\/timeseries/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(timeseriesBody),
    }),
  );

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  // 10500 hcents = $1.05
  await expect(page.getByTestId("tile-spend")).toHaveText("$1.05");
  // 1600 + 1400 = 3000 tokens → "3.0k"
  await expect(page.getByTestId("tile-tokens")).toHaveText("3.0k");
  await expect(page.getByTestId("tile-calls")).toHaveText("2");
  await expect(page.getByTestId("top-teams-table")).toBeVisible();
  // Team te2 spent more (9000 hcents = $0.90) → first row.
  await expect(page.getByTestId("top-teams-table")).toContainText("te2");
  await expect(page.getByTestId("top-teams-table")).toContainText("$0.90");
});

// --------------------------------------------------------------------------- //
// /usage                                                                      //
// --------------------------------------------------------------------------- //

test("usage page renders chart + table; team filter triggers a re-fetch with team_id", async ({
  page,
  context,
}) => {
  const teamsBody = [
    { id: "te1", tenant_id: "tn1", name: "eng", created_at: 1700000000 },
    { id: "te2", tenant_id: "tn1", name: "platform", created_at: 1700000000 },
  ];

  const seenTeamIds: string[] = [];

  await context.route("**/v1/admin/teams*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(teamsBody),
    }),
  );
  await context.route(/\/v1\/admin\/usage\?/, (route) => {
    const url = new URL(route.request().url());
    const teamId = url.searchParams.get("team_id") ?? "";
    seenTeamIds.push(teamId);
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(usageBody),
    });
  });
  await context.route(/\/v1\/admin\/usage\/timeseries/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(timeseriesBody),
    }),
  );

  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(page.getByTestId("usage-table")).toBeVisible();
  await expect(page.getByTestId("usage-chart")).toBeVisible();

  // Switching the team filter should fire another GET /v1/admin/usage
  // with team_id in the query string.
  await page.locator("#team").selectOption("te1");
  await expect.poll(() => seenTeamIds.includes("te1")).toBe(true);
});

test("usage page surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/usage/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:usage" }),
    }),
  );

  await page.goto("/usage");
  await expect(page.getByTestId("usage-load-error")).toContainText(
    /admin:usage/i,
  );
});

// --------------------------------------------------------------------------- //
// /usage/budgets                                                              //
// --------------------------------------------------------------------------- //

test("budgets editor loads current period + PUT round-trips back into the meter", async ({
  page,
  context,
}) => {
  const teamsBody = [
    { id: "te1", tenant_id: "tn1", name: "eng", created_at: 1700000000 },
  ];

  // Future reset moment: ~2026-07-01.
  let budget = {
    team_id: "te1",
    monthly_token_budget: 10_000,
    current_period_tokens: 5_000,
    monthly_cost_hcents_budget: null as number | null,
    current_period_cost_hcents: 0,
    period_resets_at: 1_782_000_000,
  };

  await context.route("**/v1/admin/teams*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(teamsBody),
    }),
  );
  await context.route("**/v1/admin/budgets/te1", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(budget),
      });
      return;
    }
    if (req.method() === "PUT") {
      const body = JSON.parse(req.postData() ?? "{}") as {
        monthly_token_budget?: number | null;
        monthly_cost_hcents_budget?: number | null;
      };
      budget = {
        ...budget,
        monthly_token_budget:
          body.monthly_token_budget === undefined
            ? budget.monthly_token_budget
            : body.monthly_token_budget,
        monthly_cost_hcents_budget:
          body.monthly_cost_hcents_budget === undefined
            ? budget.monthly_cost_hcents_budget
            : body.monthly_cost_hcents_budget,
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(budget),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/usage/budgets");
  await expect(page.getByRole("heading", { name: "Budgets" })).toBeVisible();
  // Initial meter: 5k / 10k tokens.
  await expect(page.getByTestId("token-progress")).toContainText("5.0k");
  await expect(page.getByTestId("token-progress")).toContainText("10.0k");

  // Raise the token cap to 20_000.
  await page.getByLabel("Monthly token cap").fill("20000");
  await page.getByRole("button", { name: /Save/ }).click();

  // The meter rebinds to the new ceiling.
  await expect(page.getByTestId("token-progress")).toContainText("20.0k", {
    timeout: 5_000,
  });
});
