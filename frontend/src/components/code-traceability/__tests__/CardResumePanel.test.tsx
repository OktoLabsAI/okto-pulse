import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { CardResumePanel } from '../CardResumePanel';

const api = vi.hoisted(() => ({ getCardDeliveryResume: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { boardId: 'b', cardId: 'c', specId: 's', edition: 1 };
const result = {
  board_id: 'b', card_id: 'c', spec_id: 's', edition: 1, status: 'rejected', title: 'Parser',
  actions: { record_progress: false }, progress: { total: 25 }, latest_checkpoint: { actor_id: 'author-A', summary: 'Short note' },
  accumulated_impact: { status: 'needs_reconciliation', history_count: 3 },
  obligations: { complete: false, total: 1, items: [{ ref: 'fr', title: 'Behavior', implementation_satisfied: false, test_satisfied: false }] },
  implementation_proofs: { total: 1, items: [{ record_id: 'proof', actor_id: 'author-A', source_ref: 'repo', result_revision: 'abc',
    relative_path: 'parser.py', current_obligation_refs: [], contributions: [{ obligation_ref: 'fr', declaration: 'partial' }] }] },
  tests: { total: 0, items: [] }, targets: { truncated: true, items: [] },
};
beforeEach(() => { vi.resetAllMocks(); api.getCardDeliveryResume.mockResolvedValue(result); });
afterEach(cleanup);

it('distinguishes partial declarations, stale proof, unknown recovery and frozen authority', async () => {
  render(<CardResumePanel {...props} />);
  expect(api.getCardDeliveryResume).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  await screen.findByText(/Latest checkpoint by author-A/);
  expect(screen.getByText('fr: declared partial.')).toBeInTheDocument();
  expect(screen.getByText('Currently admitted obligations: None.')).toBeInTheDocument();
  expect(screen.getByText(/Workspace access and recovery are unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Progress recording is unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Obligation coverage is unknown/)).toBeInTheDocument();
  expect(screen.getByText(/This context is shortened/)).toBeInTheDocument();
});

it('clears old data on refresh failure instead of treating it as empty success', async () => {
  render(<CardResumePanel {...props} />);
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  await screen.findByText(/Latest checkpoint by author-A/);
  api.getCardDeliveryResume.mockRejectedValueOnce(new Error('Permission denied'));
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Latest checkpoint by author-A/)).not.toBeInTheDocument();
});

it('shows planned responsibility and relevant test history without treating a plan as a passing result', async () => {
  api.getCardDeliveryResume.mockResolvedValue({ ...result,
    verification_plan: { complete: true, items: [{ scenario_id: 'auth-test', method: 'automated_test',
      criterion_ids: ['auth-criterion'], test_card_ids: ['test-card'], blockers: [] }] },
    tests: { scope: 'card_and_related_obligations', total: 1, total_exact: true, items: [
      { record_id: 'run', card_id: 'test-card', scenario_id: 'auth-test', result: 'failed', current_verified_run: true, observes_this_card: true },
    ] },
  });
  render(<CardResumePanel {...props} />);
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  await screen.findByText(/Planned scenario: auth-test/);
  expect(screen.getByText('Responsible Test Cards: test-card.')).toBeInTheDocument();
  expect(screen.getByText('auth-test: failed; authentication current.')).toBeInTheDocument();
  expect(screen.getByText('History of Test Card test-card')).toBeInTheDocument();
  expect(screen.getByText(/1 test records related to this Card/)).toBeInTheDocument();
});

it('rejects another edition and cancels pending requests after unmount', async () => {
  api.getCardDeliveryResume.mockResolvedValueOnce({ ...result, edition: 2 });
  const view = render(<CardResumePanel {...props} />);
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Latest checkpoint by author-A/)).not.toBeInTheDocument();
  let finish!: (value: unknown) => void;
  api.getCardDeliveryResume.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  const signal = api.getCardDeliveryResume.mock.calls[1][3];
  view.unmount(); expect(signal.aborted).toBe(true);
  await act(async () => finish(result));
});
