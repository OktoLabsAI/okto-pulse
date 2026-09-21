import { expect, test } from '@playwright/test';

test('packaged lineage connects Spec to task and opens its details without a Sprint stage', async ({ page }) => {
  const errors: string[] = [], writes: string[] = [], selections: string[] = [];
  let cardReads = 0;
  page.on('pageerror', error => errors.push(error.message));
  const dates = { created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z' };
  const board = { ...dates, id: 'board-1', name: 'Lineage Board', owner_id: 'local-user', settings: {}, cards: [], agents: [] };
  const spec = { ...dates, id: 'spec-1', board_id: board.id, title: 'Lineage fixture Spec', status: 'draft', version: 1, edition: 1,
    created_by: 'local-user', description: null, context: null, ideation_id: null, refinement_id: null, assignee_id: null,
    functional_requirements: [], technical_requirements: [], acceptance_criteria: [], test_scenarios: [], business_rules: [],
    api_contracts: [], integration_requirements: [], observability_requirements: [], decisions: [], labels: [],
    screen_mockups: [], architecture_designs: [], cards: [], knowledge_bases: [], qa_items: [] };
  const card = { ...dates, id: 'card-1', board_id: board.id, title: 'Lineage fixture Task', status: 'not_started',
    priority: 'medium', position: 0, created_by: 'local-user', spec_id: spec.id, card_type: 'normal',
    description: null, details: null, assignee_id: null, due_date: null, labels: [], test_scenario_ids: [],
    screen_mockups: [], knowledge_bases: [], conclusions: [], validations: [], attachments: [], qa_items: [], comments: [], architecture_designs: [] };
  await page.addInitScript(() => {
    localStorage.setItem('okto.onboarding.completed.v1', 'true');
    localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
    localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({ schemaVersion: 1,
      updatedAt: new Date().toISOString(), skippedAll: true, skippedAllAt: new Date().toISOString(), tours: {} }));
  });
  // Every API request ends in this fixture; this browser test never touches a runtime database.
  await page.route('**/api/v1/**', async (route, request) => {
    const url = new URL(request.url()), path = url.pathname;
    if (request.method() !== 'GET') {
      writes.push(path);
      return route.fulfill({ status: 403, json: { detail: 'Fixture is read-only' } });
    }
    if (path.endsWith('/lineage-graph')) {
      selections.push(url.searchParams.get('entity_type') ?? '');
      return route.fulfill({ json: { board_id: board.id, selected: { entity_type: 'spec', entity_id: spec.id },
        root_entity: { type: 'spec', id: spec.id, title: spec.title, status: spec.status },
        root_ideation: { id: spec.id, title: spec.title, status: spec.status, entity_type: 'spec' },
        resolution_path: [{ type: 'spec', id: spec.id }], nodes: [
          { id: 'spec:spec-1', entity_type: 'spec', entity_id: spec.id, title: spec.title, label: spec.title, status: spec.status, stage: 2 },
          { id: 'task:card-1', entity_type: 'task', entity_id: card.id, title: card.title, label: card.title, status: card.status, stage: 3 },
        ], edges: [{ id: 'spec-task', source: 'spec:spec-1', target: 'task:card-1', relationship: 'has_card' }],
        summary: { specs: 1, tasks: 1, nodes: 2, edges: 1 }, warnings: [] } });
    }
    if (path === '/api/v1/boards') return route.fulfill({ json: url.searchParams.get('view') === 'shared' ? [] : [board] });
    if (path === '/api/v1/boards/board-1') return route.fulfill({ json: board });
    if (path === '/api/v1/specs/spec-1') return route.fulfill({ json: spec });
    if (path === '/api/v1/cards/card-1') { cardReads += 1; return route.fulfill({ json: card }); }
    if (path.endsWith('/columns')) {
      const columns = { not_started: [card], started: [], in_progress: [], validation: [], rejected: [], on_hold: [], done: [], cancelled: [] };
      return route.fulfill({ json: { board_id: board.id, columns, columns_meta: {
        columns: Object.fromEntries(Object.entries(columns).map(([status, items]) => [status, {
          total_filtered: items.length, total_overall: items.length, has_more: false, facets: { card_type: { normal: items.length } },
        }])), facets: { assignee: [{ value: null, count: 1 }] },
      } } });
    }
    if (path.endsWith('/specs')) return route.fulfill({ json: { items: [spec], total_filtered: 1, total_overall: 1, offset: 0, limit: 25, has_more: false } });
    if (path.endsWith('/stories')) return route.fulfill({ json: { items: [], total_filtered: 0, total_overall: 0, offset: 0, limit: 25, has_more: false } });
    if (path.endsWith('/allowed-transitions')) return route.fulfill({ json: { board_id: board.id,
      entity_type: url.searchParams.get('entity_type'), entity_id: url.searchParams.get('entity_id'),
      current_status: 'draft', allowed_transitions: [], source: 'core_sdlc_registry_v1' } });
    if (path.endsWith('/effective-resources')) return route.fulfill({ json: { board_id: board.id, entity_type: 'spec', entity_id: spec.id,
      profile: 'summary', items: [], next_cursor: null, resources: { architecture: [], mockup: [], knowledge_base: [] } } });
    if (path.endsWith('/seen-status')) return route.fulfill({ json: { items: {} } });
    if (/\/(sprints|agents|topics|qa|dependencies|dependents|activity)$/.test(path)) return route.fulfill({ json: [] });
    if (path === '/api/v1/me/permissions') return route.fulfill({ json: { board_id: board.id, preset_name: 'fixture', owner_review_required: false, flags: {} } });
    return route.fulfill({ status: 404, json: { detail: 'Unmocked read blocked' } });
  });
  await page.goto('/?accept_terms=1');
  await page.getByRole('button', { name: 'Specs', exact: true }).click();
  await page.getByText(spec.title, { exact: true }).click();
  await page.getByRole('dialog', { name: spec.title }).getByTitle('Open lineage graph').click();
  const dialog = page.getByRole('dialog', { name: 'SDLC Lineage' });
  try {
    await expect(dialog).toBeVisible();
  } catch (error) {
    if (errors.length) throw new Error(errors.join('\n'));
    throw error;
  }
  const legend = dialog.getByTestId('lineage-stage-bar');
  await expect(legend.getByText('Sprint', { exact: true })).toHaveCount(0);
  await expect(legend.getByText('Tasks / Tests', { exact: true })).toBeVisible();
  await expect(dialog.locator('.react-flow__edge[data-id="spec-task"]')).toHaveCount(1);
  await dialog.locator('.react-flow__node[data-id="task:card-1"]').click();
  await dialog.getByRole('button', { name: 'Show details', exact: true }).click();
  await expect.poll(() => cardReads).toBeGreaterThan(0);
  expect(selections).toEqual(['spec']);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
