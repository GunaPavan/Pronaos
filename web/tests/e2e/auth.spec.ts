import { expect, test } from "@playwright/test";

/**
 * Auth flow e2e — Phase 62 / Claim #49.
 *
 * Pronaos's UI is auth-gated: every route except /login redirects
 * to /login when no token is present in localStorage. These tests
 * cover the four routes through that gate:
 *
 *  1. Visiting / unauthenticated → redirect to /login
 *  2. Submitting a bad key → toast, no redirect
 *  3. Submitting a good key → tokens persist, redirect to /
 *  4. Sign-out → clears token, sends back to /login
 *
 * Backend is mocked at the network layer with page.route — these
 * tests do not require a running Pronaos backend.
 */

test.beforeEach(async ({ context }) => {
  // Stub every /v1/* call. Specific specs override per-route below.
  await context.route("**/v1/**", (route) => route.fulfill({ status: 200, body: "{}" }));
});

test("unauthenticated user is redirected from / to /login", async ({ page }) => {
  await page.goto("/");
  await page.waitForURL("**/login");
  await expect(page.getByText(/Sign in to Pronaos/i)).toBeVisible();
});

test("bad API key shows error, stays on /login", async ({ page, context }) => {
  // /v1/health succeeds (gateway is reachable) but /v1/admin/usage returns 401.
  await context.route("**/v1/healthz", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "0.1.0" }),
    }),
  );
  await context.route("**/v1/admin/usage**", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "invalid api key" }),
    }),
  );

  await page.goto("/login");
  await page.getByLabel(/API key/i).fill("pron_invalid_key");
  await page.getByRole("button", { name: /continue/i }).click();

  // We expect to stay on /login and see an error toast.
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText(/lacks the admin:usage scope/i)).toBeVisible({
    timeout: 10_000,
  });
});

test("good API key lands user on the Phase 64 FinOps dashboard", async ({
  page,
  context,
}) => {
  await context.route("**/v1/healthz", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "0.1.0" }),
    }),
  );
  // Phase 64 dashboard hits /v1/admin/usage AND /v1/admin/usage/timeseries.
  // Use distinct regex matchers (glob `?` would over-match the timeseries
  // path).
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

  await page.goto("/login");
  await page.getByLabel(/API key/i).fill("pn_valid_test_key_12345");
  await page.getByRole("button", { name: /continue/i }).click();

  await page.waitForURL((url) => !url.pathname.endsWith("/login"), {
    timeout: 10_000,
  });
  await expect(page.getByRole("heading", { name: /Dashboard/i })).toBeVisible();
  // The Phase 64 dashboard has three summary tiles (Spend / Tokens / Calls).
  // On zero usage the spend tile reads "$0.0000".
  await expect(page.getByTestId("tile-spend")).toBeVisible();
  await expect(page.getByTestId("tile-calls")).toHaveText("0");
});

test("sign-out clears token + sends back to /login", async ({ page, context }) => {
  await context.route("**/v1/healthz", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "0.1.0" }),
    }),
  );
  await context.route("**/v1/admin/usage**", (route) =>
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

  // Pre-seed the token directly via localStorage so we land on the
  // dashboard without going through the login form.
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pron_existing_session");
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Dashboard/i })).toBeVisible();

  await page.getByRole("button", { name: /sign out/i }).click();
  await page.waitForURL("**/login");
  const stored = await page.evaluate(() =>
    window.localStorage.getItem("pronaos.api_key"),
  );
  expect(stored).toBeNull();
});
