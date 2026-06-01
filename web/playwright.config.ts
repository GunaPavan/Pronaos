import { defineConfig, devices } from "@playwright/test";

/**
 * Pronaos admin UI — Playwright config.
 *
 * Dev tests run against the Next.js dev server on :3000. The webServer
 * block boots `next dev` automatically when tests run; tests close it
 * on teardown.
 *
 * Backend calls are NOT hit in these tests — each spec uses
 * page.route(...) to mock /v1/* responses. Real backend integration is
 * covered separately in Pronaos's pytest suite + the verify scripts.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : "list",
  timeout: 60_000,
  expect: {
    // 10s global expect timeout; individual assertions that need more
    // time specify their own {timeout} option.
    timeout: 10_000,
  },
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    // Never reuse an existing server — always boot a fresh dev instance
    // so code changes are guaranteed to be in effect during e2e runs.
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
