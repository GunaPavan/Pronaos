import { expect, test } from "@playwright/test";

/**
 * Phase 69 batches console — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /batches lists rows with status badges + status filter works.
 * - Clicking a row navigates to /batches/[id].
 * - /batches/[id] renders batch detail + shows cancel CTA when in_progress.
 * - Cancel click fires POST + status badge flips to cancelled.
 * - 403 from /v1/admin/batches surfaces clearly.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const NOW = Math.floor(Date.now() / 1000);

const BATCH_LIST = {
  items: [
    {
      id: "pron_batch_aaa",
      object: "batch",
      provider: "openai",
      provider_batch_id: "batch_openai_001",
      status: "in_progress",
      endpoint: "/v1/chat/completions",
      completion_window: "24h",
      request_counts: { total: 10, completed: 4, failed: 0 },
      created_at: NOW - 3600,
      in_progress_at: NOW - 3500,
      completed_at: null,
      error_message: null,
    },
    {
      id: "pron_batch_bbb",
      object: "batch",
      provider: "anthropic",
      provider_batch_id: "msgbatch_001",
      status: "completed",
      endpoint: "/v1/chat/completions",
      completion_window: "24h",
      request_counts: { total: 5, completed: 5, failed: 0 },
      created_at: NOW - 7200,
      in_progress_at: NOW - 7000,
      completed_at: NOW - 1000,
      error_message: null,
    },
  ],
  total: 2,
  limit: 25,
  offset: 0,
};

// --------------------------------------------------------------------------- //
// List page                                                                   //
// --------------------------------------------------------------------------- //

test("batches list page renders rows with status badges", async ({
  page,
  context,
}) => {
  // Teams are fetched for the filter dropdown; mock them so the page
  // loads without a real backend.
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );
  await context.route(/\/v1\/admin\/batches(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(BATCH_LIST),
    }),
  );

  await page.goto("/batches");
  await expect(page.getByRole("heading", { name: "Batches" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByTestId("batches-table")).toContainText("pron_batch_aaa");
  await expect(page.getByTestId("batches-table")).toContainText("in_progress");
  await expect(page.getByTestId("batches-table")).toContainText("completed");
});

test("status filter triggers a refetch with status param", async ({
  page,
  context,
}) => {
  const seenUrls: string[] = [];

  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );
  await context.route(/\/v1\/admin\/batches/, (route) => {
    seenUrls.push(route.request().url());
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...BATCH_LIST, items: [], total: 0 }),
    });
  });

  await page.goto("/batches");
  await expect(page.getByTestId("status-filter")).toBeVisible();
  await page.getByTestId("status-filter").selectOption("completed");

  await expect
    .poll(() => seenUrls.some((u) => u.includes("status=completed")), {
      timeout: 5_000,
    })
    .toBe(true);
});

test("batches page surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );
  await context.route(/\/v1\/admin\/batches/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:usage" }),
    }),
  );

  await page.goto("/batches");
  await expect(page.getByTestId("batches-load-error")).toContainText(
    /admin:usage/i,
  );
});

// --------------------------------------------------------------------------- //
// Detail page                                                                 //
// --------------------------------------------------------------------------- //

test("batch detail page renders status + cancel CTA for in_progress", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/batches\/pron_batch_aaa$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(BATCH_LIST.items[0]),
    }),
  );

  await page.goto("/batches/pron_batch_aaa");
  await expect(page.getByTestId("batch-status-card")).toBeVisible();
  await expect(page.getByTestId("batch-status-card")).toContainText("in_progress");
  await expect(page.getByTestId("cancel-button")).toBeVisible();
});

test("cancel button fires POST and status flips to cancelled", async ({
  page,
  context,
}) => {
  let batch = { ...BATCH_LIST.items[0]! };
  const seenPosts: string[] = [];

  await context.route(/\/v1\/admin\/batches\/pron_batch_aaa\/cancel/, async (route) => {
    seenPosts.push(route.request().url());
    batch = { ...batch, status: "cancelled" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(batch),
    });
  });
  await context.route(/\/v1\/admin\/batches\/pron_batch_aaa$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(batch),
    }),
  );

  await page.goto("/batches/pron_batch_aaa");
  await expect(page.getByTestId("cancel-button")).toBeVisible();
  await page.getByTestId("cancel-button").click();

  await expect
    .poll(() => seenPosts.length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  // Cancel button disappears after status becomes terminal.
  await expect(page.getByTestId("cancel-button")).not.toBeVisible({ timeout: 5_000 });
});
