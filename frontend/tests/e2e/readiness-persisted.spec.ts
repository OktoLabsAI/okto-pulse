import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';

const fixture = '/tests/fixtures/readiness-persisted.html';

test('SC19: a delayed report retains g1 and swaps totals with obligation rows at g2', async ({ page }) => {
  await page.goto(fixture);
  const panel = page.getByRole('region', { name: 'Implementation and verification' });
  const coverage = panel.getByTestId('delivery-coverage-table');
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Implementation 75% · Verification 50%');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '75');
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '50');
  await expect(coverage.getByRole('row', { name: /FR \(four\)/ })).toContainText('Pending');

  await page.getByRole('button', { name: 'Hold next read' }).click();
  await page.getByRole('button', { name: 'Trigger report' }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Updating');
  await expect(coverage.getByRole('row', { name: /FR \(four\)/ })).toContainText('Pending');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '75');
  await page.getByRole('button', { name: 'Release read' }).click();
  await page.getByRole('button', { name: 'Publish g2' }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g2' })).toContainText('Implementation 100% · Verification 75%');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '100');
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  await expect(coverage.getByRole('row', { name: /FR \(four\)/ })).toContainText('Current proof');
  await expect(coverage.getByRole('row', { name: /FR \(three\)/ })).toContainText('Verified');
});

test('SC20: initial, unknown and N/A states remain distinct; changing board clears the prior generation', async ({ page }) => {
  const panel = page.getByRole('region', { name: 'Implementation and verification' });
  await page.goto(`${fixture}?mode=pending`);
  await expect(panel.getByRole('status').filter({ hasText: 'Waiting for calculation' }).first()).toBeVisible();
  await expect(panel.getByRole('progressbar')).toHaveCount(0);
  await expect(panel.getByText('0%', { exact: true })).toHaveCount(0);
  await page.goto(`${fixture}?mode=unknown`);
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Implementation Unknown');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveCount(0);
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '50');
  await page.goto(`${fixture}?mode=na`);
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Implementation N/A');
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '50');
  await page.getByRole('button', { name: 'Switch board' }).click();
  await expect(panel.getByRole('alert')).toContainText('Denied');
  await expect(panel.getByTestId('delivery-coverage-table')).toHaveCount(0);
  await expect(panel.getByRole('progressbar')).toHaveCount(0);
});

test('SC21: accessible labels expose phase, freshness and proof state to keyboard and assistive technology', async ({ page }) => {
  await page.goto(fixture);
  const panel = page.getByRole('region', { name: 'Implementation and verification' });
  const implementation = panel.getByRole('progressbar', { name: 'Implementation' });
  const verification = panel.getByRole('progressbar', { name: 'Verification' });
  await expect(implementation).toHaveAttribute('aria-valuetext', '75%, 3 of 4 applicable obligations');
  await expect(verification).toHaveAttribute('aria-valuetext', '50%, 2 of 4 applicable obligations');
  await expect(panel.getByRole('columnheader', { name: 'Implementation' })).toBeVisible();
  await expect(panel.getByRole('columnheader', { name: 'Verification' })).toBeVisible();
  await page.getByRole('button', { name: 'Hold next read' }).click();
  await page.getByRole('button', { name: 'Trigger report' }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Updating');
  const refresh = panel.getByRole('button', { name: 'Refresh delivery rollup' });
  await refresh.focus();
  await expect(refresh).toBeFocused();
  const audit = await new AxeBuilder({ page }).include('section[aria-label="Implementation and verification"]')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(audit.violations.map(violation => ({ id: violation.id,
    targets: violation.nodes.map(node => node.target) }))).toEqual([]);
});
