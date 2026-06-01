import { expect, test } from "@playwright/test";

/**
 * Phase 63 identity flows — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /tenants renders the list, opens the create modal, creates a tenant,
 *   refreshes
 * - /teams renders + creates a team scoped to a tenant
 * - /keys generate flow: produces a secret in the show-once modal with
 *   a copy button + masked-only display in the list afterward
 * - 403 from /v1/admin/identity surfaces a clear error
 */

test.beforeEach(async ({ page }) => {
  // Seed a token so AppShell doesn't redirect to /login.
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pron_test_session");
  });
});

// --------------------------------------------------------------------------- //
// /tenants                                                                    //
// --------------------------------------------------------------------------- //

test("tenants page lists existing tenants + create-tenant flow round-trips", async ({
  page,
  context,
}) => {
  let tenants = [
    {
      id: "t1",
      name: "acme",
      created_at: 1700000000,
      webhook_url: null,
      oidc_subject: null,
    },
  ];

  await context.route("**/v1/admin/tenants*", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(tenants),
      });
      return;
    }
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as { name: string };
      const created = {
        id: "t2",
        name: body.name,
        created_at: 1700000001,
        webhook_url: null,
        oidc_subject: null,
      };
      tenants = [...tenants, created];
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/tenants");
  await expect(page.getByRole("heading", { name: "Tenants" })).toBeVisible();
  await expect(page.getByText("acme")).toBeVisible();

  // Open create modal, fill in name, submit.
  await page.getByRole("button", { name: /New tenant/i }).click();
  await expect(page.getByRole("heading", { name: "New tenant" })).toBeVisible();
  await page.getByLabel("Name").fill("globex");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  // Refresh fires; new tenant appears.
  await expect(page.getByText("globex")).toBeVisible({ timeout: 5_000 });
});

test("tenants page surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route("**/v1/admin/tenants*", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:identity" }),
    }),
  );

  await page.goto("/tenants");
  await expect(page.getByTestId("tenants-load-error")).toContainText(
    /admin:identity/i,
  );
});

// --------------------------------------------------------------------------- //
// /teams                                                                      //
// --------------------------------------------------------------------------- //

test("teams page creates a team scoped to a tenant", async ({
  page,
  context,
}) => {
  const tenants = [
    {
      id: "t1",
      name: "acme",
      created_at: 1700000000,
      webhook_url: null,
      oidc_subject: null,
    },
  ];
  let teams = [
    { id: "te1", tenant_id: "t1", name: "eng", created_at: 1700000000 },
  ];

  await context.route("**/v1/admin/tenants*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(tenants),
    }),
  );
  await context.route("**/v1/admin/teams*", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(teams),
      });
      return;
    }
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as {
        tenant_id: string;
        name: string;
      };
      const created = {
        id: "te2",
        tenant_id: body.tenant_id,
        name: body.name,
        created_at: 1700000001,
      };
      teams = [...teams, created];
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/teams");
  await expect(page.getByRole("heading", { name: "Teams" })).toBeVisible();
  await expect(page.getByText("eng")).toBeVisible();

  await page.getByRole("button", { name: /New team/i }).click();
  await expect(page.getByRole("heading", { name: "New team" })).toBeVisible();
  await page.getByLabel("Name").fill("platform");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByText("platform")).toBeVisible({ timeout: 5_000 });
});

// --------------------------------------------------------------------------- //
// /keys                                                                       //
// --------------------------------------------------------------------------- //

test("keys page generate-once modal shows the secret + masks it on the list", async ({
  page,
  context,
}) => {
  const teams = [
    { id: "te1", tenant_id: "t1", name: "eng", created_at: 1700000000 },
  ];
  let keys: Array<{
    id: string;
    team_id: string;
    prefix: string;
    label: string;
    scopes: string[];
    status: "active" | "revoked";
    created_at: number;
    revoked_at: number | null;
    last_used_at: number | null;
  }> = [];

  await context.route("**/v1/admin/teams*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(teams),
    }),
  );
  await context.route("**/v1/admin/keys*", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(keys),
      });
      return;
    }
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as {
        team_id: string;
        label?: string;
        scopes: string[];
      };
      const created = {
        id: "k1",
        team_id: body.team_id,
        prefix: "abc123def456",
        label: body.label ?? "",
        scopes: body.scopes,
        status: "active" as const,
        created_at: 1700000002,
        api_key: "pn_live_abc123def456_secretpartisHIDDEN",
      };
      keys = [
        {
          id: created.id,
          team_id: created.team_id,
          prefix: created.prefix,
          label: created.label,
          scopes: created.scopes,
          status: created.status,
          created_at: created.created_at,
          revoked_at: null,
          last_used_at: null,
        },
      ];
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/keys");
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();

  await page.getByRole("button", { name: /Generate key/i }).click();
  await expect(page.getByRole("heading", { name: "Generate API key" })).toBeVisible();
  // Label is optional; just submit with default scopes.
  await page.getByLabel("Label (optional)").fill("ci-bot");
  await page.getByRole("button", { name: "Generate", exact: true }).click();

  // Show-once secret modal renders with the full secret.
  await expect(page.getByRole("heading", { name: "Key created" })).toBeVisible();
  await expect(page.getByTestId("generated-secret")).toContainText(
    "pn_live_abc123def456_secretpartisHIDDEN",
  );

  // Acknowledge and close.
  await page.getByRole("button", { name: /I have saved this key/i }).click();

  // After acknowledgement the list shows the key WITHOUT the secret.
  await expect(page.getByTestId("keys-table")).toBeVisible();
  await expect(page.getByText("abc123def456")).toBeVisible();
  // The secret body should NOT appear anywhere on the post-modal page.
  await expect(page.locator("body")).not.toContainText("secretpartisHIDDEN");
});
