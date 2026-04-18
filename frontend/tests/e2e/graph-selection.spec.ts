/**
 * E2E coverage for AC-4 (click selection + neighbour highlight) — Sprint 4 / S4.8.
 *
 * Steps:
 *   1. Navigate to a seeded KG board.
 *   2. Click the first node. Assert data-selected-id on the canvas matches.
 *   3. Assert at least one other node has data-faded="true" (fade on others).
 *   4. Click the pane. Assert selection clears and faded state is removed.
 *   5. Double-click a node. Assert the detail panel opens (aside element).
 *
 * ts_c6af8b05.
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test('click selection highlights neighbours and paneClick clears it (AC-4)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible', timeout: 15_000 });

  await firstNode.click();

  const canvas = page.locator('[data-testid="kg-canvas"]');
  await expect(canvas).not.toHaveAttribute('data-selected-id', '');

  await expect.poll(async () => {
    return await page.locator('[data-faded="true"]').count();
  }).toBeGreaterThan(0);

  await page.locator('.react-flow__pane').click({ position: { x: 10, y: 10 } });
  await expect(canvas).toHaveAttribute('data-selected-id', '');
  await expect(page.locator('[data-faded="true"]')).toHaveCount(0);
});

test('double-click opens the detail panel', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible', timeout: 15_000 });

  await firstNode.dblclick();

  const detailPanel = page.locator('aside, [aria-label*="detail" i], [data-testid="kg-node-detail"]').first();
  await expect(detailPanel).toBeVisible({ timeout: 5_000 });
});
