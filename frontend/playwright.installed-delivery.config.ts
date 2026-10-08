import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e', testMatch: 'installed-delivery.spec.ts',
  workers: 1, retries: 0, reporter: 'list', timeout: 45000,
  use: { baseURL: process.env.E2E_BASE_URL, channel: 'chrome',
    viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
});
