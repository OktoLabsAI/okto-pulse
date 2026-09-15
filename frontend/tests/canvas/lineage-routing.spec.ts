import { test, expect } from '@playwright/test';

test('React Flow uses the explicit right source, and every displayed status has a distinct node/minimap color', async ({ page }) => {
  await page.goto('/tests/fixtures/lineage-canvas.html');
  const root = page.locator('.react-flow__node[data-id="root"]');
  await expect(root).toBeVisible();
  await expect(page.locator('.react-flow__edge-path')).toHaveCount(4);
  await expect.poll(async () => {
    const source = await root.locator('[data-handleid="lineage-source-right"]').boundingBox();
    const target = await page.locator('.react-flow__node[data-id="draft"] [data-handleid="lineage-target-left"]').boundingBox();
    return page.locator('.react-flow__edge[data-id="edge-draft"] .react-flow__edge-path').evaluate((el, boxes) => {
      const path = el as SVGPathElement;
      const matrix = path.getScreenCTM()!;
      const start = new DOMPoint(path.getPointAtLength(0).x, path.getPointAtLength(0).y).matrixTransform(matrix);
      const endPoint = path.getPointAtLength(path.getTotalLength());
      const end = new DOMPoint(endPoint.x, endPoint.y).matrixTransform(matrix);
      return Math.abs(start.x - (boxes.source!.x + boxes.source!.width / 2)) < 8
        && Math.abs(end.x - (boxes.target!.x + boxes.target!.width / 2)) < 8;
    }, { source, target });
  }).toBe(true);
  const colors = await page.locator('[data-lineage-status]').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).borderLeftColor));
  expect(new Set(colors).size).toBe(5);
  await expect(page.locator('.react-flow__minimap-node')).toHaveCount(5);
  const mini = await page.locator('.react-flow__minimap-node').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).fill));
  expect(new Set(mini)).toEqual(new Set(colors));
  for (const status of ['draft', 'review', 'approved', 'cancelled', 'done']) {
    await expect(page.locator(`[data-lineage-status="${status}"]`)).toContainText(status);
  }
});
