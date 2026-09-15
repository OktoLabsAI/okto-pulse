import { test, expect } from '@playwright/test';

test('records a test association and refreshes delivery readiness without a Spec transition', async ({ page }) => {
  let recorded = false;
  const writes: Record<string, unknown>[] = [];
  await page.route('**/api/v1/**', async route => {
    if (!route.request().url().endsWith('/delivery-evidence')) return route.fulfill({ status: 404, json: { detail: 'Isolated fixture' } });
    if (route.request().method() === 'POST') {
      writes.push(route.request().postDataJSON()); recorded = true;
      return route.fulfill({ json: { id: 'test-proof', replayed: false } });
    }
    return route.fulfill({ json: { board_id: 'fixture', spec_id: 'spec-1', edition: 2, version: 7, status: 'in_progress', allowed: recorded, complete: true, blockers: recorded ? [] : ['delivery_test_result_missing'], rejected_record_ids: [],
      rows: [{ obligation: { title: 'Return the expected version', binding: { obligation_ref: 'ac:1', semantic_sha256: 'a'.repeat(64) } }, implementation_ids: ['impl'], test_ids: recorded ? ['test-proof'] : [], implementation_waiver_ids: [], test_waiver_ids: [], implementation_satisfied: true, test_satisfied: recorded }],
      implementations: [{ id: 'impl', relative_path: 'src/api.py', result_revision: 'a'.repeat(40), current_accepted_execution: true }],
      candidates: [{ kind: 'test', id: 'scenario', card_id: 'test-card', label: 'Version assertion' }], records: [] } });
  });
  await page.goto('/tests/fixtures/delivery-evidence.html');
  await expect(page.getByRole('status')).toContainText('incomplete');
  await page.getByText('How to complete delivery proof').click();
  await expect(page.getByText(/Skip flags do not waive delivery/)).toBeVisible();
  await page.getByLabel('Select ac:1').check();
  await page.getByLabel('Record type').selectOption('test');
  await page.getByRole('combobox', { name: /Accepted current proof/ }).selectOption('test-card:scenario');
  await page.getByRole('checkbox', { name: /src\/api.py/ }).check();
  await page.getByRole('textbox', { name: /Explanation/ }).fill('The test asserted the committed version output.');
  await page.getByRole('button', { name: 'Record association' }).click();
  await expect(page.getByRole('status')).toContainText('Delivery proof complete');
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ kind: 'test', card_id: 'test-card', scenario_id: 'scenario', implementation_ids: ['impl'], expected_edition: 2, expected_version: 7 });
  expect(writes[0]).not.toHaveProperty('status');
  await page.screenshot({ path: 'test-results/delivery-evidence.png', fullPage: true });
});
