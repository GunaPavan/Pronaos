import { expect, test } from "@playwright/test";

/**
 * Phase 70 webhook console — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /webhooks loads the tenant's current config.
 * - Save fires PUT with url + secret.
 * - Test-ping fires POST and renders the result card with HTTP status.
 * - 403 from GET surfaces a clear error state.
 */

test.beforeEach(async ({ page, context }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
  // The webhooks page calls listTenants() for the picker. Mock it
  // globally so the page doesn't redirect to /login when the dev
  // proxy fails to reach the backend.
  await context.route(/\/v1\/admin\/tenants/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TENANTS_BODY),
    }),
  );
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

const UNCONFIGURED_WEBHOOK = {
  tenant_id: "tn1",
  url: null,
  secret_set: false,
};

const CONFIGURED_WEBHOOK = {
  tenant_id: "tn1",
  url: "https://hook.example.com/events",
  secret_set: true,
};

// --------------------------------------------------------------------------- //
// Load                                                                        //
// --------------------------------------------------------------------------- //

test("webhooks page loads unconfigured state", async ({ page, context }) => {
  await context.route(/\/v1\/admin\/tenants/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TENANTS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/webhooks\/tn1$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(UNCONFIGURED_WEBHOOK),
    }),
  );

  await page.goto("/webhooks");
  await expect(page.getByRole("heading", { name: "Webhooks" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("url-input")).toHaveValue("", { timeout: 10_000 });
  // Test ping button is disabled when not configured.
  await expect(page.getByTestId("test-ping-button")).toBeDisabled();
});

// --------------------------------------------------------------------------- //
// Save (PUT)                                                                  //
// --------------------------------------------------------------------------- //

test("save fires PUT with url + secret and updates the config", async ({
  page,
  context,
}) => {
  let state = { ...UNCONFIGURED_WEBHOOK };
  const seenPuts: Array<Record<string, unknown>> = [];

  await context.route(/\/v1\/admin\/tenants/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TENANTS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/webhooks\/tn1$/, async (route) => {
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
      state = { ...CONFIGURED_WEBHOOK };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/webhooks");
  await page.getByTestId("url-input").fill("https://hook.example.com/events");
  await page.getByTestId("secret-input").fill("a-very-long-secret-32-chars-ok!");
  await page.getByTestId("save-button").click();

  await expect.poll(() => seenPuts.length, { timeout: 5_000 }).toBeGreaterThan(0);
  expect(seenPuts[0]).toMatchObject({
    url: "https://hook.example.com/events",
    secret: "a-very-long-secret-32-chars-ok!",
  });
  // Test ping button should now be enabled.
  await expect(page.getByTestId("test-ping-button")).not.toBeDisabled();
});

// --------------------------------------------------------------------------- //
// Test ping                                                                   //
// --------------------------------------------------------------------------- //

test("test-ping fires POST and renders HTTP status + response body", async ({
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
  await context.route(/\/v1\/admin\/webhooks\/tn1$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(CONFIGURED_WEBHOOK),
    }),
  );
  await context.route(/\/v1\/admin\/webhooks\/tn1\/test/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "tn1",
        http_status: 200,
        response_body: "OK",
        error: null,
        signed: true,
        delivery_id: "abc123def456",
      }),
    }),
  );

  await page.goto("/webhooks");
  await expect(page.getByTestId("test-ping-button")).not.toBeDisabled();
  await page.getByTestId("test-ping-button").click();

  await expect(page.getByTestId("ping-result")).toBeVisible({ timeout: 5_000 });
  await expect(page.getByTestId("ping-result")).toContainText("HTTP 200");
  await expect(page.getByTestId("ping-result")).toContainText("HMAC signed");
});

// --------------------------------------------------------------------------- //
// 403                                                                         //
// --------------------------------------------------------------------------- //

test("webhooks page surfaces 403 with a clear error state", async ({
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
  await context.route(/\/v1\/admin\/webhooks\/tn1$/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:usage" }),
    }),
  );

  await page.goto("/webhooks");
  await expect(page.getByTestId("webhook-load-error")).toContainText(/admin:usage/i);
});
