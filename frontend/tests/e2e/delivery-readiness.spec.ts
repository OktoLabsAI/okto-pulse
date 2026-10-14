import { expect, test } from '@playwright/test';
import { readiness, task, metric } from '../../src/components/code-traceability/__tests__/readinessFixture';

test('P08/U01/U04/U09: native board and Delivery show separate accessible metrics', async ({ page }) => {
  const errors: string[] = [], writes: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const card = { ...task, title: 'Readiness capacity verification with a long descriptive requirement name',
    status: 'done', screen_mockups: [], knowledge_bases: [], attachments: [], qa_items: [], comments: [], architecture_designs: [] };
  const board = { id: 'b', name: 'Readiness fixture', owner_id: 'local-user',
    created_at: task.created_at, updated_at: task.updated_at, settings: { delivery_evidence_gate: 'blocking' }, agents: [] };
  const spec = { id: 's', board_id: 'b', title: 'Readiness Spec', status: 'in_progress', version: 1, edition: 1,
    created_at: task.created_at, updated_at: task.updated_at, created_by: 'local-user',
    functional_requirements: [], technical_requirements: [], acceptance_criteria: [{ id: 'capacity', title: 'Capacity limit',
      text: 'Reject reservations above the room capacity.', status: 'active' }], test_scenarios: [], business_rules: [],
    api_contracts: [], integration_requirements: [], observability_requirements: [], decisions: [], labels: [],
    screen_mockups: [], architecture_designs: [], cards: [], knowledge_bases: [], qa_items: [] };
  const current = readiness(); current.lifecycle.stored_status = 'done';
  current.summary.message_code = 'historical_completion_diverged';
  current.page = { total: 1, returned: 1, has_more: false, next_cursor: null, response_truncated: false, phase_filter: 'all' };
  current.issues = [{
    id: 'capacity-check', code: 'verification_not_run', detail_state: 'available', phase: 'verification',
    score_impact: 'verification', intrinsic_state: 'missing', blocks_proof: true, blocks_target_transition: null,
    message_code: 'verification_not_run', message_params: {}, message: 'Observe the capacity boundary on the delivered revision.',
    native_reason_codes: [], affected_unit_count: 1,
    affected_units: [{ unit: 'obligation', ref: 'ac:capacity', semantic_sha256: 'a'.repeat(64), spec_edition: 1, card_id: 'c' }],
    affected_units_truncated: false, criterion_ids: ['capacity'], criterion_count: 1, criterion_ids_truncated: false,
    scenario_ids: ['capacity-boundary'], scenario_count: 1, scenario_ids_truncated: false,
    implementation_card_ids: ['c'], implementation_card_count: 1, implementation_card_ids_truncated: false,
    test_card_ids: [], test_card_count: 0, test_card_ids_truncated: false, owner: null, owner_state: 'unassigned',
    expected: [{ outcome: 'passed', revision: 'delivered-revision' }], expected_count: 1, expected_truncated: false,
    observed: [{ outcome: 'not_run', text: 'No authenticated observation for this scenario.' }], observed_count: 1, observed_truncated: false,
    evidence_refs: [], evidence_count: 0, evidence_refs_truncated: false, pinpoints: [], pinpoint_count: 0, pinpoints_truncated: false,
    root_cause_id: 'capacity-check', depends_on_issue_ids: [], action_ids: [],
    detail_query: { board_id: 'b', spec_id: 's', card_id: 'c', view: 'readiness', issue_id: 'capacity-check', snapshot_id: current.snapshot_id, limit: 20 },
  }];
  const specReadiness = readiness(null);
  specReadiness.page = current.page;
  specReadiness.issues = current.issues.map(issue => ({ ...issue, detail_query: { ...issue.detail_query, card_id: null } }));
  // Distinct global metrics ensure that the Spec never averages visible Cards.
  specReadiness.verification = metric(50, 4);
  await page.addInitScript(() => {
    localStorage.setItem('okto.onboarding.completed.v1', 'true');
    localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
    localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({ schemaVersion: 1,
      updatedAt: new Date().toISOString(), skippedAll: true, skippedAllAt: new Date().toISOString(), tours: {} }));
  });
  // Every request is intercepted. This test never reads or mutates a live Pulse database.
  await page.route('**/api/v1/**', async (route, request) => {
    const url = new URL(request.url()), path = url.pathname;
    if (request.method() !== 'GET') {
      writes.push(path); return route.fulfill({ status: 403, json: { detail: 'Read-only fixture' } });
    }
    if (path.endsWith('/delivery-readiness-summary')) return route.fulfill({ json: {
      contract_version: 'delivery-readiness/v1', metrics_version: 2, delivery_readiness: specReadiness,
      cards: [{ card_id: 'c', delivery_readiness: current }] } });
    if (path.endsWith('/delivery-evidence')) return route.fulfill({ json: url.searchParams.get('view') === 'readiness'
      ? (url.searchParams.get('card_id') ? current : specReadiness)
      : { board_id: 'b', spec_id: 's', edition: 1, version: 1, complete: true, allowed: false,
        candidates: [], implementations: [], rows: [], tests: [], rejected_record_ids: [], per_card: [] } });
    if (path === '/api/v1/boards') return route.fulfill({ json: url.searchParams.get('view') === 'shared' ? [] : [board] });
    if (path === '/api/v1/boards/b') return route.fulfill({ json: board });
    if (path === '/api/v1/specs/s') return route.fulfill({ json: spec });
    if (path === '/api/v1/cards/c') return route.fulfill({ json: card });
    if (path.endsWith('/columns')) {
      const columns = { not_started: [], started: [], in_progress: [], validation: [], rejected: [], on_hold: [], done: [card], cancelled: [] };
      return route.fulfill({ json: { board_id: 'b', columns, columns_meta: {
        columns: Object.fromEntries(Object.entries(columns).map(([status, items]) => [status, {
          total_filtered: items.length, total_overall: items.length, has_more: false, facets: { card_type: { normal: items.length } },
        }])), facets: { assignee: [{ value: null, count: 1 }] } } } });
    }
    if (path.endsWith('/specs')) return route.fulfill({ json: { items: [spec], total_filtered: 1, total_overall: 1, offset: 0, limit: 25, has_more: false } });
    if (path.endsWith('/stories')) return route.fulfill({ json: { items: [], total_filtered: 0, total_overall: 0, offset: 0, limit: 25, has_more: false } });
    if (path.endsWith('/allowed-transitions')) return route.fulfill({ json: { board_id: 'b', entity_type: url.searchParams.get('entity_type'),
      entity_id: url.searchParams.get('entity_id'), current_status: 'done', allowed_transitions: [], source: 'core_sdlc_registry_v1' } });
    if (path.endsWith('/effective-resources')) return route.fulfill({ json: { board_id: 'b', entity_type: 'card', entity_id: 'c',
      profile: 'summary', items: [], next_cursor: null, resources: { architecture: [], mockup: [], knowledge_base: [] } } });
    if (path.endsWith('/seen-status')) return route.fulfill({ json: { items: {} } });
    if (/\/(agents|topics|qa|dependencies|dependents|activity)$/.test(path)) return route.fulfill({ json: [] });
    if (path === '/api/v1/me/permissions') return route.fulfill({ json: { board_id: 'b', preset_name: 'fixture', owner_review_required: false,
      flags: { code_traceability: { evidence: { read: true }, investigation: { read: true },
        target: { read: true }, overlap: { read: true } }, card: { entity: { read: true } }, spec: { entity: { read: true } } } } });
    return route.fulfill({ status: 404, json: { detail: 'Unmocked read blocked' } });
  });
  await page.goto('/?accept_terms=1');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  const cover = page.locator('.kanban-card').filter({ hasText: card.title });
  await expect(cover).toBeVisible();
  const implementation = cover.getByRole('progressbar', { name: 'Implementation' });
  const verification = cover.getByRole('progressbar', { name: 'Verification' });
  await expect(implementation).toHaveAttribute('aria-valuenow', '100');
  await expect(verification).toHaveAttribute('aria-valuetext', '75% · 6/8 applicable obligations');
  const blue = await implementation.boundingBox(), green = await verification.boundingBox();
  expect(blue!.y + blue!.height).toBeLessThan(green!.y);
  expect(await cover.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await cover.screenshot({ path: test.info().outputPath('readiness-cover.png') });
  const inspect = cover.getByRole('button', { name: 'What is missing?' });
  await inspect.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('tab', { name: 'Delivery', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: 'What is missing?' })).toBeFocused();
  const diagnosis = page.getByRole('region', { name: 'Delivery diagnosis' });
  await diagnosis.getByText('Observe the capacity boundary on the delivered revision.').click();
  await expect(diagnosis.getByText('Expected', { exact: true })).toBeVisible();
  await expect(diagnosis.getByText('Observed', { exact: true })).toBeVisible();
  await diagnosis.screenshot({ path: test.info().outputPath('readiness-diagnosis.png') });
  const panel = page.getByRole('region', { name: 'Delivery readiness', exact: true }).last();
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  await expect(panel.getByText('Status: done')).toBeVisible();
  await expect(panel.getByText('Historical completion · current qualification pending')).toBeVisible();
  await panel.getByRole('button', { name: 'Refresh', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '100');
  await panel.screenshot({ path: test.info().outputPath('readiness-delivery-light.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' });
  await page.evaluate(() => document.documentElement.classList.add('dark'));
  await expect(panel).toBeVisible();
  expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const mobileBlue = await panel.getByRole('progressbar', { name: 'Implementation' }).boundingBox();
  const mobileGreen = await panel.getByRole('progressbar', { name: 'Verification' }).boundingBox();
  expect(mobileBlue!.y + mobileBlue!.height).toBeLessThan(mobileGreen!.y);
  await panel.screenshot({ path: test.info().outputPath('readiness-delivery-dark-mobile.png') });
  // U06: the real diagnostic link opens the native AC row, expanded and focused.
  await diagnosis.getByText('Observe the capacity boundary on the delivered revision.').click();
  const reference = diagnosis.getByRole('button', { name: 'ac:capacity', exact: true });
  await reference.focus();
  await page.keyboard.press('Enter');
  const specDialog = page.getByRole('dialog', { name: 'Readiness Spec' });
  const criterion = specDialog.locator('[data-readiness-ref="ac:capacity"]');
  await expect(criterion).toBeFocused();
  await expect(criterion.getByText('Reject reservations above the room capacity.')).toBeVisible();
  await expect(specDialog.getByRole('tab', { name: /^AC(?:\s|$)/ })).toHaveAttribute('aria-selected', 'true');
  await specDialog.screenshot({ path: test.info().outputPath('readiness-native-criterion.png') });
  // The same reference inside Spec Coverage changes the native section in place.
  await specDialog.getByRole('tab', { name: 'Coverage', exact: true }).click();
  await specDialog.getByRole('tab', { name: 'Implementation', exact: true }).click();
  await specDialog.getByRole('button', { name: 'What is missing?', exact: true }).click();
  await specDialog.getByText('Observe the capacity boundary on the delivered revision.').click();
  await specDialog.getByRole('button', { name: 'ac:capacity', exact: true }).click();
  await expect(criterion).toBeFocused();
  await expect(page.getByRole('dialog', { name: 'Readiness Spec' })).toHaveCount(1);
  await specDialog.getByRole('button', { name: 'Close spec' }).click();
  await expect(reference).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(inspect).toBeFocused();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
