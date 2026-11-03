import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { TaskRequirementHistory } from '../TaskRequirementHistory';
import { TaskRequirementControl } from '../TaskRequirementControl';

const api = vi.hoisted(() => ({ getTaskRequirementHistory: vi.fn(), getTaskRequirementOptions: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { boardId: 'b', specId: 's', cardId: 'c' };
const page = { contract_version: 'task-requirement-history/v1', board_id: 'b', spec_id: 's', card_id: 'c',
  edition: 2, current_edition: 2, total: 11, next_before_revision: 2,
  items: [{ id: 'd2', revision: 2, decision: 'required', previous_decision: 'waived',
    actor_id: 'Human author', recorded_at: '2026-11-03T09:00:00Z', scope_sha256: 'a'.repeat(64),
    note: 'Reason excerpt', note_truncated: true }] };
beforeEach(() => {
  vi.resetAllMocks();
  api.getTaskRequirementHistory.mockResolvedValue(page);
  api.getTaskRequirementOptions.mockResolvedValue({ contract_version: 'task-requirement-options/v1',
    board_id: 'b', spec_id: 's', cards: [{ card_id: 'c', title: 'Task', decision: 'required', mutable: true }],
    allowed_decisions: [], can_apply: false, effects: { dependent_tasks: [], test_scenarios_remain_governed: false } });
});
afterEach(cleanup);

it('loads history only when Details opens, including for a read-only user', async () => {
  render(<TaskRequirementControl boardId="b" specId="s" cards={[{ id: 'c', title: 'Task' }]} canEdit={false} />);
  expect(api.getTaskRequirementHistory).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'View task requirement' }));
  const summary = await screen.findByText('Details · Decision history for Task');
  expect(api.getTaskRequirementHistory).not.toHaveBeenCalled();
  fireEvent.click(summary);
  await screen.findByText('Waived → Required');
  expect(api.getTaskRequirementHistory).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Save task requirement' })).not.toBeInTheDocument();
  expect(screen.getByText(/Recorded by Human author/)).toBeInTheDocument();
});

it('requests older decisions, the full note and a previous edition explicitly', async () => {
  render(<TaskRequirementHistory {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Older decisions' }));
  await waitFor(() => expect(api.getTaskRequirementHistory).toHaveBeenLastCalledWith('b', 's', 'c',
    { edition: undefined, beforeRevision: 2, recordId: undefined }, expect.any(AbortSignal)));
  fireEvent.click(await screen.findByRole('button', { name: 'Read full note' }));
  await waitFor(() => expect(api.getTaskRequirementHistory).toHaveBeenLastCalledWith('b', 's', 'c',
    { edition: undefined, beforeRevision: undefined, recordId: 'd2' }, expect.any(AbortSignal)));
  fireEvent.change(await screen.findByLabelText('Edition'), { target: { value: '1' } });
  fireEvent.click(screen.getByRole('button', { name: 'View edition' }));
  await waitFor(() => expect(api.getTaskRequirementHistory).toHaveBeenLastCalledWith('b', 's', 'c',
    { edition: 1, beforeRevision: undefined, recordId: undefined }, expect.any(AbortSignal)));
});
