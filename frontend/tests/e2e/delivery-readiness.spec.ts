import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';
import { readiness, task, metric } from '../../src/components/code-traceability/__tests__/readinessFixture';

test('P08/U01/U04/U09: native board and Delivery show separate accessible metrics', async ({ page }) => {
  const errors: string[] = [], writes: string[] = [];
  let summaryError = false;
  let summaryHold: Promise<void> | undefined;
  page.on('pageerror', error => errors.push(error.message));
  const card = { ...task, title: 'Readiness capacity verification with a long descriptive requirement name',
    status: 'done', screen_mockups: [], knowledge_bases: [], attachments: [], qa_items: [], comments: [], architecture_designs: [] };
  const board = { id: 'b', name: 'Readiness fixture', owner_id: 'local-user',
    created_at: task.created_at, updated_at: task.updated_at, settings: { delivery_evidence_gate: 'blocking' }, agents: [] };
  const spec = { id: 's', board_id: 'b', title: 'Readiness Spec', status: 'in_progress', version: 1, edition: 1,
    created_at: task.created_at, updated_at: task.updated_at, created_by: 'local-user',
    functional_requirements: [], technical_requirements: [], acceptance_criteria: [{ id: 'capacity', title: 'Capacity limit',
      text: 'Reject reservations above the room capacity.', status: 'active' }], test_scenarios: [{ id: 'capacity-boundary', title: 'Capacity boundary',
      scenario_type: 'unit', verification_method: 'automated_test', status: 'failed', given: 'A full room', when: 'An extra reservation arrives',
      then: 'The reservation is rejected', notes: null, linked_criteria: ['capacity'], linked_task_ids: [], evidence: {
        execution_receipt: 'receipt-capacity', evidence_class: 'run_log', test_run_id: 'fixture-run', output_snippet: 'Expected rejection; observed acceptance.',
      } }], business_rules: [],
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
    evidence_refs: [{ kind: 'record', id: 'record-capacity', source_ref: null, revision: null },
      { kind: 'inspection', id: 'review-capacity', source_ref: null, revision: null },
      { kind: 'receipt', id: 'receipt-capacity', source_ref: null, revision: null },
      { kind: 'exception', id: 'waiver-capacity', source_ref: null, revision: null }], evidence_count: 4, evidence_refs_truncated: false,
    pinpoints: [{ kind: 'target', entity_type: 'card', entity_id: 'c', target_id: 'target-capacity', execution_id: 'execution-capacity',
      record_id: null, observation_id: null, source_ref: 'mock-source', revision: 'revision-1', relative_path: 'capacity.py',
      qualified_symbol: null, line_start: null, line_end: null, precision: 'target', origin: 'execution_receipt' }], pinpoint_count: 1, pinpoints_truncated: false,
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
    if (path.endsWith('/delivery-readiness-summary')) {
      if (summaryHold) await summaryHold;
      if (summaryError) return route.fulfill({ status: 503, json: { detail: 'Summary temporarily unavailable' } });
      return route.fulfill({ json: {
      contract_version: 'delivery-readiness/v1', metrics_version: 2, delivery_readiness: specReadiness,
      cards: [{ card_id: 'c', delivery_readiness: current }] } });
    }
    if (path.endsWith('/delivery-evidence')) return route.fulfill({ json: url.searchParams.get('view') === 'readiness'
      ? (url.searchParams.get('card_id') ? current : specReadiness)
      : url.searchParams.get('view') === 'ledger'
        ? { board_id: 'b', spec_id: 's', card_id: 'c', edition: 1, current_edition: 1, historical: false, total: 1, next_cursor: null,
          items: [{ id: 'record-capacity', kind: 'implementation', actor_id: 'fixture-author', created_at: task.created_at, revoked: false,
            summary: 'Capacity guard implemented in the observed revision.', text_truncated: false, obligation_refs: ['ac:capacity'], obligations_truncated: false,
            currentness: 'not_evaluated', payload: { execution_id: 'execution-capacity' } }] }
      : { board_id: 'b', spec_id: 's', edition: 1, version: 1, complete: true, allowed: false,
        records: [{ id: 'record-capacity', kind: 'implementation', actor_id: 'fixture-author', created_at: task.created_at,
          revoked: false, payload: { card_id: 'c', justification: 'Capacity guard implemented in the observed revision.' } },
          { id: 'waiver-capacity', kind: 'waiver', actor_id: 'fixture-reviewer', created_at: task.created_at, revoked: true,
            disposition: 'risk_accepted', payload: { justification: 'Temporary mock exception, subsequently revoked.', phase: 'test',
              bindings: [{ obligation_ref: 'ac:capacity', semantic_sha256: 'a'.repeat(64) }] } }],
        candidates: [], implementations: [{ id: 'record-capacity', card_id: 'c', actor_id: 'fixture-author',
          explanation: 'Capacity guard implemented in the observed revision.', bindings: [], contributions: [],
          admitted_obligation_refs: ['ac:capacity'], ready_obligation_refs: ['ac:capacity'], executions: [{
            execution_id: 'execution-capacity', target_id: 'target-capacity', source_ref: 'mock-source',
            relative_path: 'capacity.py', result_revision: 'revision-1', current_accepted_execution: true,
          }] }], rows: [{ obligation: { title: 'Capacity limit', binding: { obligation_ref: 'ac:capacity', semantic_sha256: 'a'.repeat(64) } },
          implementation_ids: ['record-capacity'], test_ids: ['test-capacity'], implementation_waiver_ids: [], test_waiver_ids: [],
          implementation_satisfied: true, test_satisfied: false }], tests: [{ id: 'test-capacity', card_id: 'test-card',
          actor_id: 'fixture-verifier', scenario_id: 'capacity-boundary', result: 'failed', current_verified_run: true }],
        rejected_record_ids: [], per_card: [] } });
    if (path.endsWith('/decision-reviews')) return route.fulfill({ json: {
      board_id: 'b', spec_id: 's', edition: 1, version: 1, review_revision: 'review-revision', complete: true,
      observation_authority: 'authenticated_reviewer_declaration', decisions: [],
      history: [{ id: 'review-capacity', edition: 1, actor_id: 'fixture-reviewer', actor_kind: 'human',
        created_at: task.created_at, revoked: false, observations: ['capacity-scope', 'mock-only'].map(decision_id => ({
          decision_id, expected_scope_sha256: 'd'.repeat(64), result: 'passed', expected: 'Only the agreed mock scope.',
          observed: `Inspected ${decision_id} against the versioned manifest.`, sources: [{ reference: 'spec:s', revision: 'edition:1', sha256: 'e'.repeat(64) }],
          reconciles: [], separation: { warning: false, conflicts: [] },
        })) }],
    } });
    if (path.endsWith('/code-traceability-projection')) return route.fulfill({ json: {
      subject_type: 'card', subject_id: 'c', subject_version: 1, profile: 'detail', context_scope: 'default',
      targets: [{ id: 'target-capacity', card_id: 'c', source_ref: 'mock-source', selector_kind: 'symbol', relative_path_hint: 'capacity.py',
        qualified_symbol: 'reserve_room', role: 'modify', required: true, lifecycle_status: 'active', revision: 1, current_resolution_id: null }],
      executions: [{ id: 'execution-capacity', card_id: 'c', target_id: 'target-capacity', target_revision: 1,
        result_investigation_receipt_id: 'investigation-capacity', disposition: 'touched', source_ref: 'mock-source',
        actual_relative_path: 'capacity.py', actual_qualified_symbol: 'reserve_room', justification: 'Capacity boundary implemented.' }],
      resolutions: [], resolution_freshness: {}, gate_readiness: { resolution_freshness: {} }, overlaps: [], waivers: [], heads: [],
    } });
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
  await expect(verification).toHaveAttribute('aria-valuetext', '75%, 6 of 8 applicable obligations');
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
  const causeSummary = diagnosis.locator('summary').filter({ hasText: 'Observe the capacity boundary on the delivered revision.' });
  await causeSummary.focus();
  const refreshed = page.waitForResponse(response => response.url().includes('view=readiness') && response.url().includes('card_id=c'));
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await refreshed;
  await expect(causeSummary).toBeFocused();
  await expect(diagnosis.getByText('Expected', { exact: true })).toBeVisible();
  await diagnosis.screenshot({ path: test.info().outputPath('readiness-diagnosis.png') });
  const panel = page.getByRole('region', { name: 'Delivery readiness', exact: true }).last();
  await expect(panel.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  await expect(panel.getByText('Status: done')).toBeVisible();
  await expect(panel.getByText('Historical completion · current qualification pending')).toBeVisible();
  await panel.getByRole('button', { name: 'Refresh', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(panel.getByRole('progressbar', { name: 'Implementation' })).toHaveAttribute('aria-valuenow', '100');
  await panel.screenshot({ path: test.info().outputPath('readiness-delivery-light.png') });
  const accessibility = async () => {
    const result = await new AxeBuilder({ page }).include('section[aria-label="Delivery readiness"]')
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
    expect(result.violations).toEqual([]);
  };
  await accessibility();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' });
  await page.evaluate(() => document.documentElement.classList.add('dark'));
  await expect(panel).toBeVisible();
  expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const mobileBlue = await panel.getByRole('progressbar', { name: 'Implementation' }).boundingBox();
  const mobileGreen = await panel.getByRole('progressbar', { name: 'Verification' }).boundingBox();
  expect(mobileBlue!.y + mobileBlue!.height).toBeLessThan(mobileGreen!.y);
  await panel.screenshot({ path: test.info().outputPath('readiness-delivery-dark-mobile.png') });
  await accessibility();
  const accessibilityTree = await panel.ariaSnapshot();
  for (const entry of ['heading "Delivery readiness"', 'progressbar "Implementation"',
    'progressbar "Verification"', 'Historical completion · current qualification pending',
    'button "What is missing?"']) expect(accessibilityTree).toContain(entry);
  await test.info().attach('readiness-accessibility-tree', { body: accessibilityTree, contentType: 'text/plain' });
  // U06: the real diagnostic link opens the native AC row, expanded and focused.
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
  const inspectionLink = diagnosis.getByRole('button', { name: 'review-capacity', exact: true });
  await inspectionLink.focus();
  await page.keyboard.press('Enter');
  const inspectionRecord = specDialog.getByRole('article', { name: 'Decision inspection record' });
  await expect(inspectionRecord).toBeFocused();
  await inspectionRecord.getByText('capacity-scope · Passed', { exact: true }).click();
  await inspectionRecord.getByText('mock-only · Passed', { exact: true }).click();
  await expect(inspectionRecord.getByText('Inspected capacity-scope against the versioned manifest.')).toBeVisible();
  await expect(inspectionRecord.getByText('Inspected mock-only against the versioned manifest.')).toBeVisible();
  expect(await inspectionRecord.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await page.setViewportSize({ width: 1280, height: 1200 });
  await inspectionRecord.scrollIntoViewIfNeeded();
  await inspectionRecord.screenshot({ path: test.info().outputPath('readiness-native-inspection.png') });
  await specDialog.getByRole('button', { name: 'Close spec' }).click();
  await expect(inspectionLink).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  const receiptLink = diagnosis.getByRole('button', { name: 'receipt-capacity', exact: true });
  await receiptLink.focus();
  await page.keyboard.press('Enter');
  const receiptDetail = specDialog.getByRole('article', { name: 'Test receipt detail' });
  await expect(receiptDetail).toBeFocused();
  await expect(receiptDetail.getByText('Expected rejection; observed acceptance.')).toBeVisible();
  expect(await receiptDetail.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await receiptDetail.screenshot({ path: test.info().outputPath('readiness-native-test-receipt.png') });
  await specDialog.getByRole('button', { name: 'Close spec' }).click();
  await expect(receiptLink).toBeFocused();
  const exceptionLink = diagnosis.getByRole('button', { name: 'waiver-capacity', exact: true });
  await exceptionLink.focus();
  await page.keyboard.press('Enter');
  const exceptionDetail = specDialog.getByRole('article', { name: 'Delivery exception detail' });
  await expect(exceptionDetail).toBeFocused();
  await expect(exceptionDetail.getByText('Delivery exception · Revoked')).toBeVisible();
  await expect(specDialog.getByRole('tab', { name: 'Implementation', exact: true })).toHaveAttribute('aria-selected', 'true');
  expect(await exceptionDetail.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await exceptionDetail.screenshot({ path: test.info().outputPath('readiness-native-exception.png') });
  const obligation = specDialog.locator('details').filter({ has: page.locator('summary', { hasText: /^Capacity limit$/ }) });
  await obligation.locator('summary').click();
  await expect(obligation.getByText('Implementation author: fixture-author')).toBeVisible();
  await expect(obligation.getByText('Verification author: fixture-verifier')).toBeVisible();
  await expect(obligation.getByText(/capacity.py · mock-source @ revision-1/)).toBeVisible();
  await expect(obligation.getByText('Result: failed · Current verified run')).toBeVisible();
  expect(await obligation.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await obligation.screenshot({ path: test.info().outputPath('readiness-proof-provenance.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await specDialog.getByRole('button', { name: 'Close spec' }).click();
  await expect(exceptionLink).toBeFocused();
  await diagnosis.getByRole('button', { name: 'record-capacity', exact: true }).click();
  const record = page.getByRole('article', { name: 'Delivery record detail' });
  await expect(record).toBeFocused();
  await expect(record.getByText('Capacity guard implemented in the observed revision.')).toBeVisible();
  await expect(page.locator('.modal-content')).toHaveCount(1);
  await record.screenshot({ path: test.info().outputPath('readiness-native-record.png') });
  await diagnosis.getByRole('button', { name: 'execution-capacity', exact: true }).click();
  const execution = page.locator('[data-readiness-ref="execution:execution-capacity"]');
  await expect(execution).toBeFocused();
  await expect(execution.getByText('Capacity boundary implemented.')).toBeVisible();
  await expect(page.locator('.modal-content')).toHaveCount(1);
  await execution.screenshot({ path: test.info().outputPath('readiness-native-execution.png') });
  await page.keyboard.press('Escape');
  await expect(inspect).toBeFocused();
  // U09: the compact area keeps its height in loading, N/A, unknown and network error.
  await page.getByRole('button', { name: 'Hide sidebar', exact: true }).click();
  await cover.scrollIntoViewIfNeeded();
  const compact = cover.getByRole('region', { name: 'Delivery readiness', exact: true });
  const height = (await compact.boundingBox())!.height;
  let release!: () => void;
  summaryHold = new Promise<void>(resolve => { release = resolve; });
  const loadingRequest = page.waitForRequest(request => request.url().includes('/delivery-readiness-summary'));
  await page.getByRole('button', { name: 'Refresh board', exact: true }).click();
  await loadingRequest;
  try {
    await expect(compact).toHaveAttribute('aria-busy', 'true');
    expect((await compact.boundingBox())!.height).toBe(height);
  } finally { summaryHold = undefined; release(); }
  await expect(compact.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  for (const key of ['implementation', 'verification'] as const) current[key] = {
    ...metric(0, 0), status: 'not_applicable', percent: null, total: 0, applicable: 0, unresolved: 0,
  };
  current.delivery_state = 'not_applicable'; current.summary.message_code = 'not_applicable';
  current.summary.unresolved_units = 0; current.summary.root_causes = 0;
  await page.getByRole('button', { name: 'Refresh board', exact: true }).click();
  await expect(compact.getByRole('button', { name: 'View scope' })).toBeVisible();
  await expect(compact.getByRole('progressbar')).toHaveCount(0);
  expect((await compact.boundingBox())!.height).toBe(height);
  for (const key of ['implementation', 'verification'] as const) current[key] = {
    ...current[key], status: 'unknown', percent: null, total: null, applicable: null, unresolved: null,
  };
  current.delivery_state = 'unknown';
  await page.getByRole('button', { name: 'Refresh board', exact: true }).click();
  await expect(compact.getByText('Unable to evaluate', { exact: true }).first()).toBeVisible();
  expect((await compact.boundingBox())!.height).toBe(height);
  summaryError = true;
  await page.getByRole('button', { name: 'Refresh board', exact: true }).click();
  await expect(compact.getByText('Current readiness is unavailable. Open the diagnosis to retry.')).toBeVisible();
  expect((await compact.boundingBox())!.height).toBe(height);
  expect(await compact.evaluate(element => element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1)).toBe(true);
  // Repeated refreshes temporarily stack success notifications over the Card.
  // Observe their native dismissal before checking unobscured mobile content.
  await expect(page.getByText('Board refreshed!', { exact: true })).toHaveCount(0, { timeout: 6000 });
  // Each metric must remain reachable through its native scroll area.
  for (const label of ['Implementation', 'Verification']) {
    const metricLabel = compact.getByText(label, { exact: true });
    await metricLabel.scrollIntoViewIfNeeded();
    expect(await metricLabel.evaluate(element => {
      const box = element.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return hit === element || element.contains(hit);
    })).toBe(true);
  }
  await accessibility();
  await compact.evaluate(element => element.scrollIntoView({ block: 'center', inline: 'center' }));
  await compact.screenshot({ path: test.info().outputPath('readiness-compact-error.png') });
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
