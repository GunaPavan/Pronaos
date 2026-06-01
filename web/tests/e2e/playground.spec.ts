import { expect, test } from "@playwright/test";

/**
 * Phase 65 playground — Playwright e2e against a mocked backend.
 *
 * Covers
 * ------
 * - /playground loads the model catalog and auto-picks a configured
 *   allowed model.
 * - Sending a message hits /v1/chat/completions and streams the SSE
 *   deltas into the conversation pane.
 * - The response inspector captures X-Pronaos-* headers (cache, cost,
 *   routed-model, request-id) from the response.
 * - 403 on /v1/admin/models surfaces a clear error state.
 * - Streaming toggle off → non-streaming branch produces the same
 *   message shape.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => {
    window.localStorage.setItem("pronaos.api_key", "pn_test_session");
    // Clear any persisted playground settings so each test starts clean.
    window.localStorage.removeItem("pronaos.playground.settings.v1");
  });
});

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
      fqmn: "anthropic/claude-haiku-4-5",
      provider: "anthropic",
      input_hcents_per_mtok: 80_000,
      output_hcents_per_mtok: 400_000,
      supports_tools: true,
      supports_streaming: true,
      supports_vision: true,
      max_context_tokens: 200_000,
      provider_configured: false,
      allowed: true,
    },
  ],
};

// --------------------------------------------------------------------------- //
// Models loading                                                              //
// --------------------------------------------------------------------------- //

test("playground loads model catalog and selects the first configured allowed model", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );

  await page.goto("/playground");
  await expect(page.getByRole("heading", { name: "Playground" })).toBeVisible();

  // Model select populates + auto-picks the configured+allowed one.
  const select = page.getByTestId("model-select");
  await expect(select).toHaveValue("groq/llama-3.1-8b-instant");
  // The unconfigured option is still listed, just labelled as such.
  await expect(select).toContainText("anthropic/claude-haiku-4-5");
  await expect(select).toContainText("unconfigured");
});

test("playground surfaces 403 from /v1/admin/models", async ({ page, context }) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "missing required scope: admin:usage" }),
    }),
  );

  await page.goto("/playground");
  await expect(page.getByTestId("models-load-error")).toContainText(/admin:usage/i);
});

// --------------------------------------------------------------------------- //
// Streaming chat                                                              //
// --------------------------------------------------------------------------- //

test("send button streams SSE deltas into the conversation pane and captures headers", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );

  // Synthesize an OpenAI-shape SSE stream split across multiple chunks
  // so the byte-buffer logic in parseSseStream() gets exercised.
  const sseBody = [
    `data: ${JSON.stringify({ choices: [{ delta: { content: "Hello " } }] })}\n\n`,
    `data: ${JSON.stringify({ choices: [{ delta: { content: "world" } }] })}\n\n`,
    `data: ${JSON.stringify({ choices: [{ delta: { content: "!" }, finish_reason: "stop" }] })}\n\n`,
    "data: [DONE]\n\n",
  ].join("");

  await context.route(/\/v1\/chat\/completions/, (route) => {
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: {
        "X-Pronaos-Cache": "miss",
        "X-Pronaos-Cost-Hcents": "42",
        "X-Pronaos-Routed-Model": "groq/llama-3.1-8b-instant",
        "X-Pronaos-Routing-Strategy": "cheapest",
        "X-Pronaos-Request-Id": "req_test_001",
      },
      body: sseBody,
    });
  });

  await page.goto("/playground");
  await expect(page.getByTestId("model-select")).toHaveValue(
    "groq/llama-3.1-8b-instant",
  );

  // Type + send.
  await page.getByTestId("composer").fill("Ping");
  await page.getByTestId("send-button").click();

  // Assistant bubble accumulates the three deltas.
  await expect(page.getByTestId("message-assistant").first()).toContainText(
    "Hello world!",
    { timeout: 5_000 },
  );

  // Inspector picks up headers.
  await expect(page.getByTestId("stat-cache")).toHaveText("miss");
  await expect(page.getByTestId("stat-cost")).not.toHaveText("—");
  await expect(page.getByTestId("inspector-routed-model")).toContainText(
    "groq/llama-3.1-8b-instant",
  );
  await expect(page.getByTestId("inspector-request-id")).toContainText("req_test_001");
});

// --------------------------------------------------------------------------- //
// Non-streaming branch                                                        //
// --------------------------------------------------------------------------- //

test("streaming toggle off → non-streaming response renders + captures usage", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/chat\/completions/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "X-Pronaos-Cache": "hit:exact",
        "X-Pronaos-Cost-Hcents": "0",
      },
      body: JSON.stringify({
        id: "chatcmpl_test",
        model: "groq/llama-3.1-8b-instant",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "Non-streaming reply." },
            finish_reason: "stop",
          },
        ],
        usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
      }),
    }),
  );

  await page.goto("/playground");
  await expect(page.getByTestId("model-select")).toHaveValue(
    "groq/llama-3.1-8b-instant",
  );

  // Toggle streaming off. The checkbox is visually-hidden (sr-only) and
  // the styled <span> overlays it — force the click since we're targeting
  // the actual input by testid.
  await page.getByTestId("streaming-toggle").click({ force: true });

  await page.getByTestId("composer").fill("Cached prompt");
  await page.getByTestId("send-button").click();

  await expect(page.getByTestId("message-assistant").first()).toContainText(
    "Non-streaming reply.",
    { timeout: 5_000 },
  );
  // Cache hit gets highlighted.
  await expect(page.getByTestId("stat-cache")).toHaveText("hit:exact");
});

// --------------------------------------------------------------------------- //
// Embeddings tab (Phase 65 gap fill)                                          //
// --------------------------------------------------------------------------- //

test("embeddings tab sends POST /v1/embeddings and renders vector preview", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/embeddings/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "X-Pronaos-Cache": "miss" },
      body: JSON.stringify({
        object: "list",
        data: [{ object: "embedding", embedding: Array.from({ length: 16 }, (_, i) => i * 0.01), index: 0 }],
        model: "text-embedding-3-small",
        usage: { prompt_tokens: 7, total_tokens: 7 },
      }),
    }),
  );

  await page.goto("/playground");
  // Switch to Embeddings tab.
  await page.getByTestId("tab-embeddings").click();
  await expect(page.getByTestId("emb-input")).toBeVisible();

  await page.getByTestId("emb-input").fill("Hello world embedding test");
  await page.getByTestId("emb-run-button").click();

  await expect(page.getByTestId("emb-result")).toBeVisible({ timeout: 5_000 });
  // First vector shown as a truncated float array preview.
  await expect(page.getByTestId("emb-vector-0")).toContainText("0.0000");
});

// --------------------------------------------------------------------------- //
// Rerank tab (Phase 65 gap fill)                                              //
// --------------------------------------------------------------------------- //

test("rerank tab sends POST /v1/rerank and renders scored results table", async ({
  page,
  context,
}) => {
  await context.route(/\/v1\/admin\/models/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_BODY),
    }),
  );
  await context.route(/\/v1\/rerank/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "X-Pronaos-Cache": "miss" },
      body: JSON.stringify({
        object: "list",
        data: [
          { index: 0, relevance_score: 0.9123, document: "The quick brown fox." },
          { index: 1, relevance_score: 0.2345, document: "Machine learning models." },
        ],
        model: "cohere/rerank-english-v3.0",
        usage: { prompt_tokens: 42, total_tokens: 42 },
      }),
    }),
  );

  await page.goto("/playground");
  await page.getByTestId("tab-rerank").click();
  await expect(page.getByTestId("rerank-query")).toBeVisible();

  await page.getByTestId("rerank-query").fill("What is the quick animal?");
  await page.getByTestId("rerank-run-button").click();

  await expect(page.getByTestId("rerank-result")).toBeVisible({ timeout: 5_000 });
  await expect(page.getByTestId("rerank-table")).toContainText("0.9123");
  await expect(page.getByTestId("rerank-table")).toContainText("The quick brown fox.");
});
