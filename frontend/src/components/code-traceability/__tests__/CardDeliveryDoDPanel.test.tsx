import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({
  getDeliveryEvidence: vi.fn(),
  recordDeliveryEvidence: vi.fn(),
  recordCardDeliveryEvidence: vi.fn(),
  getBoard: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));

const CARD = { id: 'task-1', card_type: 'normal', spec_id: 's' };

function projection(): DeliveryEvidenceProjection {
  return {
    board_id: 'b', spec_id: 's', edition: 2, version: 7, status: 'in_progress', allowed: false, complete: true,
    blockers: ['delivery_implementation_missing'], rejected_record_ids: [],
    rows: [],
    implementations: [
      { id: 'card_delivery_abc111', card_id: 'task-1', bindings: [{ obligation_ref: 'fr:fr_3a9f', semantic_sha256: 'a'.repeat(64) }], relative_path: 'src/api.py', symbol: 'record', result_revision: 'r'.repeat(40), current_accepted_execution: true },
    ],
    tests: [],
    candidates: [
      { kind: 'implementation', id: 'execution-1', card_id: 'task-1', card_version: 4, label: 'src/api.py @ rev' },
      { kind: 'implementation', id: 'execution-2', card_id: 'OTHER', card_version: 2, label: 'other card receipt' },
    ],
    records: [],
    per_card: [
      { card_id: 'task-1', title: 'TASK-142 — Re-anchor delivery bindings', card_type: 'normal', status: 'in_progress', satisfied: false,
        obligations: [
          { ref: 'fr:fr_3a9f', title: 'Bindings live on the card ledger', implementation_satisfied: true },
          { ref: 'ac:ac_77ce', title: 'Done is rejected without proof', implementation_satisfied: false },
        ] },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getDeliveryEvidence.mockResolvedValue(projection());
  api.getBoard.mockResolvedValue({ id: 'b', settings: { delivery_evidence_gate: 'blocking' } });
  api.recordCardDeliveryEvidence.mockResolvedValue({ id: 'new', replayed: false });
  api.recordDeliveryEvidence.mockResolvedValue({ id: 'waiver', replayed: false });
});
afterEach(cleanup);

it('loads accumulated impact for this card without changing its delivery verdict', async () => {
  const result = projection();
  result.per_card![0].accumulated_impact = {
    contract_version: 'delivery-net-impact/v1', status: 'needs_reconciliation', claim_only: true,
    history_count: 1, sources: [], issues: [{ source_ref: null, code: 'source_unknown', record_ids: ['progress'] }],
    issue_count: 1, issues_truncated: false,
  };
  api.getDeliveryEvidence.mockResolvedValue(result);
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} />);
  expect(await screen.findByRole('region', { name: 'Accumulated impact claims' })).toBeInTheDocument();
  expect(screen.getByText('Identify the source of this declaration.')).toBeInTheDocument();
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});

it('renders the DoD obligations name-first with per-obligation proof state', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  expect(await screen.findByText('Bindings live on the card ledger')).toBeTruthy();
  expect(screen.getByText('(fr_3a9f)')).toBeTruthy();
  expect(screen.getByText('✓ Implementation')).toBeTruthy();
  expect(screen.getAllByText('Done is rejected without proof').length).toBeGreaterThan(0);
  expect(screen.getByText('◌ No accepted proof')).toBeTruthy();
});

it('shows the gate pill with the unproven count', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  const pill = await screen.findByTestId('dod-gate-pill');
  expect(pill.textContent).toContain('Blocking');
  expect(pill.textContent).toContain('1 of 2 unproven');
});

it('shows the blocked banner naming the unproven obligation', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  const banner = await screen.findByTestId('dod-blocked-banner');
  expect(banner.textContent).toContain('delivery_evidence_incomplete');
  expect(banner.textContent).toContain('Done is rejected without proof');
});

