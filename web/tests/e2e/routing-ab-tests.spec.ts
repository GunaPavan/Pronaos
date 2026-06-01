import { expect, test } from "@playwright/test";

/**
 * Phase 66 gap fill — /routing/ab-tests e2e tests.
 *
 * Mocks:
 *   GET /v1/admin/teams               → single team
 *   GET /v1/admin/team/{id}/ab-test   → active test with two arms + t-test result
 */

const TEAM = { id: "team_abtest_001", name: "AB Test Team", tenant_id: "ten_001", created_at: 1700000000 };

const AB_RESPONSE = {
  team_id: TEAM.id,
  test_id: "abtest-uuid-1234",
  test_name: "Haiku vs Sonnet cost test",
  started_at: "2026-05-01T00:00:00",
  arm_a_model: "anthropic/claude-haiku-4-5",
  arm_b_model: "anthropic/claude-sonnet-4-5",
  arm_a_stats: {
    arm: "a",
    n: 200,
    mean_cost_hcents: 45.2,
    mean_total_tokens: 850.0,
    median_total_tokens: 800.0,
  },
  arm_b_stats: {
    arm: "b",
    n: 210,
    mean_cost_hcents: 182.7,
    mean_total_tokens: 870.0,
    median_total_tokens: 820.0,
  },
  t_test: {
    t_statistic: -12.34,
    p_value: 0.0001,
    df: 398.5,
    cohens_d: -1.23,
    ci_low: -150.0,
    ci_high: -110.0,
    significant_at_05: true,
  },
};

const NO_TEST_RESPONSE = {
  team_id: TEAM.id,
  test_id: null,
  test_name: null,
  started_at: null,
  arm_a_model: null,
  arm_b_model: null,
  arm_a_stats: null,
  arm_b_stats: null,
  t_test: null,
};

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

test("ab-tests page renders active test config + arms table + t-test result", async ({
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
  await context.route(/\/v1\/admin\/team\/team_abtest_001\/ab-test/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(AB_RESPONSE),
    }),
  );

  await page.goto("/routing/ab-tests");
  await expect(page.getByRole("heading", { name: /A\/B Tests/i })).toBeVisible();

  // Team picker.
  await expect(page.getByTestId("abtest-team-select")).toHaveValue(TEAM.id);

  // Config card shows test name.
  await expect(page.getByTestId("abtest-config-card")).toContainText(
    "Haiku vs Sonnet cost test",
    { timeout: 5_000 },
  );
  await expect(page.getByTestId("abtest-config-card")).toContainText("abtest-u"); // badge shows first 8 chars

  // Arms table shows both rows.
  const armsTable = page.getByTestId("abtest-arms-table");
  await expect(armsTable).toBeVisible();
  await expect(armsTable).toContainText("200");  // arm A n
  await expect(armsTable).toContainText("210");  // arm B n

  // T-test card shows p-value.
  await expect(page.getByTestId("abtest-ttest-card")).toBeVisible();
  await expect(page.getByTestId("abtest-pvalue")).toContainText("0.0001");

  // 95% CI shown.
  await expect(page.getByTestId("abtest-ci")).toBeVisible();
});

test("ab-tests page shows no-test state when team has no active test", async ({
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
  await context.route(/\/v1\/admin\/team\/team_abtest_001\/ab-test/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(NO_TEST_RESPONSE),
    }),
  );

  await page.goto("/routing/ab-tests");
  await expect(page.getByRole("heading", { name: /A\/B Tests/i })).toBeVisible();
  await expect(page.getByTestId("abtest-none")).toBeVisible({ timeout: 5_000 });
});
