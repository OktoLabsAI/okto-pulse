import { writeFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';

// Started by test_native_verification_context with native SQL, signed evidence,
// real REST handlers and the same population already compared through MCP.
// No fabricated API responses: route.fetch forwards to the disposable backend.
test('native partial proof stays blocked in the read-only Spec rollup', async ({ page }) => {
  const backend = process.env.PULSE_NATIVE_DELIVERY_BACKEND;
  test.skip(!backend, 'Run through the native backend integration test');
  const api = new URL(backend!);
  expect(api.hostname).toBe('127.0.0.1');
  let reads = 0;
  const writes: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    if (request.method() !== 'GET') writes.push(request.method() + ' ' + request.url());
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, '');
    if (path.endsWith('/delivery-evidence')) reads += 1;
    const response = await route.fetch({ url: api.origin + path + new URL(request.url()).search });
    await route.fulfill({ response });
  });
  await page.goto('/tests/fixtures/delivery-evidence.html');
  await expect(page.getByText('Blocked', { exact: true })).toBeVisible();
  const table = page.getByTestId('delivery-coverage-table');
  await expect(table.getByText('✓', { exact: true })).toHaveCount(6);
  await expect(table.getByText('(fr)', { exact: true })).toBeVisible();
  await expect(table.getByText('(br)', { exact: true })).toBeVisible();
  await expect(table.getByText('(ac)', { exact: true })).toBeVisible();
  await expect(table.getByText('◌', { exact: true })).toHaveCount(8);
  await expect(table.getByRole('row')).toHaveCount(8);
  await expect(page.getByRole('status')).toContainText('Edition 2');
  await expect(page.getByTestId('delivery-gate-mode')).toHaveText('Gate: Unknown (Board)');
  await expect(page.getByRole('button', { name: 'Record association' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Refresh delivery rollup' }).click();
  await expect(page.getByText('Blocked', { exact: true })).toBeVisible();
  await expect(table.getByText('✓', { exact: true })).toHaveCount(6);
  await expect.poll(() => reads).toBe(2);
  expect(writes).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('native-delivery.png'), fullPage: true });
});


test('records authenticated proof on its Card and preserves partial Spec coverage', async ({ page }) => {
  const backend = process.env.PULSE_NATIVE_DELIVERY_BACKEND;
  test.skip(!backend, 'Run through the native backend integration test');
  const api = new URL(backend!);
  expect(api.hostname).toBe('127.0.0.1');
  const writes: { path: string; payload: unknown; status: number; response: unknown }[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, '');
    const response = await route.fetch({ url: api.origin + path + url.search });
    if (request.method() !== 'GET') {
      writes.push({ path, payload: request.postDataJSON(), status: response.status(), response: await response.json() });
    }
    await route.fulfill({ response });
  });
  await page.goto('/tests/fixtures/delivery-evidence.html?view=test-card');
  await page.getByTestId('dod-record-button').click();
  for (const ref of ['fr:fr', 'br:br', 'ac:ac']) await page.getByLabel('Select ' + ref, { exact: true }).check();
  await page.getByRole('combobox', { name: 'Authenticated run on this test card' }).selectOption('test-card:ts');
  await page.getByRole('checkbox', { name: /src\/file.py/ }).check();
  await page.getByLabel('Explanation / audit reason').fill('Browser association of the authenticated blocking observation.');
  await page.getByRole('button', { name: 'Record delivery evidence', exact: true }).click();
  await expect(page.getByTestId('dod-record-form')).toHaveCount(0);
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe('/boards/board/cards/test-card/specs/spec/delivery-evidence');
  expect(writes[0].status).toBe(200);
  expect(writes[0].payload).toMatchObject({
    kind: 'test', expected_spec_edition: 2, scenario_id: 'ts',
    obligation_refs: ['fr:fr', 'br:br', 'ac:ac'],
  });
  expect(writes[0].payload).not.toHaveProperty('status');
  expect(writes[0].response).toMatchObject({ replayed: false });
  writeFileSync(process.env.PULSE_NATIVE_BROWSER_RESULT!, JSON.stringify(writes[0]));
  await page.goto('/tests/fixtures/delivery-evidence.html');
  await expect(page.getByText('Blocked', { exact: true })).toBeVisible();
  await expect(page.getByTestId('delivery-coverage-table').getByText('✓', { exact: true })).toHaveCount(6);
  await expect(page.getByTestId('delivery-coverage-table').getByText('◌', { exact: true })).toHaveCount(8);
  await page.screenshot({ path: test.info().outputPath('native-card-association.png'), fullPage: true });
});
