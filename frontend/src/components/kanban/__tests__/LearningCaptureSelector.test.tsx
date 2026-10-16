import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LearningCaptureSelector } from '../LearningCaptureSelector';
import type { CaptureHistory } from '@/services/learning-capture-api';

const api = vi.hoisted(() => ({ source: vi.fn(), history: vi.fn() }));
vi.mock('@/services/learning-capture-api', () => ({ useLearningCaptureApi: () => api }));
const source = { source_digest: 'a'.repeat(64), source_policy_version: 7, scenarios: [] };
const item = { learning_id: 'learning', generation: 2, source_revision: 3, fingerprint: 'f'.repeat(64),
  capture: { capture_id: 'capture', author_id: 'author', captured_at: '2026-09-29T12:00:00Z',
    content: '<script>lesson</script>', context: 'Context', applicability: 'Scope', intent: { kind: 'create' as const },
    source: { digest: source.source_digest, policy_version: 7 } } };
const change = vi.fn();
function selector(bugId = 'bug', disabled = false) {
  return <LearningCaptureSelector boardId="board" bugId={bugId} disabled={disabled} onChange={change} />;
}
async function choose() {
  fireEvent.click(screen.getByRole('button', { name: 'Choose saved Learning' }));
  return screen.findByRole('radio');
}
beforeEach(() => {
  change.mockReset(); api.source.mockReset().mockResolvedValue(source);
  api.history.mockReset().mockResolvedValue({ items: [item], next_cursor: null });
});
afterEach(cleanup);

it('loads lazily, renders authored text inertly and sends only the selected identity', async () => {
  const view = render(selector());
  expect(api.source).not.toHaveBeenCalled();
  fireEvent.click(await choose());
  expect(view.container.querySelector('script')).toBeNull();
  expect(change).toHaveBeenLastCalledWith({ learning_id: 'learning', generation: 2, fingerprint: item.fingerprint });
  fireEvent.click(screen.getByRole('button', { name: 'Clear Learning selection' }));
  expect(change).toHaveBeenLastCalledWith(null);
  expect(screen.getByRole('radio')).not.toBeChecked();
});

it.each(['digest', 'policy_version'] as const)('disables a capture from an obsolete %s', async field => {
  api.history.mockResolvedValue({ items: [{ ...item, capture: { ...item.capture,
    source: { ...item.capture.source, [field]: field === 'digest' ? 'b'.repeat(64) : 6 } } }], next_cursor: null });
  render(selector());
  expect(await choose()).toBeDisabled();
  expect(screen.getByText(/Captured against a different Bug version/)).toBeInTheDocument();
});

it('clears selection on pagination and refresh, and uses bounded history requests', async () => {
  api.history.mockResolvedValueOnce({ items: [item], next_cursor: 'opaque-next' });
  render(selector()); fireEvent.click(await choose());
  fireEvent.click(screen.getByRole('button', { name: 'Next Learnings' }));
  expect(change).toHaveBeenLastCalledWith(null);
  expect(await screen.findByRole('radio')).not.toBeChecked();
  expect(api.history).toHaveBeenLastCalledWith('board', 'bug', 'opaque-next', expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('radio'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh Learnings' }));
  expect(change).toHaveBeenLastCalledWith(null);
  expect(await screen.findByRole('radio')).not.toBeChecked();
});

it('distinguishes unavailable history from an empty result and hides provider details', async () => {
  api.history.mockRejectedValueOnce(new Error('PRIVATE data')).mockResolvedValueOnce({ items: [], next_cursor: null });
  render(selector()); fireEvent.click(screen.getByRole('button', { name: 'Choose saved Learning' }));
  expect(await screen.findByRole('alert')).not.toHaveTextContent('PRIVATE');
  expect(screen.queryByText('No saved Learnings on this page.')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh Learnings' }));
  expect(await screen.findByText('No saved Learnings on this page.')).toBeInTheDocument();
});

it('aborts the old context and ignores its late response', async () => {
  let resolve!: (value: CaptureHistory) => void;
  api.history.mockImplementationOnce(() => new Promise<CaptureHistory>(done => { resolve = done; }))
    .mockResolvedValueOnce({ items: [], next_cursor: null });
  const view = render(selector()); fireEvent.click(screen.getByRole('button', { name: 'Choose saved Learning' }));
  const signal = api.history.mock.calls[0][3] as AbortSignal;
  view.rerender(selector('other-bug'));
  expect(signal.aborted).toBe(true);
  await screen.findByText('No saved Learnings on this page.');
  await act(async () => resolve({ items: [item], next_cursor: null }));
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expect(change).toHaveBeenLastCalledWith(null);
});

it('prevents selection changes while a validation is being submitted', async () => {
  const view = render(selector()); fireEvent.click(await choose());
  view.rerender(selector('bug', true));
  expect(screen.getByRole('radio')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Refresh Learnings' })).toBeDisabled();
});
