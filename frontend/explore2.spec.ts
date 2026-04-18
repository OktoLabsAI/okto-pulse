import { test, expect } from '@playwright/test';

test('Explore KG page', async ({ page }) => {
  await page.goto('http://127.0.0.1:8999');
  await page.waitForLoadState('networkidle');

  // Look for text containing "Knowledge"
  const knowledgeElements = await page.getByText(/knowledge/i).all();
  console.log('Knowledge elements found:', knowledgeElements.length);
  for (const elem of knowledgeElements) {
    const text = await elem.textContent();
    console.log(' -', text?.slice(0, 100));
  }

  // Try to navigate to a board first, then look for KG
  const boardButtons = await page.getByRole('button', { name: /my board/i }).all();
  if (boardButtons.length > 0) {
    console.log('Clicking My Board button');
    await boardButtons[0].click();
    await page.waitForLoadState('networkidle');

    // Now look for KG
    await page.screenshot({ path: 'board-page.png' });

    const kgButtons = await page.getByText(/kg|knowledge/i).all();
    console.log('KG elements on board page:', kgButtons.length);
    for (const elem of kgButtons) {
      const text = await elem.textContent();
      console.log(' -', text?.slice(0, 100));
    }
  }
});
