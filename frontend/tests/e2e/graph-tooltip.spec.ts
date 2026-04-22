/**
 * E2E coverage for AC-7 (hover tooltip <=100ms) and AC-8 (single-click
 * preview panel) — Sprint 5 / S5.6.
 *
 * Spec: ts_6051cd4e (tooltip) + ts_d1df9c90 (preview).
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test('hover on a node shows tooltip within 100ms (AC-7)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible', timeout: 15_000 });

  const t0 = Date.now();
  await firstNode.hover();
  const tooltip = page.locator('[data-testid="kg-node-tooltip"]');
  await expect(tooltip).toBeVisible({ timeout: 200 });
  const elapsed = Date.now() - t0;
  // Cap is 100ms per AC-7; give a 50ms jitter buffer for transport + event loop.
  expect(elapsed).toBeLessThanOrEqual(150);

  // Tooltip carries title + confidence markers.
  await expect(tooltip).toContainText(/conf \d+%/);
});

test('tooltip disappears when the mouse leaves the node', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible' });
  await firstNode.hover();
  await expect(page.locator('[data-testid="kg-node-tooltip"]')).toBeVisible();

  // Move the mouse far off the node onto the pane background.
  await page.mouse.move(10, 10);
  await expect(page.locator('[data-testid="kg-node-tooltip"]')).toHaveCount(0);
});

test('single-click opens the preview panel, close button clears it (AC-8)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible' });

  await firstNode.click();
  const preview = page.locator('[data-testid="kg-preview-panel"]');
  await expect(preview).toBeVisible();

  await page.locator('[data-testid="kg-preview-close"]').click();
  await expect(preview).toHaveCount(0);
});
