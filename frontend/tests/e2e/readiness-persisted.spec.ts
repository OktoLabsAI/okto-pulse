import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

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
  await page.evaluate(() => {
    const observed = window as Window & { __readinessPerceivedAt?: number };
    const root = document.getElementById('root')!;
    const observer = new MutationObserver(() => {
      if (root.textContent?.includes('Generation g2')) {
        observed.__readinessPerceivedAt = performance.now();
        observer.disconnect();
      }
    });
    observer.observe(root, { subtree: true, childList: true, characterData: true });
  });
  await page.getByRole('button', { name: 'Publish g2' }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Generation g2' })).toContainText('Implementation 100% · Verification 75%');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '100');
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  await expect(coverage.getByRole('row', { name: /FR \(four\)/ })).toContainText('Current proof');
  await expect(coverage.getByRole('row', { name: /FR \(three\)/ })).toContainText('Verified');
  const perceptionMs = await page.evaluate(() => {
    const observed = window as Window & { __readinessPublishedAt?: number; __readinessPerceivedAt?: number };
    return observed.__readinessPerceivedAt! - observed.__readinessPublishedAt!;
  });
  await test.info().attach('readiness-ui-perception-ms', {
    body: perceptionMs.toFixed(3), contentType: 'text/plain',
  });
  console.log(`SC19 browser perception: ${perceptionMs.toFixed(3)} ms`);
  expect(perceptionMs).toBeLessThanOrEqual(1000);
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

test('SC20: a failing Spec retains its prior generation while another Spec remains current', async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole('button', { name: 'Show healthy Spec' }).click();
  const panels = page.getByRole('region', { name: 'Implementation and verification' });
  const primary = panels.first();
  const healthy = panels.last();
  await expect(primary.getByRole('status').filter({ hasText: 'Generation g1' })).toBeVisible();
  await expect(healthy.getByRole('status').filter({ hasText: 'Generation healthy-g1' })).toContainText('Current');

  await page.getByRole('button', { name: 'Fail primary Spec' }).click();
  await expect(primary.getByRole('status').filter({ hasText: 'Generation g1' })).toContainText('Calculation unavailable');
  await expect(primary.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '75');
  await expect(healthy.getByRole('status').filter({ hasText: 'Generation healthy-g1' })).toContainText('Current');
  await expect(healthy.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '50');
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

test('persisted panel renders percentages within one second in 100 warm openings', async ({ page }) => {
  await page.addInitScript(() => {
    const measured = window as Window & { __readinessFirstReadyMs?: number };
    const check = () => {
      if (measured.__readinessFirstReadyMs !== undefined) return;
      const panel = document.querySelector('section[aria-label="Implementation and verification"]');
      const implementation = panel?.querySelector('[role="progressbar"][aria-label="Implementation"]');
      const verification = panel?.querySelector('[role="progressbar"][aria-label="Verification"]');
      if (implementation?.getAttribute('aria-valuenow') === '75'
          && verification?.getAttribute('aria-valuenow') === '50') {
        measured.__readinessFirstReadyMs = performance.now();
        observer.disconnect();
      }
    };
    const observer = new MutationObserver(check);
    document.addEventListener('DOMContentLoaded', () => {
      observer.observe(document.documentElement, { subtree: true, childList: true,
        attributes: true, attributeFilter: ['aria-valuenow'] });
      check();
    }, { once: true });
  });

  // Warm the browser and Vite before measuring. Each following navigation is a
  // fresh document; the init script records the first DOM commit with both metrics.
  await page.goto(fixture);
  await expect(page.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '75');
  const samples: number[] = [];
  for (let index = 0; index < 100; index++) {
    await page.goto(`${fixture}?opening=${index}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => Number.isFinite(
      (window as Window & { __readinessFirstReadyMs?: number }).__readinessFirstReadyMs));
    samples.push(await page.evaluate(() =>
      (window as Window & { __readinessFirstReadyMs: number }).__readinessFirstReadyMs));
  }
  const sorted = [...samples].sort((a, b) => a - b);
  const report = {
    openings: samples.length,
    p50_ms: sorted[49], p95_ms: sorted[94], p99_ms: sorted[98], max_ms: sorted[99],
    samples_ms: samples,
    surface: 'disposable React panel fixture; full installed board still requires separate proof',
  };
  await test.info().attach('readiness-panel-100-openings.json', {
    body: JSON.stringify(report, null, 2), contentType: 'application/json',
  });
  mkdirSync('output/playwright', { recursive: true });
  writeFileSync('output/playwright/readiness-panel-100-openings.json',
    JSON.stringify(report, null, 2), 'utf8');
  expect(samples).toHaveLength(100);
  expect(report.max_ms).toBeLessThanOrEqual(1000);
});
