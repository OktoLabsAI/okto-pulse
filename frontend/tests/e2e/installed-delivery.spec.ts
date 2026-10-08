import { test, expect } from '@playwright/test';

test('installed SPA shows accepted native proof without granting missing IR coverage', async ({ page }) => {
  test.skip(process.env.PULSE_INSTALLED_DELIVERY_TEST !== "1", "Run only through the disposable installed-runtime harness");
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  // Suppress optional tours only; no API interception or permission fabrication.
  await page.addInitScript(() => {
    localStorage.setItem('okto.onboarding.completed.v1', 'true');
    localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
    localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({ schemaVersion: 1,
      updatedAt: new Date().toISOString(), skippedAll: true, skippedAllAt: new Date().toISOString(), tours: {} }));
  });
  await page.goto('/?accept_terms=1');
  await page.getByText('Architecture', { exact: true }).first().click();
  await page.getByRole('button', { name: 'Specs', exact: true }).click();
  await page.getByText('Spec', { exact: true }).last().click();
  const modal = page.getByRole('dialog', { name: 'Spec', exact: true });
  await expect(modal).toBeVisible();
  await modal.getByRole('tab', { name: 'Delivery evidence', exact: true }).click();
  await expect(modal.getByText('Blocked', { exact: true })).toBeVisible();
  const table = modal.getByTestId('delivery-coverage-table');
  await expect(table.getByText('✓', { exact: true })).toHaveCount(6);
  await expect(table.getByText('◌', { exact: true })).toHaveCount(8);
  await modal.getByRole('button', { name: 'Close spec' }).click();
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByText('Observe blocking', { exact: true }).first().click();
  await page.getByRole('tab', { name: 'Delivery', exact: true }).click();
  await expect(page.getByText(/Test Cards are excluded from the implementation DoD gate/)).toBeVisible();
  await expect(page.getByTestId('dod-gate-pill')).toHaveCount(0);
  await expect(page.getByTestId('dod-blocked-banner')).toHaveCount(0);
  await page.getByTestId('dod-record-button').click();
  for (const ref of ['fr:fr', 'br:br', 'ac:ac']) await page.getByLabel('Select ' + ref, { exact: true }).check();
  await page.getByRole('combobox', { name: 'Authenticated run on this test card' }).selectOption('test-card:ts');
  await page.getByRole('checkbox', { name: /src\/file.py/ }).check();
  await page.getByLabel('Explanation / audit reason').fill('Installed SPA associates the authenticated blocking observation.');
  const saved = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith('/cards/test-card/specs/spec/delivery-evidence'));
  await page.getByRole('button', { name: 'Record delivery evidence', exact: true }).click();
  const response = await saved;
  expect(response.status()).toBe(200);
  const recorded = await response.json();
  expect(recorded).toMatchObject({ replayed: false });
  await expect(page.getByRole('tab', { name: 'Delivery', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Delivery', exact: true }).click();
  await expect(page.getByTestId('dod-record-form')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Recorded test outcomes' }).getByText('ts · passed', { exact: true })).toHaveCount(3);
  const readback = await page.request.get('/api/v1/boards/board/specs/spec/delivery-evidence');
  expect(readback.status()).toBe(200);
  const rollup = await readback.json();
  expect(rollup.allowed).toBe(false);
  expect(rollup.tests.some((item: { id: string }) => item.id === recorded.id)).toBe(true);
  expect(rollup.rows.filter((row: { test_satisfied: boolean }) => !row.test_satisfied)).toHaveLength(4);

  expect(errors).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('installed-delivery.png'), fullPage: true });
});
