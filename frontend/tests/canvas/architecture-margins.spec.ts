import { test, expect, type Page } from '@playwright/test';

const fixture = '/tests/fixtures/architecture-canvas.html';
const stored = async (page: Page) => JSON.parse(await page.getByTestId('stored-diagram').textContent() || '{}');
async function assertMargins(page: Page) {
  const gaps = await page.getByTestId('architecture-canvas-surface').evaluate(surface => {
    const s = surface.getBoundingClientRect();
    return [...surface.querySelectorAll<HTMLElement>('[data-testid^="architecture-element-"]')].map(node => {
      const b = node.getBoundingClientRect();
      return [b.left - s.left, b.top - s.top, s.right - b.right, s.bottom - b.bottom];
    });
  });
  for (const gap of gaps.flat()) expect(gap).toBeGreaterThan(100);
}

test.beforeEach(async ({ page }) => { await page.goto(fixture); });

test('negative nodes and connectors are visible after fit, without changing stored coordinates', async ({ page }) => {
  const original = await stored(page);
  await page.getByTitle('Fit to view', { exact: true }).click();
  await expect.poll(async () => {
    const viewport = await page.getByTestId('architecture-canvas').boundingBox();
    const boxes = await page.locator('[data-testid^="architecture-element-"]').all();
    for (const node of boxes) {
      const box = (await node.boundingBox())!;
      if (box.x < viewport!.x + 20 || box.y < viewport!.y + 20 || box.x + box.width > viewport!.x + viewport!.width - 20 || box.y + box.height > viewport!.y + viewport!.height - 20) return false;
    }
    return true;
  }).toBe(true);
  await assertMargins(page);
  expect(await stored(page)).toEqual(original);
});

test('dragging left/top grows the canvas without jumping the other nodes or accumulating drift', async ({ page }) => {
  // Bring the whole diagram into the viewport at 100% without changing zoom.
  await page.getByTestId('architecture-canvas').evaluate(el => { el.scrollLeft = 250; el.scrollTop = 180; });
  const node = page.getByTestId('architecture-element-adapter');
  const other = page.getByTestId('architecture-element-global');
  const before = (await node.boundingBox())!;
  const otherBefore = (await other.boundingBox())!;
  const x = before.x + before.width / 2;
  const y = before.y + before.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 48, y - 96, { steps: 4 });
  await page.mouse.move(x - 96, y - 144, { steps: 4 });
  await page.mouse.up();
  const adapter = (await stored(page)).adapter_payload.elements.find((e: {id: string}) => e.id === 'adapter');
  expect(adapter.x).toBe(-456);
  expect(adapter.y).toBe(-384);
  const after = (await other.boundingBox())!;
  expect(Math.abs(after.x - otherBefore.x)).toBeLessThan(2);
  expect(Math.abs(after.y - otherBefore.y)).toBeLessThan(2);
  await assertMargins(page);
});

test('zoom, focus, fullscreen and read-only keep negative nodes reachable without writes', async ({ page }) => {
  const original = await stored(page);
  for (let i = 0; i < 5; i++) await page.getByTitle('Zoom out', { exact: true }).click();
  await assertMargins(page);
  await page.getByTitle('Reset zoom', { exact: true }).click();
  await page.getByRole('button', { name: 'Toggle read only' }).click();
  await page.getByRole('button', { name: 'Focus left node' }).click();
  await expect(page.getByTestId('architecture-element-local')).toBeInViewport({ ratio: 1 });
  await page.getByTitle('Fullscreen diagram', { exact: true }).click();
  await page.getByTitle('Fit to view', { exact: true }).click();
  await assertMargins(page);
  expect(await stored(page)).toEqual(original);
});
