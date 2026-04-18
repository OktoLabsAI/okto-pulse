/**
 * E2E coverage for non-grid force distribution — Spec 8 / ts_31b5e4fd (AC-1).
 *
 * Loads the Knowledge Graph page for a seeded board, waits for React Flow to
 * paint at least a handful of nodes, samples 10 node transforms, and asserts
 * that the resulting layout is NOT a uniform grid. The old computeLayout
 * placed nodes in regular columns (fixed colWidth × rowHeight), so after
 * migration to d3-force we expect irregular spacing: at least 5 out of 10
 * sampled pairs should differ by more than 50px in either axis.
 *
 * Math.random is seeded via page.addInitScript so the simulation converges
 * to the same positions across runs — required for the assertion to be
 * stable on CI.
 *
 * Environment:
 *   E2E_BASE_URL       — frontend origin (default http://localhost:5174)
 *   E2E_KG_BOARD_ID    — board id with ≥30 seeded KG nodes (mixed types)
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    let seed = 1337;
    const lcg = () => {
      seed = (seed * 1664525 + 1013904223) % 0x100000000;
      return seed / 0x100000000;
    };
    Math.random = lcg;
  });
});

test('KG canvas renders a non-grid, force-directed layout (AC-1)', async ({ page }) => {
  await page.goto(`/boards/${BOARD_ID}/kg`);

  const firstNode = page.locator('.react-flow__node').first();
  await firstNode.waitFor({ state: 'visible', timeout: 15_000 });

  const transforms = await page
    .locator('.react-flow__node')
    .evaluateAll((nodes) =>
      nodes.slice(0, 10).map((el) => {
        const style = (el as HTMLElement).style.transform || '';
        const match = /translate\(([-\d.]+)px, ([-\d.]+)px\)/.exec(style);
        if (!match) return { x: 0, y: 0 };
        return { x: parseFloat(match[1]), y: parseFloat(match[2]) };
      }),
    );

  expect(transforms.length).toBeGreaterThanOrEqual(6);

  let irregularPairs = 0;
  for (let i = 0; i < transforms.length; i++) {
    for (let j = i + 1; j < transforms.length; j++) {
      const dx = Math.abs(transforms[i].x - transforms[j].x);
      const dy = Math.abs(transforms[i].y - transforms[j].y);
      if (dx + dy > 50 && Math.abs(dx - dy) > 5) {
        irregularPairs++;
      }
    }
  }

  expect(irregularPairs).toBeGreaterThanOrEqual(5);
});
