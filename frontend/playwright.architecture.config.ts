import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/canvas', workers: 1, reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:5189', channel: 'chrome', viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
  webServer: {
    command: 'npm exec vite -- --config vite.architecture-test.config.ts',
    url: 'http://127.0.0.1:5189/tests/fixtures/architecture-canvas.html',
    reuseExistingServer: !process.env.CI,
  },
});
