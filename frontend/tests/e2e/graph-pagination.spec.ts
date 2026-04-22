/**
 * E2E coverage for AC-9 (node-limit dropdown triggers refetch) and AC-17
 * (Load More appends + hides when next_cursor is null) — Sprint 4 / S4.9b.
 *
 * Spec: ts_4b522cb4 + ts_be395923.
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test('changing node-limit dropdown refetches and updates node count (AC-9)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  await page.locator('.react-flow__node').first().waitFor({ state: 'visible' });

  const initialCount = await page.locator('.react-flow__node').count();

  await page.locator('[data-testid="kg-node-limit"]').selectOption('50');

  // After the refetch we expect the node count to settle at ≤ 50.
  await expect
    .poll(async () => await page.locator('.react-flow__node').count(), { timeout: 10_000 })
    .toBeLessThanOrEqual(50);

  // And to differ from the pre-change count (unless the board was smaller anyway).
  if (initialCount > 50) {
    expect(await page.locator('.react-flow__node').count()).toBeLessThan(initialCount);
  }
});

test('Load More appends rows and vanishes when next_cursor is null (AC-17)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  await page.locator('.react-flow__node').first().waitFor({ state: 'visible' });
  await page.locator('[data-testid="kg-node-limit"]').selectOption('50');

  const loadMore = page.locator('[data-testid="kg-load-more"]');
  // If the test board has ≤50 nodes the button should already be gone.
  if (!(await loadMore.isVisible())) {
    test.skip(true, 'Board does not have a second page under the 50-node cut-off');
    return;
  }

  const before = await page.locator('.react-flow__node').count();
  await loadMore.click();

  await expect
    .poll(async () => await page.locator('.react-flow__node').count(), { timeout: 10_000 })
    .toBeGreaterThan(before);

  // Clicking until the cursor is exhausted makes the button disappear.
  while (await loadMore.isVisible()) {
    await loadMore.click();
    await page.waitForTimeout(200);
  }
  await expect(loadMore).toHaveCount(0);
});
