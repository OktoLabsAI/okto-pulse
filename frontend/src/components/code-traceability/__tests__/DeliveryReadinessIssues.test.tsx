import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReadinessAction, ReadinessDetail, ReadinessIssue } from '@/types/delivery-readiness';
import { DeliveryReadinessPanel } from '../DeliveryReadinessPanel';
import { DeliveryReadinessIssues } from '../DeliveryReadinessIssues';
import { readinessDiagnostic } from '../readinessDiagnostic';
import { readiness } from './readinessFixture';

const api = vi.hoisted(() => ({ getDeliveryReadiness: vi.fn(), getDeliveryReadinessPage: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const action: ReadinessAction = {
  id: 'action-1', kind: 'execute_verification', message_code: 'verification_not_run', message_params: {},
  message: 'Observe the registered capacity scenario.', issue_ids: ['cause-1'], state: 'requires_input',
  depends_on_action_ids: [], responsible_card_ids: ['test-card'], permission: 'spec.tests.execute',
  execution_surface: 'external_agent', pulse_tool: null, arguments: {},
  missing_inputs: ['observed_result'], context_refs: ['scenario-1'], resolves_unit_count: 2,
};
function issue(id = 'cause-1'): ReadinessIssue {
  return { id, code: 'verification_not_run', detail_state: 'available', phase: 'verification', score_impact: 'verification',
    intrinsic_state: 'missing', blocks_proof: true, blocks_target_transition: null, message_code: 'verification_not_run',
    message_params: {}, message: 'Capacity has no current verification.', native_reason_codes: [],
    affected_unit_count: 2, affected_units: [{ unit: 'obligation', ref: 'ac:capacity', semantic_sha256: 'a'.repeat(64), spec_edition: 1, card_id: 'c' }], affected_units_truncated: true,
    criterion_ids: ['capacity'], criterion_count: 1, criterion_ids_truncated: false,
    scenario_ids: ['scenario-1'], scenario_count: 1, scenario_ids_truncated: false,
    implementation_card_ids: ['c'], implementation_card_count: 1, implementation_card_ids_truncated: false,
    test_card_ids: ['test-card'], test_card_count: 1, test_card_ids_truncated: false,
    owner: null, owner_state: 'unassigned',
    expected: [{ outcome: 'passed', revision: 'required-base' }], expected_count: 1, expected_truncated: false,
    observed: [{ text: '<script>not a command</script>', outcome: 'not_run' }], observed_count: 1, observed_truncated: false,
    evidence_refs: [], evidence_count: 0, evidence_refs_truncated: false,
    pinpoints: [], pinpoint_count: 0, pinpoints_truncated: false,
    root_cause_id: id, depends_on_issue_ids: [], action_ids: ['action-1'],
    detail_query: { board_id: 'b', spec_id: 's', card_id: 'c', issue_id: id, view: 'readiness', snapshot_id: 'a'.repeat(64), limit: 20 },
  };
}
function response() {
  return { ...readiness(), issues: [issue()], next_actions: [action],
    page: { total: 2, returned: 1, has_more: true, next_cursor: 'next-page', response_truncated: true, phase_filter: 'all' } };
}
function detail(): ReadinessDetail {
  return { ...readiness(), issue_id: 'cause-1', detail_state: 'available',
    entry_counts: { affected_unit: 0, evidence: 0, pinpoint: 0, action: 1 },
    entries: [{ id: 'entry', type: 'action', section: 'actions', value: action }],
    page: { total: 1, returned: 1, has_more: false, next_cursor: null, response_truncated: false, phase_filter: 'all' } };
}
beforeEach(() => { vi.clearAllMocks(); api.getDeliveryReadiness.mockResolvedValue(response()); api.getDeliveryReadinessPage.mockResolvedValue(detail()); });
afterEach(cleanup);

it('groups actual backend action states and shows escaped factual details without a resolve checkbox', async () => {
  const navigate = vi.fn();
  render(<DeliveryReadinessIssues value={response()} onNavigate={navigate} onSnapshotInvalid={vi.fn()} />);
  expect(screen.getByRole('region', { name: 'Input, human decision, permission or availability' })).toBeVisible();
  expect(screen.queryByRole('region', { name: 'Can act now' })).not.toBeInTheDocument();
  const summary = screen.getByText('Capacity has no current verification.').closest('summary')!;
  expect(summary.closest('details')).not.toHaveAttribute('open');
  fireEvent.click(summary);
  expect(screen.getByText('<script>not a command</script>')).toBeVisible();
  expect(screen.getByText('Expected', { exact: true })).toBeVisible();
  expect(screen.getByText('Observed', { exact: true })).toBeVisible();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  fireEvent.click(screen.getAllByRole('button', { name: 'test-card' })[0]);
  expect(navigate).toHaveBeenCalledWith({ type: 'card', id: 'test-card' });
  fireEvent.click(screen.getByRole('button', { name: 'Open all details and actions' }));
  expect(await screen.findByText('1 of 1 detail entries loaded')).toBeVisible();
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledWith('b', 's',
    { card_id: 'c', phase: undefined, issue_id: 'cause-1', snapshot_id: 'a'.repeat(64), limit: 20 }, expect.any(AbortSignal));
});

it('copy is deterministic, retains partial-page/caveat/permission/input information and known queries', () => {
  const value = response();
  const copied = readinessDiagnostic(value);
  expect(copied).toContain('PARTIAL summary: 1 of 2 causes');
  expect(copied).toContain('every mutation must reauthorize');
  expect(copied).toContain('requires_input; external_agent');
  expect(copied).toContain('observed_result');
  expect(copied).toContain('"cursor":"next-page"');
  expect(copied).toContain('Detail query:');
  expect(readinessDiagnostic(structuredClone(value))).toBe(copied);
});

it('focuses the diagnosis heading, loads another page without changing metrics, and returns focus on hide', async () => {
  const next = { ...response(), issues: [issue('cause-2')], page: { ...response().page, returned: 1, has_more: false, next_cursor: null } };
  api.getDeliveryReadinessPage.mockResolvedValue(next);
  render(<DeliveryReadinessPanel boardId="b" specId="s" cardId="c" />);
  await screen.findByRole('progressbar', { name: 'Verification' });
  const open = screen.getByRole('button', { name: 'What is missing?' }); open.focus(); fireEvent.click(open);
  expect(screen.getByRole('heading', { name: 'What is missing?' })).toHaveFocus();
  fireEvent.click(screen.getByRole('button', { name: 'Load more causes' }));
  await waitFor(() => expect(screen.getAllByText('Capacity has no current verification.')).toHaveLength(2));
  expect(screen.getByRole('progressbar', { name: 'Verification' })).toHaveAttribute('aria-valuenow', '75');
  expect(screen.queryByRole('button', { name: 'Load more causes' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Hide diagnosis' }));
  expect(open).toHaveFocus();
});

it('a changed snapshot during paging removes the old percentages and prevents mixing generations', async () => {
  api.getDeliveryReadinessPage.mockResolvedValue({ ...response(), snapshot_id: 'b'.repeat(64) });
  render(<DeliveryReadinessPanel boardId="b" specId="s" cardId="c" focusDiagnosis />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more causes' }));
  expect(await screen.findByText(/snapshot changed/)).toBeVisible();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Delivery diagnosis' })).not.toBeInTheDocument();
});

it('late details cannot replace a different Card diagnosis', async () => {
  let resolve!: (value: ReadinessDetail) => void;
  api.getDeliveryReadinessPage.mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = render(<DeliveryReadinessIssues value={response()} onSnapshotInvalid={vi.fn()} />);
  fireEvent.click(screen.getByText('Capacity has no current verification.').closest('summary')!);
  fireEvent.click(screen.getByRole('button', { name: 'Open all details and actions' }));
  const signal = api.getDeliveryReadinessPage.mock.calls[0][3];
  view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(detail()));
  expect(screen.queryByText('1 of 1 detail entries loaded')).not.toBeInTheDocument();
});
