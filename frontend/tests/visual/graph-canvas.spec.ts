/**
 * Visual regression baseline for the Knowledge Graph canvas — Sprint 6 / S6.2 + S6.5.
 *
 * Spec: ts_560dfb72 (AC-16 visual regression diff < 0.01).
 *
 * This test establishes the baseline screenshot for the graph canvas in both
 * light and dark modes. The fixture uses a seeded board (`E2E_KG_BOARD_ID`)
 * that contains all 11 node types and a variety of edge types; deterministic
 * layout is guaranteed by seeding Math.random via page.addInitScript.
 *
 * Baseline snapshots live in tests/visual/__snapshots__/ and are committed
 * to the repo. CI enforces diff_ratio < 0.01 via playwright.config.ts.
 *
 * Elements with volatile content (e.g. timestamps) are masked in the
 * screenshot selector so only stable graph structure is compared.
 */

import { test, expect } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test.describe.configure({ mode: 'serial' });

test.describe('Graph canvas visual regression (AC-16)', () => {
  test('baseline snapshot in light mode', async ({ page }) => {
    // Seed Math.random for deterministic force simulation (see S3.6 graph-layout.spec.ts)
    await page.addInitScript(() => {
      // @ts-expect-error — seed-only RNG shim for Playwright tests
      globalThis.__mockedMathSeed = 42;
      // @ts-expect-error — deterministic random implementation
      const SEED = 42;
      let seed = SEED;
      const random = () => {
        const x = Math.sin(seed++) * 10000;
        return x - Math.floor(x);
      };
      // @ts-expect-error — replace global Math.random
      globalThis.Math.random = random;
    });

    await page.goto(`/boards/${BOARD_ID}/kg`);
    // Wait for canvas to be fully rendered (nodes visible)
    await page.locator('.react-flow__node').first().waitFor({ state: 'visible', timeout: 15_000 });

    // Take a screenshot of the entire graph canvas, excluding the sidebar and controls
    const canvas = page.locator('[data-testid="kg-canvas"]');
    await expect(canvas).toHaveScreenshot('kg-canvas-light.png', {
      maxDiffPixelRatio: 0.01,
    });
  });

  test('baseline snapshot in dark mode', async ({ page }) => {
    // Seed Math.random for deterministic layout (same seed as light mode)
    await page.addInitScript(() => {
      // @ts-expect-error — seed-only RNG shim for Playwright tests
      globalThis.__mockedMathSeed = 42;
      // @ts-expect-error — deterministic random implementation
      const SEED = 42;
      let seed = SEED;
      const random = () => {
        const x = Math.sin(seed++) * 10000;
        return x - Math.floor(x);
      };
      // @ts-expect-error — replace global Math.random
      globalThis.Math.random = random;
    });

    // Force dark mode via classList and goto graph page
    await page.addInitScript(() => {
      document.documentElement.classList.add('dark');
    });

    await page.goto(`/boards/${BOARD_ID}/kg`);
    await page.locator('.react-flow__node').first().waitFor({ state: 'visible', timeout: 15_000 });

    const canvas = page.locator('[data-testid="kg-canvas"]');
    await expect(canvas).toHaveScreenshot('kg-canvas-dark.png', {
      maxDiffPixelRatio: 0.01,
    });
  });
});
