import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { TaskRequirementControl } from '../TaskRequirementControl';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { TaskRequirementOptions } from '@/types/task-requirement';

const api = vi.hoisted(() => ({ getTaskRequirementOptions: vi.fn(), applyTaskRequirementDecision: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const cards = [{ id: 'a', title: 'Integration' }, { id: 'b', title: 'Documentation' }];
const props = { boardId: 'board', specId: 'spec', cards: [cards[0]] };

export function preview(ids = ['a']): TaskRequirementOptions {
  return { contract_version: 'task-requirement-options/v1', board_id: 'board', spec_id: 'spec',
    edition: 2, spec_version: 7, basis: 'a'.repeat(64),
    cards: ids.map(id => ({ card_id: id, title: cards.find(card => card.id === id)!.title,
      lifecycle_status: 'in_progress', decision: 'required', decision_id: null,
      scope_sha256: 'b'.repeat(64), mutable: true, reason_code: null, history_count: 0, user_note: null })),
    allowed_decisions: ['waived', 'optional', 'required'], note_optional: true, can_apply: true,
    effects: { lifecycle_unchanged: true, proof_credit: false, dependencies_unchanged: true,
      dependent_tasks: [], test_scenarios_remain_governed: false } };
}

beforeEach(() => {
  vi.resetAllMocks();
  api.getTaskRequirementOptions.mockImplementation((_b, _s, ids) => Promise.resolve(preview(ids)));
  api.applyTaskRequirementDecision.mockImplementation((_b, _s, body) => Promise.resolve({
    contract_version: 'task-requirement-operation/v1', status: 'applied', decision: body.decision,
    affected_cards: body.card_ids, replayed: false }));
});
afterEach(cleanup);

async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Change task requirement' }));
  await waitFor(() => expect(screen.getByLabelText('Requirement')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Requirement'), { target: { value: 'waived' } });
}

it('loads only on access and saves a human choice with no mandatory note or proof fields', async () => {
  const changed = vi.fn();
  render(<TaskRequirementControl {...props} onChanged={changed} />);
  expect(api.getTaskRequirementOptions).not.toHaveBeenCalled();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Save task requirement' }));
  await screen.findByText('Task requirement saved. Work status and proof are unchanged.');
  expect(api.applyTaskRequirementDecision).toHaveBeenCalledWith('board', 'spec', {
    contract_version: 'task-requirement-decision/v1', card_ids: ['a'], decision: 'waived',
    basis: 'a'.repeat(64), note: null, idempotency_key: expect.any(String) });
  expect(changed).toHaveBeenCalledOnce();
  expect(api.getTaskRequirementOptions).toHaveBeenCalledOnce();
});

it('selects several tasks and sends one atomic command', async () => {
  render(<TaskRequirementControl {...props} cards={cards} />);
  fireEvent.click(screen.getByRole('button', { name: 'Change task requirement' }));
  expect(api.getTaskRequirementOptions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('checkbox', { name: 'Integration' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Documentation' }));
  await waitFor(() => expect(screen.getByLabelText('Requirement')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Requirement'), { target: { value: 'optional' } });
  fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: 'Deferred by decision.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save task requirement' }));
  await waitFor(() => expect(api.applyTaskRequirementDecision).toHaveBeenCalledOnce());
  expect(api.applyTaskRequirementDecision.mock.calls[0][2]).toMatchObject({ card_ids: ['a', 'b'], decision: 'optional', note: 'Deferred by decision.' });
});

it('keeps note and choice after a conflict and requires review before retry', async () => {
  api.applyTaskRequirementDecision.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'stale' }));
  render(<TaskRequirementControl {...props} />);
  await open();
  fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: 'Keep this explanation' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save task requirement' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Note (optional)')).toHaveValue('Keep this explanation');
  expect(screen.getByLabelText('Requirement')).toHaveValue('waived');
  expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeDisabled();
  expect(api.getTaskRequirementOptions).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Review latest task requirements' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeEnabled());
  expect(api.applyTaskRequirementDecision).toHaveBeenCalledOnce();
});

it('retries an uncertain response with the exact original payload and key', async () => {
  api.applyTaskRequirementDecision.mockRejectedValueOnce(new TypeError('network'));
  render(<TaskRequirementControl {...props} />);
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Save task requirement' }));
  const retry = await screen.findByRole('button', { name: 'Retry same decision' });
  expect(screen.getByLabelText('Requirement')).toBeDisabled();
  expect(screen.getByLabelText('Note (optional)')).toBeDisabled();
  fireEvent.click(retry);
  await screen.findByText('Task requirement saved. Work status and proof are unchanged.');
  expect(api.applyTaskRequirementDecision.mock.calls[1]).toEqual(api.applyTaskRequirementDecision.mock.calls[0]);
});

it('invalidates a visible draft on a revision change without replacing its basis silently', async () => {
  const view = render(<TaskRequirementControl {...props} revision={1} />);
  await open();
  fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: 'Draft note' } });
  view.rerender(<TaskRequirementControl {...props} revision={2} />);
  expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeDisabled();
  expect(screen.getByLabelText('Note (optional)')).toHaveValue('Draft note');
  expect(api.getTaskRequirementOptions).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Review latest task requirements' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeEnabled());
});

it('uses server permissions and frozen state to prevent editing, while keeping help accessible', async () => {
  api.getTaskRequirementOptions.mockResolvedValue({ ...preview(), allowed_decisions: [], can_apply: false });
  render(<TaskRequirementControl {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Change task requirement' }));
  await screen.findByText('Only an authorized human can change these requirements.');
  expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeDisabled();
  const handler = vi.fn();
  window.addEventListener('okto:open-help', handler);
  fireEvent.click(screen.getByRole('button', { name: 'Task requirement rules' }));
  expect(handler.mock.calls[0][0].detail).toEqual({ sectionId: 'task-requirements' });
  window.removeEventListener('okto:open-help', handler);
});

it('rejects another selection response and never saves it', async () => {
  api.getTaskRequirementOptions.mockResolvedValue(preview(['b']));
  render(<TaskRequirementControl {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Change task requirement' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: 'Save task requirement' })).toBeDisabled();
  expect(api.applyTaskRequirementDecision).not.toHaveBeenCalled();
});
