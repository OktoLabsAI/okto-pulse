import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:5174',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // Sprint 6 / S6.1 — Visual regression project for baseline snapshots.
    // Runs deterministically with seeded Math.random (seed=42) and enforces
    // maxDiffPixelRatio: 0.01. Viewport fixed to 1920x1080 for stable baselines.
    {
      name: 'visual-regression',
      testDir: './tests/visual',
      testMatch: '**/*.spec.ts',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1920, height: 1080 },
      },
      // Enforce AC-16: visual regression diff < 1%
      expect: {
        // 1% pixel difference threshold (ignores anti-aliasing jitter)
        maxDiffPixels: undefined,
        maxDiffPixelRatio: 0.01,
      },
    },
  ],
});
