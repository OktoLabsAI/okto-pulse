import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: 'readiness-persisted.spec.ts',
  workers: 1,
  reporter: 'list',
  outputDir: './output/playwright/readiness-persisted',
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5190', screenshot: 'only-on-failure' },
  webServer: {
    command: 'npm exec vite -- --host 127.0.0.1 --port 5190 --strictPort',
    url: 'http://127.0.0.1:5190/tests/fixtures/readiness-persisted.html',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
