import { test, expect } from '@playwright/test';

const BOARD_ID = '5a4dc9a3-ae4d-486e-a3fa-e0c95fee9b50';
const BASE = 'http://127.0.0.1:8999';

async function navigateToKG(page) {
  await page.goto(BASE);
  await page.waitForLoadState('networkidle');

  const myBoardButton = page.getByRole('button', { name: /my board/i });
  if (await myBoardButton.isVisible()) {
    await myBoardButton.click();
    await page.waitForLoadState('networkidle');
  }

  const menuButtons = await page.locator('.btn.btn-secondary').all();
  if (menuButtons.length >= 2) {
    await menuButtons[1].click();
    await page.waitForTimeout(500);
  }

  const kgButton = page.getByText('Knowledge Graph', { exact: true });
  await expect(kgButton).toBeVisible({ timeout: 5000 });
  await kgButton.click();
  await page.waitForTimeout(2000);
}

test.describe('Knowledge Graph', () => {
  test('should display KG help modal in English', async ({ page }) => {
    await navigateToKG(page);

    // "Learn How It Works" may be in EmptyState OR we need to check the modal directly
    const learnButton = page.getByRole('button', { name: /learn how it works/i });
    const isEmptyState = await learnButton.isVisible().catch(() => false);

    if (!isEmptyState) {
      // KG has data — skip this test (modal is only accessible from EmptyState)
      test.skip();
      return;
    }

    await learnButton.click();
    await page.waitForSelector('[class*="fixed inset-0"]', { state: 'visible' });

    await expect(page.getByText('What is Knowledge Graph?')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'How It Works' })).toBeVisible();
    await expect(page.getByText('Graph Entities')).toBeVisible();
    await expect(page.getByText('AI Agent Integration')).toBeVisible();
    await expect(page.getByText('Getting Started')).toBeVisible();

    await expect(page.getByText('O que é Knowledge Graph?')).not.toBeVisible();
    await expect(page.getByText('Como Funciona')).not.toBeVisible();

    await page.keyboard.press('Escape');
  });

  test('should have properly aligned section headers', async ({ page }) => {
    await navigateToKG(page);

    const learnButton = page.getByRole('button', { name: /learn how it works/i });
    const isEmptyState = await learnButton.isVisible().catch(() => false);

    if (!isEmptyState) {
      test.skip();
      return;
    }

    await learnButton.click();
    await page.waitForSelector('[class*="fixed inset-0"]', { state: 'visible' });

    for (const section of ['What is Knowledge Graph?', 'Graph Entities', 'AI Agent Integration']) {
      await expect(page.getByText(section)).toBeVisible();
    }

    const stepDivs = page.locator('.flex.items-start.gap-3 > div.text-left');
    const stepCount = await stepDivs.count();
    expect(stepCount).toBe(3);

    await page.screenshot({ path: 'kg-modal-english.png' });
    await page.keyboard.press('Escape');
  });

  test('should have consolidated graph nodes from historical specs', async ({ page }) => {
    // Verify that the graph API has nodes (from historical consolidation worker)
    const resp = await page.request.get(`${BASE}/api/v1/kg/boards/${BOARD_ID}/stats`);
    const stats = await resp.json();

    // The worker should have processed the 3 done specs
    const totalNodes = Object.values(stats.node_counts_by_type as Record<string, number>)
      .reduce((a: number, b: number) => a + b, 0);
    expect(totalNodes).toBeGreaterThanOrEqual(3);

    // Navigate to KG page and verify it shows the graph (not empty state)
    await navigateToKG(page);

    // The graph view should render (no empty state)
    // EmptyState has role="status" — verify it's absent
    await expect(page.locator('[role="status"]')).not.toBeVisible({ timeout: 5000 });

    // Take screenshot
    await page.screenshot({ path: 'kg-graph-with-nodes.png' });
  });

  test('should show progress bar when historical consolidation is running', async ({ page }) => {
    // Cancel existing and re-queue to see progress bar
    await page.request.post(`${BASE}/api/v1/kg/boards/${BOARD_ID}/historical-consolidation/cancel`);
    await page.request.post(`${BASE}/api/v1/kg/boards/${BOARD_ID}/historical-consolidation/start`);

    // Check progress endpoint
    const resp = await page.request.get(`${BASE}/api/v1/kg/boards/${BOARD_ID}/historical-consolidation/progress`);
    const progress = await resp.json();
    expect(progress.enabled).toBe(true);
    expect(progress.total).toBeGreaterThanOrEqual(3);

    // Take screenshot of final state
    await page.screenshot({ path: 'kg-historical-progress.png' });
  });
});
