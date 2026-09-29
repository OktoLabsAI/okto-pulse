import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LearningSubmissionEditor } from '../LearningSubmissionEditor';
import type { CaptureSource } from '@/services/learning-capture-api';

const api = vi.hoisted(() => ({ source: vi.fn() }));
vi.mock('@/services/learning-capture-api', () => ({ useLearningCaptureApi: () => api }));
const source = { source_digest: 'a'.repeat(64), source_policy_version: 7, scenarios: [
  { id: 'proof', title: 'Signed inspection', authenticated: true },
  { id: 'legacy', title: 'Legacy note', authenticated: false },
] };
const change = vi.fn();
function editor(bugId = 'bug', canCreate = true) {
  return <LearningSubmissionEditor boardId="board" bugId={bugId} canCreate={canCreate} onChange={change} />;
}
async function fill() {
  fireEvent.click(screen.getByRole('checkbox', { name: 'Record a Learning with this report' }));
  for (const label of ['Learning for this report', 'Learning context', 'Learning applicability']) {
    fireEvent.change(screen.getByLabelText(label), { target: { value: 'Authored content' } });
  }
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Signed inspection' }));
}
beforeEach(() => { change.mockReset(); api.source.mockReset().mockResolvedValue(source); });
afterEach(cleanup);

it('loads only after opt-in and requires authored content and authenticated evidence', async () => {
  render(editor()); expect(api.source).not.toHaveBeenCalled();
  await fill();
  expect(screen.getByRole('checkbox', { name: /Legacy note/ })).toBeDisabled();
  expect(change).toHaveBeenLastCalledWith({ content: 'Authored content', context: 'Authored content',
    applicability: 'Authored content', expected_source_digest: source.source_digest,
    expected_source_version: 7, scenario_ids: ['proof'] }, false);
  fireEvent.change(screen.getByLabelText('Learning applicability'), { target: { value: ' ' } });
  expect(change).toHaveBeenLastCalledWith(null, true);
  fireEvent.click(screen.getByRole('checkbox', { name: 'Record a Learning with this report' }));
  expect(change).toHaveBeenLastCalledWith(null, false);
});

it('preserves text but requires evidence reselection after refresh', async () => {
  render(editor()); await fill();
  api.source.mockResolvedValue({ ...source, source_digest: 'b'.repeat(64), source_policy_version: 8 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh Learning evidence' }));
  expect(change).toHaveBeenLastCalledWith(null, true);
  expect(await screen.findByRole('checkbox', { name: 'Signed inspection' })).not.toBeChecked();
  expect(screen.getByLabelText('Learning for this report')).toHaveValue('Authored content');
  fireEvent.click(screen.getByRole('checkbox', { name: 'Signed inspection' }));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ expected_source_version: 8 }), false);
});

it('fails closed without authoring authority and lets the user remove a revoked selection', async () => {
  const view = render(editor('bug', false));
  expect(screen.getByRole('checkbox')).toBeDisabled(); expect(api.source).not.toHaveBeenCalled();
  view.rerender(editor()); await fill(); view.rerender(editor('bug', false));
  expect(screen.getByRole('alert')).toHaveTextContent('permission is unavailable');
  expect(change).toHaveBeenLastCalledWith(null, true);
  fireEvent.click(screen.getByRole('checkbox'));
  expect(change).toHaveBeenLastCalledWith(null, false);
});

it('does not mistake provider failure for no evidence and preserves typed text', async () => {
  api.source.mockRejectedValueOnce(new Error('PRIVATE provider details'));
  render(editor()); fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.change(screen.getByLabelText('Learning for this report'), { target: { value: 'Retain this lesson' } });
  expect(await screen.findByRole('alert')).not.toHaveTextContent('PRIVATE');
  expect(screen.getByLabelText('Learning for this report')).toHaveValue('Retain this lesson');
  expect(change).toHaveBeenLastCalledWith(null, true);
});

it('aborts the previous Bug request and discards its late response', async () => {
  let resolve!: (value: CaptureSource) => void;
  api.source.mockImplementationOnce(() => new Promise<CaptureSource>(done => { resolve = done; }));
  const view = render(editor()); fireEvent.click(screen.getByRole('checkbox'));
  const signal = api.source.mock.calls[0][2] as AbortSignal;
  view.rerender(editor('other-bug'));
  expect(signal.aborted).toBe(true);
  await waitFor(() => expect(api.source).toHaveBeenCalledTimes(2));
  await act(async () => resolve({ ...source, scenarios: [{ id: 'foreign', title: 'Foreign evidence', authenticated: true }] }));
  expect(screen.queryByText('Foreign evidence')).not.toBeInTheDocument();
  expect(change).toHaveBeenLastCalledWith(null, true);
});
