import { expect, test } from "@playwright/test";

/**
 * Phase 66 gap fill — /routing/observations e2e tests.
 *
 * Mocks:
 *   GET /v1/admin/teams                       → single team
 *   GET /v1/admin/team/{id}/prompt-cache-stats → cache stats with two models
 *   GET /v1/admin/team/{id}/reasoning-stats    → reasoning stats with one model
 */

const TEAM = { id: "team_obs_001", name: "Obs Test Team", tenant_id: "ten_001", created_at: 1700000000 };

const CACHE_STATS = {
  team_id: TEAM.id,
  min_samples: 20,
  min_hit_rate: 0.1,
  stats: [
    {
      fqmn: "anthropic/claude-haiku-4-5",
      n_samples: 120,
      prompt_tokens: 500000,
      cached_tokens: 87000,
      saved_hcents: 3480,
      hit_rate: 0.174,
    },
    {
      fqmn: "anthropic/claude-sonnet-4-5",
      n_samples: 45,
      prompt_tokens: 200000,
      cached_tokens: 15000,
      saved_hcents: 600,
      hit_rate: 0.075,
    },
  ],
};

const REASONING_STATS = {
  team_id: TEAM.id,
  min_samples: 20,
  max_ratio: null,
  stats: [
    {
      fqmn: "anthropic/claude-sonnet-4-5",
      n_samples: 45,
      completion_tokens: 9000,
      reasoning_tokens: 4500,
      ratio: 0.5,
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

test("observations page renders prompt-cache table and reasoning table", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([TEAM]),
    }),
  );
  await context.route(/\/v1\/admin\/team\/team_obs_001\/prompt-cache-stats/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(CACHE_STATS),
    }),
  );
  await context.route(/\/v1\/admin\/team\/team_obs_001\/reasoning-stats/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(REASONING_STATS),
    }),
  );

  await page.goto("/routing/observations");
  await expect(page.getByRole("heading", { name: /Routing Observations/i })).toBeVisible();

  // Team picker auto-selects the only team.
  await expect(page.getByTestId("obs-team-select")).toHaveValue(TEAM.id);

  // Prompt-cache table appears with the first model's hit rate.
  const cacheTable = page.getByTestId("obs-cache-table");
  await expect(cacheTable).toBeVisible({ timeout: 5_000 });
  await expect(cacheTable).toContainText("anthropic/claude-haiku-4-5");
  await expect(cacheTable).toContainText("17.4%");

  // Reasoning table appears with the model's ratio.
  const reasoningTable = page.getByTestId("obs-reasoning-table");
  await expect(reasoningTable).toBeVisible({ timeout: 5_000 });
  await expect(reasoningTable).toContainText("anthropic/claude-sonnet-4-5");
  await expect(reasoningTable).toContainText("0.500×");
});

test("observations page shows empty-state when no stats", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([TEAM]),
    }),
  );
  await context.route(/\/v1\/admin\/team\/team_obs_001\/prompt-cache-stats/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...CACHE_STATS, stats: [] }),
    }),
  );
  await context.route(/\/v1\/admin\/team\/team_obs_001\/reasoning-stats/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...REASONING_STATS, stats: [] }),
    }),
  );

  await page.goto("/routing/observations");
  await expect(page.getByRole("heading", { name: /Routing Observations/i })).toBeVisible();

  // Both tables absent; empty-state text appears.
  await expect(page.getByText(/No prompt-cache observations yet/i)).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText(/No reasoning observations yet/i)).toBeVisible();
});
