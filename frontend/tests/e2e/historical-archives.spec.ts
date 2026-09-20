import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';

test('packaged archive navigation, section access, revocation, responsive layout and accessibility', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  const archiveRequests: string[] = [];
  const writes: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const board = { id: 'board-1', name: 'Archive Board', owner_id: 'local-user', settings: {}, cards: [], agents: [],
    created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z' };
  const origin = { kind: 'sprint', id: 'historical-origin' };
  await page.addInitScript(() => {
    localStorage.setItem('okto.onboarding.completed.v1', 'true');
    localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
    localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({ schemaVersion: 1,
      updatedAt: new Date().toISOString(), skippedAll: true, skippedAllAt: new Date().toISOString(), tours: {} }));
  });
  // All network API calls terminate in fixtures: this test cannot mutate real Pulse data.
  await page.route('**/api/v1/**', async (route, request) => {
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() !== 'GET') {
      writes.push(path);
      return route.fulfill({ status: 403, json: { detail: 'Fixture rejects writes' } });
    }
    if (path.includes('/historical-archives')) {
      archiveRequests.push(path);
      if (path.endsWith('/historical-archives')) return route.fulfill({ json: {
        format: 'historical-archive-discovery/v1', board_id: board.id,
        items: [{ origin, archive_id: 'archive', sections: ['content', 'history'] }], next_offset: null,
      } });
      if (url.searchParams.get('offset') === '1') return route.fulfill({ status: 404, json: { detail: 'Historical archive not found' } });
      const section = path.endsWith('/history') ? 'history' : 'content';
      return route.fulfill({ json: { format: 'historical-archive-section/v1', board_id: board.id, origin,
        archive_id: 'archive', section, next_offset: section === 'history' ? 1 : null,
        records: section === 'history' ? [{ summary: 'Original audit evidence', actor_id: 'historical-author', version: 3 }]
          : [{ title: 'Archived delivery', description: '<script>original text</script>', related_spec_id: 'opaque-old-spec',
            objective: 'Long historical context '.repeat(30) }],
      } });
    }
    if (path === '/api/v1/boards') return route.fulfill({ json: url.searchParams.get('view') === 'shared' ? [] : [board] });
    if (path === '/api/v1/boards/board-1') return route.fulfill({ json: board });
    if (path.endsWith('/columns')) return route.fulfill({ json: { board_id: board.id, columns: {
      not_started: [], started: [], in_progress: [], validation: [], on_hold: [], done: [], cancelled: [],
    } } });
    if (path.endsWith('/stories')) return route.fulfill({ json: { items: [], total_filtered: 0, total_overall: 0, offset: 0, limit: 25, has_more: false } });
    if (path.endsWith('/topics')) return route.fulfill({ json: [] });
    if (path === '/api/v1/me/permissions') return route.fulfill({ json: { board_id: board.id,
      preset_name: 'archive-fixture', owner_review_required: false, flags: {} } });
    return route.fulfill({ status: 404, json: { detail: 'Unmocked reads blocked' } });
  });
  await page.goto('/?accept_terms=1');
  await page.getByRole('button', { name: 'Archives', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Historical archives' });
  await panel.getByRole('button', { name: 'sprint · historical-origin' }).click();
  await expect(panel.getByText('Archived delivery', { exact: true })).toBeVisible();
  await expect(panel.getByText('<script>original text</script>', { exact: true })).toBeVisible();
  await expect(panel.getByRole('link')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Questions & answers' })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Evaluations' })).toHaveCount(0);
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    // The Board shell already provides a collapsible 256px navigation sidebar.
    // Exercise that real control on mobile; don't squeeze reading into 72px.
    if (width === 360) await page.getByRole('button', { name: 'Hide sidebar' }).click();
    else if (await page.getByRole('button', { name: 'Show sidebar' }).count()) {
      await page.getByRole('button', { name: 'Show sidebar' }).click();
    }
    await expect.poll(() => panel.evaluate(node => node.clientWidth)).toBeGreaterThan(280);
    for (const dark of [false, true]) {
      await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
      const report = await new AxeBuilder({ page }).include('[aria-label="Historical archives"]').analyze();
      expect(report.violations.filter(item => ['serious', 'critical'].includes(item.impact ?? ''))).toEqual([]);
      expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
      if (width === 360 && !dark) await page.screenshot({ path: test.info().outputPath('archives-360.png') });
    }
  }
  await panel.getByRole('button', { name: 'History', exact: true }).click();
  await expect(panel.getByText('Original audit evidence')).toBeVisible();
  await panel.getByRole('button', { name: 'Next section page' }).click();
  await expect(panel.getByRole('alert')).toContainText('no longer have access');
  await expect(panel.getByText('Original audit evidence')).toHaveCount(0);
  expect(archiveRequests.some(path => /\/(qa|evaluations)$/.test(path))).toBe(false);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