it('records implementation proof through the card-scoped surface with the card CAS', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  fireEvent.click(await screen.findByTestId('dod-record-button'));
  fireEvent.click(screen.getByLabelText('Select ac:ac_77ce'));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'task-1:execution-1' } });
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Execution receipt covers the gate behavior.' } });
  fireEvent.click(screen.getByRole('button', { name: /Record delivery evidence/ }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledTimes(1));
  expect(api.recordCardDeliveryEvidence.mock.calls[0]).toEqual([
    'b', 'task-1', 's',
    expect.objectContaining({
      kind: 'implementation', execution_id: 'execution-1',
      expected_card_version: 4, expected_spec_edition: 2,
      obligation_refs: [],
      bindings: [{ obligation_ref: 'ac:ac_77ce', contribution: 'partial' }],
    }),
  ]);
  expect(api.recordDeliveryEvidence).not.toHaveBeenCalled();
});

it('declares complete separately from partial for two obligations', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  fireEvent.click(await screen.findByTestId('dod-record-button'));
  fireEvent.click(screen.getByLabelText('Select fr:fr_3a9f'));
  fireEvent.click(screen.getByLabelText('Select ac:ac_77ce'));
  fireEvent.click(screen.getByLabelText('Complete contribution for ac:ac_77ce'));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'task-1:execution-1' } });
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Gate complete, other work remains.' } });
  fireEvent.click(screen.getByRole('button', { name: /Record delivery evidence/ }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledTimes(1));
  expect(api.recordCardDeliveryEvidence.mock.calls[0][3].bindings).toEqual([
    { obligation_ref: 'fr:fr_3a9f', contribution: 'partial' },
    { obligation_ref: 'ac:ac_77ce', contribution: 'complete' },
  ]);
});

it('shows a partial receipt after reload without marking implementation complete', async () => {
  const value = projection();
  value.per_card![0].obligations[0].implementation_satisfied = false;
  value.implementations[0].contributions = [{ binding: { obligation_ref: 'fr:fr_3a9f', semantic_sha256: 'a'.repeat(64) }, contribution: 'partial' }];
  api.getDeliveryEvidence.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  expect(await screen.findByText('◌ Partial contribution')).toBeTruthy();
  expect(screen.queryByText('✓ Implementation')).toBeNull();
  expect(screen.getByTestId('dod-gate-pill').textContent).toContain('2 of 2 unproven');
});

it('identifies legacy proof without inventing a complete declaration', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} />);
  expect(await screen.findByText('Legacy record · contribution not declared')).toBeTruthy();
});

it('names exact receipt sets per obligation without a Cartesian assignment', async () => {
  const value = projection();
  value.candidates.push({ kind: 'implementation', id: 'execution-3', card_id: 'task-1', card_version: 4, label: 'src/other.py @ rev' });
  api.getDeliveryEvidence.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  fireEvent.click(await screen.findByTestId('dod-record-button'));
  fireEvent.click(screen.getByLabelText('Select receipts separately for each obligation'));
  fireEvent.click(screen.getByLabelText('Select fr:fr_3a9f'));
  fireEvent.click(screen.getByLabelText('Select ac:ac_77ce'));
  fireEvent.click(screen.getByLabelText('Complete contribution for fr:fr_3a9f'));
  fireEvent.click(screen.getByLabelText('Receipt execution-1 for fr:fr_3a9f'));
  fireEvent.click(screen.getByLabelText('Receipt execution-3 for fr:fr_3a9f'));
  expect(screen.queryByLabelText('Receipt execution-2 for fr:fr_3a9f')).toBeNull();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Complete parser, partial gate.' } });
  expect((screen.getByRole('button', { name: /Record delivery evidence/ }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText('Receipt execution-3 for ac:ac_77ce'));
  fireEvent.click(screen.getByRole('button', { name: /Record delivery evidence/ }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledTimes(1));
  const sent = api.recordCardDeliveryEvidence.mock.calls[0][3];
  expect(sent.execution_id).toBeUndefined();
  expect(sent.expected_card_version).toBe(4);
  expect(sent.bindings).toEqual([
    { obligation_ref: 'fr:fr_3a9f', contribution: 'complete', execution_refs: [{ execution_id: 'execution-1' }, { execution_id: 'execution-3' }] },
    { obligation_ref: 'ac:ac_77ce', contribution: 'partial', execution_refs: [{ execution_id: 'execution-3' }] },
  ]);
});

it('uses server readiness per binding when another receipt in the record is stale', async () => {
  const value = projection();
  value.per_card![0].obligations.forEach(obligation => { obligation.implementation_satisfied = false; });
  value.implementations[0] = { ...value.implementations[0], current_accepted_execution: false,
    admitted_obligation_refs: ['ac:ac_77ce'], ready_obligation_refs: ['ac:ac_77ce'],
    bindings: [{ obligation_ref: 'fr:fr_3a9f', semantic_sha256: 'a'.repeat(64) }, { obligation_ref: 'ac:ac_77ce', semantic_sha256: 'b'.repeat(64) }],
    contributions: [
      { binding: { obligation_ref: 'fr:fr_3a9f', semantic_sha256: 'a'.repeat(64) }, contribution: 'complete' },
      { binding: { obligation_ref: 'ac:ac_77ce', semantic_sha256: 'b'.repeat(64) }, contribution: 'complete' },
    ],
  };
  api.getDeliveryEvidence.mockResolvedValue(value);
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} />);
  const obligations = await screen.findByTestId('dod-obligations');
  expect(within(obligations).getByText('Bindings live on the card ledger')).toBeTruthy();
  const items = obligations.querySelectorAll('li');
  expect(items[0].textContent).toContain('No accepted proof');
  expect(items[1].textContent).toContain('✓ Implementation');
});

it('offers only this card receipts as accepted proof', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canRecord />);
  fireEvent.click(await screen.findByTestId('dod-record-button'));
  const options = screen.getAllByRole('option') as HTMLOptionElement[];
  expect(options.some(o => o.value === 'task-1:execution-1')).toBeTruthy();
  expect(options.some(o => o.value.includes('execution-2'))).toBeFalsy();
});

