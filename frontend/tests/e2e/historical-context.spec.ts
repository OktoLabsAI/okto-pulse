import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';

for (const kind of ['spec', 'card'] as const) {
  test(`packaged ${kind} historical context preserves provenance, safe text and revoked access`, async ({ page }) => {
    test.setTimeout(90_000);
    const errors: string[] = [], writes: string[] = [], reads: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const dates = { created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z' };
    const board = { ...dates, id: 'board-1', name: 'Context Board', owner_id: 'local-user', settings: {}, cards: [], agents: [] };
    const spec = { ...dates, id: 'spec-1', board_id: board.id, title: 'Context fixture Spec', status: 'draft', version: 1, edition: 1,
      created_by: 'local-user', description: null, context: null, ideation_id: null, refinement_id: null, assignee_id: null,
      functional_requirements: [], technical_requirements: [], acceptance_criteria: [], test_scenarios: [], business_rules: [],
      api_contracts: [], integration_requirements: [], observability_requirements: [], decisions: [], labels: [],
      screen_mockups: [], architecture_designs: [], cards: [], knowledge_bases: [], qa_items: [] };
    const card = { ...dates, id: 'card-1', board_id: board.id, title: 'Context fixture Card', status: 'not_started',
      priority: 'medium', position: 0, created_by: 'local-user', spec_id: spec.id, sprint_id: null, card_type: 'normal',
      description: null, details: null, assignee_id: null, due_date: null, labels: [], test_scenario_ids: [],
      screen_mockups: [], knowledge_bases: [], conclusions: [], validations: [], attachments: [], qa_items: [], comments: [], architecture_designs: [] };
    await page.addInitScript(() => {
      localStorage.setItem('okto.onboarding.completed.v1', 'true');
      localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
      localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({ schemaVersion: 1,
        updatedAt: new Date().toISOString(), skippedAll: true, skippedAllAt: new Date().toISOString(), tours: {} }));
    });
    // Every API request terminates here. No fixture interaction writes real Pulse data.
    await page.route('**/api/v1/**', async (route, request) => {
      const url = new URL(request.url()), path = url.pathname;
      if (request.method() !== 'GET') {
        writes.push(path);
        return route.fulfill({ status: 403, json: { detail: 'Writes blocked by fixture' } });
      }
      if (path.includes('/historical-context/')) {
        reads.push(path);
        if (url.searchParams.get('offset') === '1') return route.fulfill({ status: 404, json: { detail: 'Historical context not found' } });
        return route.fulfill({ json: { format: 'historical-context/v1', board_id: board.id, target: { kind, id: `${kind}-1` },
          items: [{ binding_id: 'binding', origin: { kind: 'sprint', id: 'archived-origin' }, archive_id: 'archive', section: 'qa', field: null,
            record: { question: '<script>original context</script>', asked_by: 'original-author', answer: null,
              created_at: '2025-01-02T03:04:05Z', choices: ['Long original choice '.repeat(24)] } }], next_offset: 1 } });
      }
      if (path === '/api/v1/boards') return route.fulfill({ json: url.searchParams.get('view') === 'shared' ? [] : [board] });
      if (path === '/api/v1/boards/board-1') return route.fulfill({ json: board });
      if (path === '/api/v1/specs/spec-1') return route.fulfill({ json: spec });
      if (path === '/api/v1/cards/card-1') return route.fulfill({ json: card });
      if (path.endsWith('/columns')) {
        const columns = { not_started: [card], started: [], in_progress: [], validation: [], rejected: [], on_hold: [], done: [], cancelled: [] };
        return route.fulfill({ json: { board_id: board.id, columns, columns_meta: {
          columns: Object.fromEntries(Object.entries(columns).map(([status, items]) => [status, {
            total_filtered: items.length, total_overall: items.length, has_more: false, facets: { card_type: { normal: items.length } },
          }])), facets: { assignee: [{ value: null, count: 1 }] },
        } } });
      }
      if (path === '/api/v1/boards/board-1/specs') return route.fulfill({ json: { items: [spec], total_filtered: 1, total_overall: 1, offset: 0, limit: 25, has_more: false } });
      if (path.endsWith('/stories')) return route.fulfill({ json: { items: [], total_filtered: 0, total_overall: 0, offset: 0, limit: 25, has_more: false } });
      if (path.endsWith('/allowed-transitions')) return route.fulfill({ json: { board_id: board.id,
        entity_type: url.searchParams.get('entity_type'), entity_id: url.searchParams.get('entity_id'),
        current_status: kind === 'spec' ? 'draft' : 'not_started', allowed_transitions: [], source: 'core_sdlc_registry_v1' } });
      if (path.endsWith('/effective-resources')) return route.fulfill({ json: { board_id: board.id, entity_type: kind, entity_id: `${kind}-1`,
        profile: 'summary', items: [], next_cursor: null, resources: { architecture: [], mockup: [], knowledge_base: [] } } });
      if (path.endsWith('/seen-status')) return route.fulfill({ json: { items: {} } });
      if (/\/(sprints|agents|topics|qa|dependencies|dependents|activity)$/.test(path)) return route.fulfill({ json: [] });
      if (path === '/api/v1/me/permissions') return route.fulfill({ json: { board_id: board.id, preset_name: 'fixture', owner_review_required: false, flags: {} } });
      return route.fulfill({ status: 404, json: { detail: 'Unmocked read blocked' } });
    });
    await page.goto('/?accept_terms=1');
    await page.getByRole('button', { name: kind === 'spec' ? 'Specs' : 'Tasks', exact: true }).click();
    await page.getByText(kind === 'spec' ? spec.title : card.title, { exact: true }).click();
    await page.getByRole('tab', { name: 'Historical context', exact: true }).click();
    const panel = page.getByRole('region', { name: 'Historical context', exact: true });
    await expect(panel.getByText('<script>original context</script>', { exact: true })).toBeVisible();
    await expect(panel.getByText('original-author', { exact: true })).toBeVisible();
    await expect(panel.getByText(/sprint · archived-origin/)).toBeVisible();
    await expect(panel.getByRole('link')).toHaveCount(0);
    await expect(panel.getByRole('textbox')).toHaveCount(0);
    for (const width of [360, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      for (const dark of [false, true]) {
        await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
        const audit = await new AxeBuilder({ page }).include('[aria-label="Historical context"]').analyze();
        expect(audit.violations.filter(item => ['serious', 'critical'].includes(item.impact ?? ''))).toEqual([]);
        expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
        if (width === 360 && !dark) await page.screenshot({ path: test.info().outputPath(`${kind}-context-360.png`) });
      }
    }
    await panel.getByRole('button', { name: 'Next context page' }).click();
    await expect(panel.getByRole('alert')).toContainText('no longer have access');
    await expect(panel.getByText('original-author')).toHaveCount(0);
    expect(reads).toEqual(Array(2).fill(`/api/v1/boards/board-1/historical-context/${kind}/${kind}-1`));
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
}
