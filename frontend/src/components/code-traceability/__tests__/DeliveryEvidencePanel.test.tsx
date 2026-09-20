import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeliveryEvidencePanel } from '../DeliveryEvidencePanel';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({
  getDeliveryEvidence: vi.fn(),
  recordDeliveryEvidence: vi.fn(),
  recordCardDeliveryEvidence: vi.fn(),
  getBoard: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));

function projection(): DeliveryEvidenceProjection {
  return {
    board_id: 'b', spec_id: 's', edition: 2, version: 7, status: 'in_progress', allowed: false, complete: true,
    blockers: ['delivery_test_result_missing'], rejected_record_ids: [],
    rows: [
      { obligation: { title: 'Bindings live on the card ledger', binding: { obligation_ref: 'fr:fr_3a9f', semantic_sha256: 'a'.repeat(64) } }, implementation_ids: ['impl'], test_ids: ['test'], implementation_waiver_ids: [], test_waiver_ids: [], implementation_satisfied: true, test_satisfied: true },
      { obligation: { title: 'Rollup derives from card ledgers', binding: { obligation_ref: 'fr:fr_4b21', semantic_sha256: 'b'.repeat(64) } }, implementation_ids: ['impl'], test_ids: [], implementation_waiver_ids: [], test_waiver_ids: [], implementation_satisfied: true, test_satisfied: false },
    ],
    implementations: [], tests: [], candidates: [], records: [],
    per_card: [
      { card_id: 'task-1', title: 'TASK-142 — Re-anchor delivery bindings', card_type: 'normal', status: 'done', satisfied: true, obligations: [{ ref: 'fr:fr_3a9f', title: 'Bindings live on the card ledger', implementation_satisfied: true }] },
      { card_id: 'task-2', title: 'TASK-143 — Rollup aggregation query', card_type: 'normal', status: 'in_progress', satisfied: false, obligations: [{ ref: 'fr:fr_4b21', title: 'Rollup derives from card ledgers', implementation_satisfied: true }, { ref: 'ac:ac_77ce', title: 'Done is rejected without proof', implementation_satisfied: false }] },
      { card_id: 'test-1', title: 'TEST-21 — DoD rejection scenario', card_type: 'test', status: 'done', satisfied: false, obligations: [] },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getDeliveryEvidence.mockResolvedValue(projection());
  api.getBoard.mockResolvedValue({ id: 'b', settings: { delivery_evidence_gate: 'blocking' } });
});
afterEach(cleanup);

it('renders the informational rollup: verdict, summary and board gate mode', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('Blocked')).toBeTruthy();
  expect(screen.getByText(/test coverage missing on 1 obligation/)).toBeTruthy();
  expect(screen.getByTestId('delivery-gate-mode').textContent).toContain('Gate: Blocking (Board)');
});

it('shows obligations name-first with the stable ref as secondary metadata', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('Bindings live on the card ledger')).toBeTruthy();
  expect(screen.getByText('(fr_3a9f)')).toBeTruthy();
  expect(screen.getByText('Rollup derives from card ledgers')).toBeTruthy();
  expect(screen.getByText('(fr_4b21)')).toBeTruthy();
});

it('marks implementation and test coverage per obligation', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-coverage-table');
  const marks = screen.getAllByText('✓');
  const pending = screen.getAllByText('◌');
  expect(marks.length).toBe(3); // impl+test row 1, impl row 2
  expect(pending.length).toBe(1); // test row 2
});

it('renders per-card grouping with proof counts and DoD statuses', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('TASK-142 — Re-anchor delivery bindings')).toBeTruthy();
  expect(screen.getByText('1 obligation with accepted proof')).toBeTruthy();
  expect(screen.getByText('TASK-143 — Rollup aggregation query')).toBeTruthy();
  expect(screen.getByText('1/2 obligations with proof · 1 unproven')).toBeTruthy();
  expect(screen.getByText('TEST-21 — DoD rejection scenario')).toBeTruthy();
  expect(screen.getByText('Test card · records authenticated outcomes; delivery requires a current passing run')).toBeTruthy();
  expect(screen.getAllByText('Satisfied').length).toBe(1);
  expect(screen.getByText('In progress')).toBeTruthy();
  expect(screen.getByText('Excluded from DoD gate')).toBeTruthy();
});

