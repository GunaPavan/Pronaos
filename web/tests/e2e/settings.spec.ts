import { expect, test } from "@playwright/test";

/**
 * Phase 71 settings page — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /settings renders gateway config cards with enabled/disabled badges.
 * - OIDC tenant editor saves via PATCH with oidc_subject in body.
 * - 403 from /v1/admin/settings surfaces a clear error state.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const TENANTS_BODY = [
  {
    id: "tn1",
    name: "acme",
    created_at: 1700000000,
    webhook_url: null,
    oidc_subject: null,
  },
];

const SETTINGS_BODY = {
  redis_configured: true,
  semantic_cache_enabled: false,
  anthropic_configured: true,
  groq_configured: true,
  openai_configured: false,
  bedrock_configured: false,
  vertex_configured: false,
  mcp_enabled: true,
  presidio_enabled: false,
  singleflight_distributed: false,
  oidc_configured: true,
  oidc_issuer: "https://auth.example.com",
  database_scheme: "sqlite+aiosqlite",
};

// --------------------------------------------------------------------------- //
// Load                                                                        //
// --------------------------------------------------------------------------- //

test("settings page renders gateway config cards with correct badges", async ({
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
  await context.route(/\/v1\/admin\/settings/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(SETTINGS_BODY),
    }),
  );

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  // Redis configured → enabled badge
  await expect(page.getByTestId("feature-redis_configured")).toContainText("enabled");
  // OpenAI not configured → disabled badge
  await expect(page.getByTestId("feature-openai_configured")).toContainText("disabled");
  // OIDC configured → enabled badge + issuer URL visible
  await expect(page.getByTestId("feature-oidc_configured")).toContainText("enabled");
  await expect(page.getByTestId("config-grid")).toContainText("auth.example.com");
});

// --------------------------------------------------------------------------- //
// OIDC save                                                                   //
// --------------------------------------------------------------------------- //

test("OIDC save fires PATCH with oidc_subject in body", async ({
  page,
  context,
}) => {
  let tenantState = { ...TENANTS_BODY[0]! };
  const seenPatches: Array<Record<string, unknown>> = [];

  await context.route(/\/v1\/admin\/tenants(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([tenantState]),
    }),
  );
  await context.route(/\/v1\/admin\/tenants\/tn1$/, async (route) => {
    const req = route.request();
    if (req.method() === "PATCH") {
      const patch = JSON.parse(req.postData() ?? "{}") as Record<string, unknown>;
      seenPatches.push(patch);
      tenantState = { ...tenantState, ...patch } as typeof tenantState;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(tenantState),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });
  await context.route(/\/v1\/admin\/settings/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(SETTINGS_BODY),
    }),
  );

  await page.goto("/settings");
  await expect(page.getByTestId("oidc-subject-input")).toBeVisible();

  await page.getByTestId("oidc-subject-input").fill("auth0|user123");
  await page.getByTestId("oidc-save-button").click();

  await expect
    .poll(() => seenPatches.length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  expect(seenPatches[0]).toMatchObject({ oidc_subject: "auth0|user123" });
});

// --------------------------------------------------------------------------- //
// 403                                                                         //
// --------------------------------------------------------------------------- //

test("settings page surfaces 403 with a clear error state", async ({
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
  await context.route(/\/v1\/admin\/settings/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:usage" }),
    }),
  );

  await page.goto("/settings");
  await expect(page.getByTestId("settings-load-error")).toContainText(/admin:usage/i);
});
