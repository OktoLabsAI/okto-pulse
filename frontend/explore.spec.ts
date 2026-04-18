import { test, expect } from '@playwright/test';

test('Explore page structure', async ({ page }) => {
  await page.goto('http://127.0.0.1:8999');
  await page.waitForLoadState('networkidle');

  // Take screenshot
  await page.screenshot({ path: 'homepage.png' });

  // List all links
  const links = await page.getByRole('link').all();
  console.log('Links found:', links.length);
  for (const link of links) {
    const text = await link.textContent();
    console.log(' -', text);
  }

  // List all buttons
  const buttons = await page.getByRole('button').all();
  console.log('Buttons found:', buttons.length);
  for (const button of buttons) {
    const text = await button.textContent();
    console.log(' -', text?.slice(0, 50));
  }
});
