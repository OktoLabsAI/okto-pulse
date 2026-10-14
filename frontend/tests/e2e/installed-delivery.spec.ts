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
  await modal.getByRole('tab', { name: 'Coverage', exact: true }).click();
  await modal.getByRole('tab', { name: 'Implementation', exact: true }).click();
  const readinessResponse = await page.request.get('/api/v1/boards/board/specs/spec/delivery-evidence?view=readiness');
  expect(readinessResponse.status()).toBe(200);
  const before = await readinessResponse.json();
  expect(before.contract_version).toBe('delivery-readiness/v1');
  expect(before.implementation).toMatchObject({ percent: 42, satisfied: 3, applicable: 7 });
  expect(before.verification).toMatchObject({ percent: 42, satisfied: 3, applicable: 7 });
  const priorRollupResponse = await page.request.get('/api/v1/boards/board/specs/spec/delivery-evidence');
  expect(priorRollupResponse.status()).toBe(200);
  const priorRollup = await priorRollupResponse.json();
  const previousOutcomeCount = priorRollup.tests.filter((item: { card_id: string }) => item.card_id === 'test-card').length;
  expect(previousOutcomeCount).toBeGreaterThan(0);
  const metrics = modal.getByRole('region', { name: 'Delivery readiness', exact: true });
  await expect(metrics.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '42');
  await expect(metrics.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '42');
  // U09: inspect the browser accessibility tree consumed by assistive
  // technology, not only the presence of ARIA attributes in the DOM.
  const accessibility = await page.context().newCDPSession(page);
  await accessibility.send('Accessibility.enable');
  const tree = await accessibility.send('Accessibility.getFullAXTree');
  const bars = tree.nodes.filter(node => !node.ignored && node.role?.value === 'progressbar');
  for (const label of ['Implementation', 'Verification']) {
    const bar = bars.find(node => node.name?.value === label && node.value?.value === 42);
    expect(bar, `${label} must expose its current value to assistive technology`).toBeDefined();
    const properties = Object.fromEntries(bar!.properties!.map(property => [property.name, property.value.value]));
    expect(properties).toMatchObject({ valuemin: 0, valuemax: 100,
      valuetext: '42% · 3/7 applicable obligations' });
  }
  await test.info().attach('installed-readiness-accessibility', {
    body: JSON.stringify(bars, null, 2), contentType: 'application/json',
  });
  await accessibility.detach();
  await metrics.screenshot({ path: test.info().outputPath('installed-readiness-metrics.png') });
  await expect(modal.getByText('Blocked', { exact: true })).toBeVisible();
  const table = modal.getByTestId('delivery-coverage-table');
  await expect(table.getByText('✓', { exact: true })).toHaveCount(6);
  await expect(table.getByText('◌', { exact: true })).toHaveCount(8);
  await modal.getByRole('button', { name: 'Close spec' }).click();
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByText('Observe blocking', { exact: true }).first().click();
  await page.getByRole('tab', { name: 'Delivery', exact: true }).click();
  await page.getByText('How this affects delivery', { exact: true }).click();
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
  const outcomes = page.getByRole('region', { name: 'Recorded test outcomes' });
  await expect(outcomes.getByText('passed', { exact: true })).toHaveCount(previousOutcomeCount + 1);
  await expect(outcomes.getByText(/Closing cards is a separate workflow step/)).toBeVisible();
  const readback = await page.request.get('/api/v1/boards/board/specs/spec/delivery-evidence');
  expect(readback.status()).toBe(200);
  const rollup = await readback.json();
  expect(rollup.allowed).toBe(false);
  expect(rollup.tests.some((item: { id: string }) => item.id === recorded.id)).toBe(true);
  expect(rollup.rows.filter((row: { test_satisfied: boolean }) => !row.test_satisfied)).toHaveLength(4);
  const afterResponse = await page.request.get('/api/v1/boards/board/specs/spec/delivery-evidence?view=readiness');
  expect(afterResponse.status()).toBe(200);
  const after = await afterResponse.json();
  // M23: associating the same authenticated result again does not manufacture
  // credit for the four missing IR obligations or change the denominator.
  expect(after.implementation).toEqual(before.implementation);
  expect(after.verification).toEqual(before.verification);

  expect(errors).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('installed-delivery.png'), fullPage: true });
});