it('routes a human waiver to the legacy spec surface, never the card ledger', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} canWaiver />);
  fireEvent.click(await screen.findByText('Request Waiver (human)'));
  fireEvent.click(screen.getByLabelText('Select ac:ac_77ce'));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Not testable in this lane.' } });
  fireEvent.click(screen.getByRole('button', { name: /Record waiver/ }));
  await waitFor(() => expect(api.recordDeliveryEvidence).toHaveBeenCalledTimes(1));
  expect(api.recordDeliveryEvidence.mock.calls[0]).toEqual([
    'b', 's', expect.objectContaining({ kind: 'waiver', phase: 'implementation', obligation_refs: ['ac:ac_77ce'] }),
  ]);
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});

it('hides recording controls when the actor lacks the permissions', async () => {
  render(<CardDeliveryDoDPanel boardId="b" card={CARD} />);
  await screen.findByText('Bindings live on the card ledger');
  expect(screen.queryByTestId('dod-record-button')).toBeNull();
  expect(screen.queryByText('Request Waiver (human)')).toBeNull();
});

it('test cards record authenticated scenario runs verifying other cards implementations', async () => {
  const p = projection();
  p.per_card = [{ card_id: 'task-1', title: 'TEST-21', card_type: 'test', status: 'done', satisfied: false, obligations: [] }];
  p.rows = [{ obligation: { title: 'Done is rejected without proof', binding: { obligation_ref: 'ac:ac_77ce', semantic_sha256: 'c'.repeat(64) } }, implementation_ids: [], test_ids: [], implementation_waiver_ids: [], test_waiver_ids: [], implementation_satisfied: true, test_satisfied: false }];
  p.candidates = [{ kind: 'test', id: 'scenario-9', card_id: 'task-1', card_version: 2, label: 'DoD rejection scenario' }];
  api.getDeliveryEvidence.mockResolvedValue(p);
  render(<CardDeliveryDoDPanel boardId="b" card={{ id: 'task-1', card_type: 'test', spec_id: 's' }} canTest />);
  fireEvent.click(await screen.findByTestId('dod-record-button'));
  fireEvent.click(screen.getByLabelText('Select ac:ac_77ce'));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'task-1:scenario-9' } });
  fireEvent.click(screen.getByRole('checkbox', { name: /card_delivery_abc111/ }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Verified passing run.' } });
  fireEvent.click(screen.getByRole('button', { name: /Record delivery evidence/ }));
  await waitFor(() => expect(api.recordCardDeliveryEvidence).toHaveBeenCalledTimes(1));
  expect(api.recordCardDeliveryEvidence.mock.calls[0][3]).toMatchObject({
    kind: 'test', scenario_id: 'scenario-9', implementation_ids: ['card_delivery_abc111'],
  });
});
