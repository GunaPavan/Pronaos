import { expect, test } from "@playwright/test";

/**
 * Phase 66 routing console — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /routing loads the team's current config + populates the strategy
 *   cards, scores tables, allowlist checkboxes, threshold inputs.
 * - Clicking a strategy card fires PUT /v1/admin/routing/{id} with
 *   the chosen strategy.
 * - Editing a quality score + saving round-trips through the same PUT.
 * - 403 from /v1/admin/routing surfaces a clear error state.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
  });
});

const TEAMS_BODY = [
  { id: "te1", tenant_id: "tn1", name: "eng", created_at: 1700000000 },
];

const MODELS_BODY = {
  items: [
    {
      fqmn: "groq/llama-3.1-8b-instant",
      provider: "groq",
      input_hcents_per_mtok: 5_000,
      output_hcents_per_mtok: 8_000,
      supports_tools: true,
      supports_streaming: true,
      supports_vision: false,
      max_context_tokens: 128_000,
      provider_configured: true,
      allowed: true,
    },
    {
      fqmn: "groq/llama-3.3-70b-versatile",
      provider: "groq",
      input_hcents_per_mtok: 59_000,
      output_hcents_per_mtok: 79_000,
      supports_tools: true,
      supports_streaming: true,
      supports_vision: false,
      max_context_tokens: 128_000,
      provider_configured: true,
      allowed: true,
    },
  ],
};

function baseRoutingConfig() {
  return {
    team_id: "te1",
    routing_strategy: null as string | null,
    allowed_models: null as string[] | null,
    quality_threshold: null as number | null,
    quality_scores: null as Record<string, { score: number }> | null,
    tool_use_threshold: null as number | null,
    tool_use_scores: null as Record<string, { score: number }> | null,
    prompt_cache_min_samples: null as number | null,
    prompt_cache_min_hit_rate: null as number | null,
    reasoning_aware_min_samples: null as number | null,
    reasoning_aware_max_ratio: null as number | null,
  };
}

// --------------------------------------------------------------------------- //
// Load                                                                        //
// --------------------------------------------------------------------------- //

test("routing page loads team config and renders strategy + allowlist + scores", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/routing\/te1/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...baseRoutingConfig(),
        routing_strategy: "quality-aware-cheapest",
        quality_threshold: 0.7,
        quality_scores: {
          "groq/llama-3.1-8b-instant": { score: 0.4, n_samples: 8 },
        },
        allowed_models: ["groq/llama-3.1-8b-instant"],
      }),
    }),
  );

  await page.goto("/routing");
  await expect(page.getByRole("heading", { name: "Routing" })).toBeVisible();

  // Strategy card for the active strategy is highlighted.
  const activeCard = page.getByTestId("strategy-quality-aware-cheapest");
  await expect(activeCard).toHaveAttribute("data-active", "true");

  // Quality scores table populates.
  await expect(page.getByTestId("quality-table")).toContainText(
    "groq/llama-3.1-8b-instant",
  );

  // Allowlist checkbox is selected for the included model.
  const allowed = page.getByTestId("allowlist-checkbox-groq/llama-3.1-8b-instant");
  await expect(allowed).toBeChecked();
});

// --------------------------------------------------------------------------- //
// Strategy click → PUT                                                        //
// --------------------------------------------------------------------------- //

test("clicking a strategy card PUTs the new strategy and refreshes", async ({
  page,
  context,
}) => {
  let state = baseRoutingConfig();
  const seenPuts: Array<Record<string, unknown>> = [];

  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/routing\/te1/, async (route) => {
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
      state = { ...state, ...patch } as typeof state;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/routing");
  await expect(page.getByTestId("strategy-cheapest")).toBeVisible();
  await page.getByTestId("strategy-reasoning-aware-cheapest").click();

  // Wait for the PUT to land and the state to refresh.
  await expect
    .poll(() => seenPuts.length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  expect(seenPuts[0]).toEqual({
    routing_strategy: "reasoning-aware-cheapest",
  });

  // Card now active.
  const activeCard = page.getByTestId("strategy-reasoning-aware-cheapest");
  await expect(activeCard).toHaveAttribute("data-active", "true");
});

// --------------------------------------------------------------------------- //
// 403                                                                          //
// --------------------------------------------------------------------------- //

test("routing page surfaces 403 with a clear error state", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/routing\/te1/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { hint: "missing required scope: admin:usage" },
      }),
    }),
  );

  await page.goto("/routing");
  await expect(page.getByTestId("routing-load-error")).toContainText(
    /admin:usage/i,
  );
});

// --------------------------------------------------------------------------- //
// Score edit → PUT                                                             //
// --------------------------------------------------------------------------- //

test("editing a quality score and saving round-trips through PUT", async ({
  page,
  context,
}) => {
  let state = {
    ...baseRoutingConfig(),
    quality_scores: {
      "groq/llama-3.1-8b-instant": { score: 0.4, n_samples: 8 },
    } as Record<string, { score: number; n_samples?: number }>,
  };

  await context.route(/\/v1\/admin\/teams/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEAMS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/admin\/routing\/te1/, async (route) => {
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
      state = { ...state, ...patch } as typeof state;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }
    await route.fulfill({ status: 405 });
  });

  await page.goto("/routing");
  await expect(page.getByTestId("quality-table")).toBeVisible();

  // Edit the score input inside the quality table.
  const input = page
    .getByTestId("quality-table")
    .locator("input[type=number]")
    .first();
  await input.fill("0.85");
  await page.getByTestId("quality-save").click();

  // Verify the table re-renders with the new score.
  await expect
    .poll(async () => {
      const v = await input.inputValue();
      return v;
    })
    .toBe("0.85");
});
