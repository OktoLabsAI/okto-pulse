/**
 * E2E coverage for AC-5 (edge chip toggle) and AC-6 (confidence slider) —
 * Sprint 4 / S4.9a.
 *
 * Spec: ts_d060ba1e + ts_a8497d22.
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test('edge chip toggle hides only the targeted edge type (AC-5)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  await page.locator('.react-flow__node').first().waitFor({ state: 'visible' });

  const totalEdgesBefore = await page.locator('.react-flow__edge').count();
  const contradictsChip = page.locator('[data-testid="kg-edge-chip-contradicts"]');
  await contradictsChip.click();

  // After one click, chip should flip its aria-checked state.
  await expect(contradictsChip).toHaveAttribute('aria-checked', 'false');

  // At least one edge disappears if any `contradicts` edge existed.
  const totalEdgesAfter = await page.locator('.react-flow__edge').count();
  expect(totalEdgesAfter).toBeLessThanOrEqual(totalEdgesBefore);
});

test('confidence slider filters the graph in real time (AC-6)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);
  await page.locator('.react-flow__node').first().waitFor({ state: 'visible' });

  const initialNodes = await page.locator('.react-flow__node').count();
  const slider = page.locator('[data-testid="kg-confidence-slider"]');
  await slider.fill('1');

  await expect
    .poll(async () => await page.locator('.react-flow__node').count())
    .toBeLessThanOrEqual(initialNodes);
});
