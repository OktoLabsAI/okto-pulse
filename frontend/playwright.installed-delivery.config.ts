import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e', testMatch: 'installed-delivery.spec.ts',
  workers: 1, retries: 0, reporter: 'list', timeout: 45000,
  // Use the same Playwright-managed Chromium as the main E2E project.
  // System Chrome 155.0.8059.79 exposes empty CDP AX valuetext even for a
  // static, correct ARIA progressbar; see the P11 implementation report.
  use: { baseURL: process.env.E2E_BASE_URL,
    viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
});
