import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LearningIntentSelector } from '../LearningIntentSelector';
import type { LearningCandidates } from '@/services/learning-capture-api';

const api = vi.hoisted(() => ({ candidates: vi.fn() }));
vi.mock('@/services/learning-capture-api', () => ({ useLearningCaptureApi: () => api }));
const candidate = { learning_id: 'target', generation: 2, source_revision: 3, fingerprint: 'b'.repeat(64),
  content: '<script>Existing lesson</script>', context: 'Original limits', similarity: 0.97, suggestion: 'reuse' as const };
const page: LearningCandidates = { status: 'available', items: [candidate], limitation: null, limitations: [] };
const change = vi.fn();
function selector(refresh = 0) { return <LearningIntentSelector boardId="board" bugId="bug" content="New lesson" refresh={refresh} onChange={change} />; }
beforeEach(() => { change.mockReset(); api.candidates.mockReset().mockResolvedValue(page); });
afterEach(cleanup);
async function search() { fireEvent.click(screen.getByRole('button', { name: 'Find suggestions' })); await screen.findByText(candidate.content); }

it.each(['reuse', 'supersede'])('requires explicit %s and reason; the score makes no decision', async kind => {
  const { container } = render(selector());
  expect(api.candidates).not.toHaveBeenCalled();
  await search();
  expect(change).not.toHaveBeenCalled();
  expect(container.querySelector('script')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: kind === 'reuse' ? 'Reuse this Learning' : 'Replace for this Bug' }));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ ready: false }));
  fireEvent.change(screen.getByLabelText(kind === 'reuse' ? 'Reason for reuse' : 'Reason for replacement'), { target: { value: 'Applies to this correction' } });
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ ready: true, intent: {
    kind, target_node_id: 'target', target_generation: 2, expected_fingerprint: candidate.fingerprint,
    reason: 'Applies to this correction', ...(kind === 'supersede' ? { scope: 'source_bug' } : {}) } }));
  fireEvent.click(screen.getByRole('button', { name: 'Create a new Learning' }));
  expect(change).toHaveBeenLastCalledWith({ ready: true });
});

it('invalidates a target on source refresh without choosing create or a replacement automatically', async () => {
  const view = render(selector()); await search();
  fireEvent.click(screen.getByRole('button', { name: 'Reuse this Learning' }));
  fireEvent.change(screen.getByLabelText('Reason for reuse'), { target: { value: 'Same lesson' } });
  view.rerender(selector(1));
  expect(change).toHaveBeenLastCalledWith({ ready: false });
  expect(screen.getByRole('alert')).toHaveTextContent('Choose a target again');
  expect(screen.getByRole('button', { name: 'Create a new Learning' })).toHaveAttribute('aria-pressed', 'false');
});

it('distinguishes unavailable suggestions from an empty search window', async () => {
  api.candidates.mockResolvedValueOnce({ status: 'unavailable', items: [], limitation: 'search_unavailable', limitations: [] })
    .mockResolvedValueOnce({ ...page, items: [] });
  render(selector()); fireEvent.click(screen.getByRole('button', { name: 'Find suggestions' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Suggestions are unavailable');
  expect(screen.queryByText('No verified suggestions in this search window.')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Find suggestions' }));
  await screen.findByText('No verified suggestions in this search window.');
  expect(change).not.toHaveBeenCalled();
});

it('aborts on unmount and ignores the late private result', async () => {
  let resolve!: (value: LearningCandidates) => void;
  api.candidates.mockReturnValueOnce(new Promise<LearningCandidates>(done => { resolve = done; }));
  const view = render(selector()); fireEvent.click(screen.getByRole('button', { name: 'Find suggestions' }));
  const signal = api.candidates.mock.calls[0][3] as AbortSignal;
  view.unmount(); expect(signal.aborted).toBe(true);
  await act(async () => resolve(page));
  expect(change).not.toHaveBeenCalled();
});
