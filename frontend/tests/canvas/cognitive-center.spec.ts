import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Every API call is synthetic; the fixture server has no production proxy.
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const base = {
      artifact_id: "spec:design",
      source_ref_original: "spec:design",
      aliases: [],
      artifact_type: "spec",
      signal: "cognitive_pending",
      signal_source: "cognitive_item",
      status: "pending",
      reason_code: null,
      outcome_type: null,
      error_cause: null,
      revisit_at: null,
      readiness_effect: "blocking_cognitive",
      blocking: true,
      precedence_explanation: { tier: "cognitive_active" },
      would_block_done: false,
    };
    const rows = [
      base,
      {
        ...base,
        artifact_id: "ideation:failure",
        source_ref_original: "ideation:failure",
        artifact_type: "ideation",
        signal: "dlq",
        signal_source: "dlq",
        status: "dead_lettered",
        readiness_effect: "blocking_technical",
        error_cause: "technical_dlq",
      },
    ];
    if (url.pathname.endsWith("/me/permissions"))
      return route.fulfill({
        json: {
          board_id: "fixture",
          flags: {
            kg: {
              operations: {
                cognitive: { read: true, skip: true, clear: true },
                health: { read: true },
                queue: { read: true, reprocess: true },
              },
            },
          },
          owner_review_required: false,
        },
      });
    if (url.pathname.endsWith("/cognitive-readiness/items")) {
      const history = url.searchParams.get("signal") === "terminal_history";
      const offset = Number(url.searchParams.get("offset"));
      const items = history
        ? [
            {
              ...base,
              status: "consolidated",
              signal: "terminal_history",
              readiness_effect: "ready_committed",
            },
          ]
        : offset
          ? [
              {
                ...base,
                artifact_id: "spec:next",
                source_ref_original: "spec:next",
              },
            ]
          : rows;
      return route.fulfill({
        json: {
          items,
          summary: {
            total: history ? 1 : 26,
            limit: 25,
            offset,
            enforcement_active: false,
            by_signal: {},
            technical_blocking_signals: 1,
            cognitive_pending_signals: 1,
          },
          precedence: [],
        },
      });
    }
    if (url.pathname.endsWith("/cognitive-readiness/metrics"))
      return route.fulfill({
        json: {
          total: 26,
          cognitive_pending_signals: 1,
          technical_dlq: 1,
          expired_revisit_skips: 0,
          by_signal: { cognitive_pending: 1, dlq: 1 },
          by_status: {},
          by_readiness_effect: {},
          by_reason_code: {},
          by_age_bucket: {},
        },
      });
    if (url.pathname.startsWith("/api/v1/specs/"))
      return route.fulfill({
        json: {
          id: "design",
          board_id: "fixture",
          title: "Define the application memory contract",
        },
      });
    if (url.pathname.startsWith("/api/v1/ideations/"))
      return route.fulfill({
        json: {
          id: "failure",
          board_id: "fixture",
          title: "Capture reusable deployment decisions",
        },
      });
    if (url.pathname.endsWith("/cognitive-readiness/skip"))
      return route.fulfill({ json: { status: "skipped" } });
    return route.fulfill({
      status: 404,
      json: { detail: "Synthetic endpoint not provided" },
    });
  });
  await page.goto("/tests/fixtures/cognitive-center.html");
});

test("explains next steps, opens artifacts, paginates and removes historical actions", async ({
  page,
}) => {
  await expect(
    page.getByRole("heading", { name: "Resolve knowledge gaps" }),
  ).toBeVisible();
  await expect(
    page.getByText("Define the application memory contract"),
  ).toBeVisible();
  await expect(
    page.getByText("This page does not execute consolidation.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.screenshot({ path: "test-results/cognitive-center-desktop.png" });
  await page
    .getByRole("button", { name: "Open source", exact: true })
    .first()
    .click();
  await expect(page.getByText("Opened: spec:design")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("spec:next", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "History", exact: true }).click();
  await expect(
    page.getByText("History — no action needed", { exact: false }),
  ).toBeVisible();
  await expect(page.getByTestId("cac-skip-toggle")).toHaveCount(0);
});

test("waiver requires an explicit decision and portal help never shifts the layout", async ({
  page,
}) => {
  const center = page.getByTestId("cognitive-action-center");
  await expect(page.getByTestId("cac-skip-toggle")).toBeVisible();
  const before = await page.locator("article").first().boundingBox();
  const help = page.getByRole("button", { name: "Help: Completion policy" });
  await help.hover();
  await expect(page.getByRole("tooltip")).toBeVisible();
  expect(await page.locator("article").first().boundingBox()).toEqual(before);
  expect(
    await page
      .getByRole("tooltip")
      .evaluate((el) => el.parentElement === document.body),
  ).toBe(true);
  await help.focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await page.getByTestId("cac-skip-toggle").click();
  await expect(page.getByTestId("cac-skip-reason")).toHaveValue("");
  await expect(
    page.getByText("This records a waiver only.", { exact: false }),
  ).toBeVisible();
  await page
    .getByTestId("cac-skip-reason")
    .selectOption("evidence_insufficient");
  await expect(page.getByTestId("cac-skip-revisit")).toHaveAttribute(
    "type",
    "datetime-local",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await center.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
    true,
  );
  await page.getByTestId("cac-skip-form").scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/cognitive-center-mobile.png" });
});
