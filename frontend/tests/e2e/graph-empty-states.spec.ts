/**
 * E2E coverage for AC-19 (distinct empty states) and AC-20 (error retry) —
 * Sprint 5 / S5.8.
 *
 * Spec: ts_0264f81a (empty states) + ts_3225925f (error retry).
 *
 * The "no nodes yet" branch requires an empty-board fixture; if the
 * default seeded board has nodes, the corresponding test is skipped.
 * The "no nodes match filters" branch is reproducible on any non-empty
 * board by cranking the confidence slider to 1 (and asserting that the
 * canvas switches to data-empty-state="filtered").
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';
const EMPTY_BOARD_ID = process.env.E2E_KG_EMPTY_BOARD_ID;

test('filtered-empty state shows when every node is filtered out (AC-19)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  await page.locator('.react-flow__node').first().waitFor({ state: 'visible' });

  // Max out the confidence slider — should hide every node (< 1.0 all go).
  await page.locator('[data-testid="kg-confidence-slider"]').fill('1');

  const empty = page.locator('[data-testid="kg-canvas-empty"]');
  await expect(empty).toBeVisible();
  await expect(empty).toHaveAttribute('data-empty-state', 'filtered');

  // Clearing filters should restore the graph.
  await page.locator('[data-testid="kg-clear-filters"]').click();
  await expect(page.locator('.react-flow__node').first()).toBeVisible();
});

test('empty-yet state renders when the board has no nodes (AC-19)', async ({ page }) => {
  test.skip(!EMPTY_BOARD_ID, 'Set E2E_KG_EMPTY_BOARD_ID to a board with zero consolidated nodes');
  await page.goto(`/boards/${EMPTY_BOARD_ID}/kg`);
  const empty = page.locator('[data-testid="kg-empty-yet"]');
  await expect(empty).toBeVisible();
  await expect(empty).toHaveAttribute('data-empty-state', 'yet');
});

test('error state exposes Retry and re-runs the fetch (AC-20)', async ({ page }) => {
  // Force the initial fetch to fail so we land on the error state.
  await page.route('**/api/v1/kg/**/subgraph*', (route, request) => {
    if (!request.url().includes('retry=ok')) {
      return route.fulfill({ status: 500, body: JSON.stringify({ detail: 'boom' }) });
    }
    return route.continue();
  });

  await page.goto(`/boards/${BOARD_ID}/kg`);
  await expect(page.locator('[data-testid="kg-error"]')).toBeVisible();
  await expect(page.locator('[data-testid="kg-error-retry"]')).toBeVisible();

  // Unblock the next fetch and click retry.
  await page.unroute('**/api/v1/kg/**/subgraph*');
  await page.locator('[data-testid="kg-error-retry"]').click();

  // Either the canvas populates (success) or the error clears; assert the
  // error state is no longer shown to confirm the retry path was taken.
  await expect(page.locator('[data-testid="kg-error"]')).toHaveCount(0, { timeout: 10_000 });
});