it('is strictly informational: no recording controls exist on this surface', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-coverage-table');
  expect(screen.queryByRole('checkbox')).toBeNull();
  expect(screen.queryByRole('combobox')).toBeNull();
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(screen.queryByRole('button', { name: /Record association/ })).toBeNull();
  expect(screen.queryByRole('button', { name: /Revoke/ })).toBeNull();
  expect(screen.getByRole('button', { name: /Refresh delivery rollup/ })).toBeTruthy();
});

it('has no Move-to-Done control: the tab is informational only', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-coverage-table');
  expect(screen.queryByTestId('delivery-done-cue')).toBeNull();
  expect(screen.queryByText('Move to Done')).toBeNull();
  expect(screen.getByText(/Waivers are recorded at rollup level/)).toBeTruthy();
});

it('renders the skip override toggle following the Tests-tab pattern', async () => {
  const onSkip = vi.fn();
  render(<DeliveryEvidencePanel boardId="b" specId="s" skipDeliveryEvidence={false} onSkipDeliveryEvidenceChange={onSkip} />);
  const toggle = await screen.findByTestId('delivery-skip-toggle');
  expect(toggle.getAttribute('role')).toBe('switch');
  expect(toggle.getAttribute('aria-checked')).toBe('false');
  expect(screen.getByText('Skip delivery evidence requirement')).toBeTruthy();
  fireEvent.click(toggle);
  expect(onSkip).toHaveBeenCalledWith(true);
});

it('renders the skip toggle at the top of the tab, before the coverage table', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" onSkipDeliveryEvidenceChange={() => {}} />);
  const toggle = await screen.findByTestId('delivery-skip-toggle');
  const table = await screen.findByTestId('delivery-coverage-table');
  expect(toggle.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it('reflects the active skip state on the toggle', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" skipDeliveryEvidence onSkipDeliveryEvidenceChange={() => {}} />);
  const toggle = await screen.findByTestId('delivery-skip-toggle');
  expect(toggle.getAttribute('aria-checked')).toBe('true');
});

it('shows a passive badge when no update handler is available', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" skipDeliveryEvidence />);
  await screen.findByTestId('delivery-coverage-table');
  expect(screen.queryByTestId('delivery-skip-toggle')).toBeNull();
  expect(screen.getByText('Skipped')).toBeTruthy();
});

it('drops the done cue and shows Complete when every obligation is covered', async () => {
  const p = projection();
  p.allowed = true;
  p.rows.forEach(row => { row.test_satisfied = true; });
  api.getDeliveryEvidence.mockResolvedValue(p);
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('Complete')).toBeTruthy();
  expect(screen.getByText(/All 2 obligations satisfied/)).toBeTruthy();
  expect(screen.queryByTestId('delivery-done-cue')).toBeNull();
});

it('reflects the advisory gate mode from board settings', async () => {
  api.getBoard.mockResolvedValue({ id: 'b', settings: { delivery_evidence_gate: 'advisory' } });
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-gate-mode');
  expect(screen.getByTestId('delivery-gate-mode').textContent).toContain('Gate: Advisory (Board)');
});

it('falls back to the blocking gate label when board settings fail to load', async () => {
  api.getBoard.mockRejectedValue(new Error('unavailable'));
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-gate-mode');
  expect(screen.getByTestId('delivery-gate-mode').textContent).toContain('Gate: Blocking (Board)');
});

it('surfaces load errors without faking a verdict', async () => {
  api.getDeliveryEvidence.mockRejectedValue(new Error('Unavailable'));
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByText('Unavailable')).toBeTruthy();
  expect(screen.queryByText('Blocked')).toBeNull();
  expect(screen.queryByText('Complete')).toBeNull();
});

it('refreshes the rollup on demand without any write call', async () => {
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  await screen.findByTestId('delivery-coverage-table');
  fireEvent.click(screen.getByRole('button', { name: /Refresh delivery rollup/ }));
  await waitFor(() => expect(api.getDeliveryEvidence).toHaveBeenCalledTimes(2));
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
  expect(api.recordDeliveryEvidence).not.toHaveBeenCalled();
});
