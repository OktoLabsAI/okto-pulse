import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { Spec } from '@/types';
import type { ReadinessAction, ReadinessIssue, ReadinessResponse } from '@/types/delivery-readiness';
import { TaskDeliveryDiagnosis } from '../TaskDeliveryDiagnosis';
import { readiness } from './readinessFixture';

const api = vi.hoisted(() => ({ getDeliveryReadinessPage: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const spec = { id: 's', edition: 1, version: 1,
  functional_requirements: [{ id: 'login', text: 'Sign in with a valid account' }],
  acceptance_criteria: [{ id: 'expired', text: 'Reject expired credentials' }],
  test_scenarios: [{ id: 'scenario-login', title: 'Verify sign-in' }],
  cards: [{ id: 'implementation-task', title: 'Build sign-in' }] } as Spec;
const props = { boardId: 'b', specId: 's', cardId: 'c', active: true, revision: '1', generationId: 'generation-1', spec };
function issue(id = 'cause-1', overrides: Partial<ReadinessIssue> = {}): ReadinessIssue {
  return { id, code: 'verification_not_run', detail_state: 'available', phase: 'verification', score_impact: 'verification',
    intrinsic_state: 'missing', blocks_proof: true, blocks_target_transition: null, message_code: 'verification_not_run',
    message_params: {}, message: 'Native verification detail.', native_reason_codes: [],
    affected_unit_count: 1, affected_units: [{ unit: 'obligation', ref: 'fr:login', semantic_sha256: 'a'.repeat(64), spec_edition: 1, card_id: 'c' }], affected_units_truncated: false,
    criterion_ids: [], criterion_count: 0, criterion_ids_truncated: false,
    scenario_ids: [], scenario_count: 0, scenario_ids_truncated: false,
    implementation_card_ids: [], implementation_card_count: 0, implementation_card_ids_truncated: false,
    test_card_ids: [], test_card_count: 0, test_card_ids_truncated: false,
    owner: null, owner_state: 'unassigned',
    expected: [], expected_count: 0, expected_truncated: false, observed: [], observed_count: 0, observed_truncated: false,
    evidence_refs: [], evidence_count: 0, evidence_refs_truncated: false, pinpoints: [], pinpoint_count: 0, pinpoints_truncated: false,
    root_cause_id: id, depends_on_issue_ids: [], action_ids: ['action-1'],
    detail_query: { board_id: 'b', spec_id: 's', card_id: 'c', issue_id: id, view: 'readiness', snapshot_id: 'a'.repeat(64), limit: 20 }, ...overrides };
}
function response(issues = [issue()]): ReadinessResponse {
  const result = readiness();
  return { ...result, projection: { projection_version: 1, generation_id: 'generation-1', snapshot_id: result.snapshot_id, freshness: 'current' },
    issues, page: { ...result.page, total: issues.length, returned: issues.length } };
}
const action: ReadinessAction = { id: 'action-1', kind: 'execute_verification', message_code: 'verification_not_run', message_params: {},
  message: 'Native action detail.', issue_ids: ['cause-1'], state: 'requires_input', depends_on_action_ids: [],
  responsible_card_ids: ['implementation-task'], permission: 'spec.tests.execute', execution_surface: 'external_agent',
  pulse_tool: null, arguments: {}, missing_inputs: ['observed_result'], context_refs: [], resolves_unit_count: 1 };
beforeEach(() => { vi.resetAllMocks(); api.getDeliveryReadinessPage.mockResolvedValue(response()); });
afterEach(cleanup);

it('loads once when Details opens and caches the same page across close/reopen', async () => {
  const view = render(<TaskDeliveryDiagnosis {...props} active={false} />);
  expect(api.getDeliveryReadinessPage).not.toHaveBeenCalled();
  view.rerender(<TaskDeliveryDiagnosis {...props} />);
  const table = await screen.findByRole('table', { name: 'Requirements and verification' });
  expect(within(table).getAllByRole('columnheader').map(cell => cell.textContent)).toEqual([
    'Requirement / Objective', 'Affected stage', 'What needs attention', 'Next step']);
  expect(within(table).getByRole('row', { name: /Sign in with a valid account/ })).toHaveTextContent('VerificationThere is no valid execution');
  expect(screen.queryByRole('button', { name: 'Verification and pending work' })).not.toBeInTheDocument();
  expect(api.getDeliveryReadinessPage.mock.calls[0][2]).toEqual({ card_id: 'c', limit: 20 });
  view.rerender(<TaskDeliveryDiagnosis {...props} active={false} />);
  view.rerender(<TaskDeliveryDiagnosis {...props} />);
  expect(await screen.findByRole('table')).toBeInTheDocument();
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  view.rerender(<TaskDeliveryDiagnosis {...props} revision="2" />);
  expect(await screen.findByRole('table')).toBeInTheDocument();
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(2);
});

it('names the different implementation prerequisites that affect the same test scenario', async () => {
  const scenario = { ...issue().affected_units[0], unit: 'scenario' as const, ref: 'scenario-login' };
  api.getDeliveryReadinessPage.mockResolvedValue(response([
    issue('first', { code: 'implementation_missing', phase: 'implementation', affected_units: [scenario], expected: [{ obligation_ref: 'fr:login' }] }),
    issue('second', { code: 'implementation_missing', phase: 'implementation', affected_units: [scenario], expected: [{ obligation_ref: 'ac:expired' }] }),
  ]));
  render(<TaskDeliveryDiagnosis {...props} />);
  const rows = within(await screen.findByRole('table')).getAllByRole('row', { name: /Verify sign-in/ });
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent('Verification — implementation prerequisite');
  expect(rows[0]).toHaveTextContent('Related requirements:Sign in with a valid account');
  expect(rows[1]).toHaveTextContent('Related requirements:Reject expired credentials');
  expect(rows[0]).not.toHaveTextContent('Reject expired credentials');
});

it('separates task-wide and unknown impacts and does not invent associations from messages', async () => {
  api.getDeliveryReadinessPage.mockResolvedValue(response([
    issue('global', { code: 'lifecycle_gate_pending', phase: 'lifecycle', affected_units: [], affected_unit_count: 0 }),
    issue('unknown', { code: 'future_reason', phase: 'implementation', affected_units: [], affected_unit_count: 0, message: 'fr:login failed' }),
  ]));
  render(<TaskDeliveryDiagnosis {...props} />);
  const table = await screen.findByRole('table', { name: 'Task-wide and unassigned checks' });
  expect(within(table).getByRole('row', { name: /Task-wide check/ })).toHaveTextContent('A task completion check is still blocking delivery.');
  expect(within(table).getByRole('row', { name: /Affected requirement not identified/ })).toHaveTextContent('An additional delivery check needs attention.');
  expect(screen.queryByText('Sign in with a valid account')).not.toBeInTheDocument();
  expect(screen.queryByRole('table', { name: 'Requirements and verification' })).not.toBeInTheDocument();
});

it('names the failed verification check beside the requirement it impacts', async () => {
  const value = response([issue('failed', {
    code: 'verification_failed', scenario_ids: ['scenario-login'], expected: [{ scenario_id: 'scenario-login' }] })]);
  // Actual Core failure diagnostics lead with implement_contribution even
  // when implementation is complete; that means repair, not missing evidence.
  value.next_actions = [{ ...action, kind: 'implement_contribution', issue_ids: ['failed'], state: 'ready' }];
  api.getDeliveryReadinessPage.mockResolvedValue(value);
  render(<TaskDeliveryDiagnosis {...props} />);
  const row = await screen.findByRole('row', { name: /Sign in with a valid account/ });
  expect(row).toHaveTextContent('The check for this delivered version failed.');
  expect(row).toHaveTextContent('Verification checks:Verify sign-in');
  expect(within(row).getAllByText('Verify sign-in')).toHaveLength(1);
  expect(row).toHaveTextContent('Review the failed check, fix its cause, then verify the corrected version and record the result.');
  expect(row).not.toHaveTextContent('complete the work and record its implementation evidence');
  expect(row).toHaveTextContent('Responsible task: Build sign-in');
});

it('explains an unclear applicable result without claiming that independent checks disagree', async () => {
  const value = response([issue('ambiguous', { code: 'verification_conflict', intrinsic_state: 'conflict',
    scenario_ids: ['scenario-login'], action_ids: ['action-1'] })]);
  value.next_actions = [{ ...action, kind: 'resolve_verification_conflict', issue_ids: ['ambiguous'], state: 'ready' }];
  api.getDeliveryReadinessPage.mockResolvedValue(value);
  render(<TaskDeliveryDiagnosis {...props} />);
  const row = await screen.findByRole('row', { name: /Sign in with a valid account/ });
  expect(row).toHaveTextContent('Verification cannot be confirmed because the applicable result is unclear.');
  expect(row).toHaveTextContent('Ask the responsible agent to identify the result for this delivered version and resolve the ambiguity.');
  expect(row).toHaveTextContent('Verify sign-in');
  expect(row).not.toHaveTextContent('results disagree');
  expect(row).not.toHaveTextContent('run the required check');
});

it('still asks for implementation evidence when implementation is missing', async () => {
  const value = response([issue('missing', { code: 'implementation_missing', phase: 'implementation' })]);
  value.next_actions = [{ ...action, kind: 'implement_contribution', issue_ids: ['missing'], state: 'ready' }];
  api.getDeliveryReadinessPage.mockResolvedValue(value);
  render(<TaskDeliveryDiagnosis {...props} />);
  const row = await screen.findByRole('row', { name: /Sign in with a valid account/ });
  expect(row).toHaveTextContent('The responsible agent needs to complete the work and record its implementation evidence.');
  expect(row).not.toHaveTextContent('Review the failed check');
});

it('shows every provided affected requirement and marks truncated impacts explicitly', async () => {
  const first = issue();
  first.affected_units.push({ ...first.affected_units[0], ref: 'ac:expired' });
  first.affected_unit_count = 12; first.affected_units_truncated = true;
  api.getDeliveryReadinessPage.mockResolvedValue(response([first]));
  render(<TaskDeliveryDiagnosis {...props} />);
  const row = await screen.findByRole('row', { name: /Sign in with a valid account/ });
  expect(row).toHaveTextContent('Reject expired credentials');
  expect(row).toHaveTextContent('Showing 2 of 12 impacted items.');
});

it('keeps action permissions and responsible task visible and joins only authoritative action links', async () => {
  api.getDeliveryReadinessPage.mockResolvedValue({ ...response(), next_actions: [{ ...action, state: 'forbidden' },
    { ...action, id: 'unrelated', issue_ids: [], kind: 'complete_lifecycle' }] });
  render(<TaskDeliveryDiagnosis {...props} />);
  const row = await screen.findByRole('row', { name: /Sign in with a valid account/ });
  expect(row).toHaveTextContent('Permission is required.');
  expect(row).toHaveTextContent('Responsible task: Build sign-in');
  expect(row).not.toHaveTextContent('use the available task status transition');
});

it('does not use requirement titles from another spec edition', async () => {
  render(<TaskDeliveryDiagnosis {...props} spec={{ ...spec, edition: 2 }} />);
  expect(await screen.findByRole('row', { name: /Functional requirement \(login\)/ })).toBeInTheDocument();
  expect(screen.queryByText('Sign in with a valid account')).not.toBeInTheDocument();
});

it('loads evidence only on explicit request and removes old facts after revoked access', async () => {
  render(<TaskDeliveryDiagnosis {...props} />);
  fireEvent.click(await screen.findByText('Evidence and technical details'));
  expect(await screen.findByText('Native verification detail.')).toBeVisible();
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  api.getDeliveryReadinessPage.mockRejectedValue({ status: 403, message: 'Access denied' });
  fireEvent.click(screen.getByRole('button', { name: 'Open all details and actions' }));
  await screen.findByRole('alert');
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it('retries failed reads only on explicit retry or modal refresh', async () => {
  api.getDeliveryReadinessPage.mockRejectedValue(new Error('Network unavailable'));
  const view = render(<TaskDeliveryDiagnosis {...props} />);
  await screen.findByRole('alert');
  view.rerender(<TaskDeliveryDiagnosis {...props} active={false} />);
  view.rerender(<TaskDeliveryDiagnosis {...props} />);
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
  api.getDeliveryReadinessPage.mockResolvedValue(response());
  fireEvent.click(screen.getByRole('button', { name: 'Retry delivery checks' }));
  await screen.findByRole('table');
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(2);
});

it('aborts an inactive read and ignores its late result', async () => {
  let resolve!: (value: ReadinessResponse) => void;
  api.getDeliveryReadinessPage.mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = render(<TaskDeliveryDiagnosis {...props} />);
  const signal = api.getDeliveryReadinessPage.mock.calls[0][3];
  view.rerender(<TaskDeliveryDiagnosis {...props} active={false} />);
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(response()));
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it.each(['generation', 'scope', 'freshness'])('rejects mismatched %s without showing stale checks', async kind => {
  const value = response();
  if (kind === 'generation') value.projection!.generation_id = 'another';
  if (kind === 'scope') value.scope.card_id = 'another';
  if (kind === 'freshness') value.projection!.freshness = 'updating';
  api.getDeliveryReadinessPage.mockResolvedValue(value);
  render(<TaskDeliveryDiagnosis {...props} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Use Refresh card');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it('paginates on demand, rejects changed snapshots and restarts at the first page on refresh', async () => {
  const first = response(); first.page = { ...first.page, total: 21, has_more: true, next_cursor: 'next' };
  const second = response([issue('second')]);
  second.snapshot_id = 'b'.repeat(64); second.issues[0].detail_query.snapshot_id = second.snapshot_id;
  api.getDeliveryReadinessPage.mockResolvedValueOnce(first).mockResolvedValueOnce(second).mockResolvedValue(first);
  const view = render(<TaskDeliveryDiagnosis {...props} />);
  expect(await screen.findByText('Showing 1 of 21 checks. More checks are available on the next page.')).toBeVisible();
  fireEvent.click(await screen.findByRole('button', { name: 'Next checks' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Use Refresh card');
  expect(api.getDeliveryReadinessPage.mock.calls[1][2]).toEqual({ card_id: 'c', limit: 20, cursor: 'next' });
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  view.rerender(<TaskDeliveryDiagnosis {...props} revision="2" />);
  await screen.findByRole('table');
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(3);
  expect(api.getDeliveryReadinessPage.mock.calls[2][2]).toEqual({ card_id: 'c', limit: 20 });
});

it('points to technical details when evidence previews are truncated without another checks page', async () => {
  const value = response([issue('large', { expected: [], expected_count: 10, expected_truncated: true })]);
  value.page = { ...value.page, response_truncated: true };
  api.getDeliveryReadinessPage.mockResolvedValue(value);
  render(<TaskDeliveryDiagnosis {...props} />);
  expect(await screen.findByText("Showing 1 of 1 checks. Open a check's technical details to see the information omitted from this summary.")).toBeVisible();
  expect(screen.queryByText(/More checks are available on the next page/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Next checks' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('Evidence and technical details'));
  expect(await screen.findByRole('button', { name: 'Open all details and actions' })).toBeVisible();
  expect(api.getDeliveryReadinessPage).toHaveBeenCalledTimes(1);
});

it.each([true, false])('does not treat an empty assessment as delivery approval (complete=%s)', async complete => {
  api.getDeliveryReadinessPage.mockResolvedValue({ ...response([]), evaluation_complete: complete });
  render(<TaskDeliveryDiagnosis {...props} />);
  expect(await screen.findByText(complete ? /delivery status above still determines/ : /Some checks could not be assessed/)).toBeVisible();
  expect(screen.queryByText('Delivered')).not.toBeInTheDocument();
});
