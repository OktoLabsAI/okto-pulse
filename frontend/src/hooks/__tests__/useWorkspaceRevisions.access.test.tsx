import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardStore } from '@/store/dashboard';
import type { Board } from '@/types';
import { useWorkspaceRevisions } from '../useWorkspaceRevisions';

const mocks = vi.hoisted(() => ({ client: {}, api: {
  getWorkspaceRevisions: vi.fn(), getBoard: vi.fn(),
} }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => mocks.client }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => mocks.api }));

const board = (id: string) => ({ id, name: `Protected ${id}` }) as Board;
function Consumer() {
  const currentBoard = useDashboardStore(state => state.currentBoard);
  const boards = useDashboardStore(state => state.boards);
  useWorkspaceRevisions({ boardId: currentBoard?.id,
    subjects: currentBoard ? [{ subject_type: 'spec_collection', subject_id: currentBoard.id }] : [],
    onChange: () => {}, onAccessLost: () => {},
  });
  return <div>
    <nav>{boards.map(item => <span key={item.id}>{item.name}</span>)}</nav>
    {currentBoard && <section aria-label="Board shell">{currentBoard.name}</section>}
  </div>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  mocks.client = {};
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  mocks.api.getWorkspaceRevisions.mockRejectedValue(new AuthenticatedFetchError({ status: 403, message: 'Denied' }));
  useDashboardStore.setState({ boards: [board('a'), board('b')], sharedBoards: [board('a')],
    currentBoard: board('a'), columns: {} as never, columnsMeta: {},
    selectedCardId: 'old-card', isCardModalOpen: true, cardModalTarget: null });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it.each([401, 403, 404])('removes the inaccessible Board shell and cached list entries after confirmed %s', async status => {
  mocks.api.getBoard.mockRejectedValue(new AuthenticatedFetchError({ status, message: 'Denied' }));
  const generation = useDashboardStore.getState().columnsGeneration;
  render(<><Consumer /><Consumer /></>);
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(mocks.api.getBoard).toHaveBeenCalledTimes(1);
  expect(screen.queryByLabelText('Board shell')).not.toBeInTheDocument();
  expect(screen.queryByText('Protected a')).not.toBeInTheDocument();
  expect(screen.getAllByText('Protected b')).toHaveLength(2);
  const state = useDashboardStore.getState();
  expect(state.sharedBoards).toEqual([]);
  expect(state.selectedCardId).toBeNull();
  expect(state.isCardModalOpen).toBe(false);
  expect(state.error).toContain('Access to this Board is no longer available');
  expect(state.columnsGeneration).toBe(generation + 1);
});

it('preserves the Board shell when only a subject permission was denied', async () => {
  mocks.api.getBoard.mockResolvedValue(board('a'));
  render(<Consumer />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByLabelText('Board shell')).toHaveTextContent('Protected a');
});

it('does not treat a failed Board recheck as proof of revocation', async () => {
  mocks.api.getBoard.mockRejectedValue(new Error('Network unavailable'));
  render(<Consumer />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByLabelText('Board shell')).toHaveTextContent('Protected a');
});

it('discards a late denial after the authenticated client changes', async () => {
  let reject!: (error: unknown) => void;
  mocks.api.getBoard.mockImplementationOnce(() => new Promise((_resolve, denied) => { reject = denied; }));
  const view = render(<Consumer />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  mocks.client = {};
  mocks.api.getBoard.mockResolvedValue(board('a'));
  view.rerender(<Consumer />);
  await act(async () => {
    reject(new AuthenticatedFetchError({ status: 403, message: 'Old identity denied' }));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByLabelText('Board shell')).toHaveTextContent('Protected a');
});

it('discards a late denial after selecting another Board', async () => {
  let reject!: (error: unknown) => void;
  mocks.api.getBoard.mockImplementationOnce(() => new Promise((_resolve, denied) => { reject = denied; }));
  render(<Consumer />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  mocks.api.getBoard.mockResolvedValue(board('b'));
  act(() => useDashboardStore.getState().setCurrentBoard(board('b')));
  await act(async () => {
    reject(new AuthenticatedFetchError({ status: 403, message: 'Old Board denied' }));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByLabelText('Board shell')).toHaveTextContent('Protected b');
  expect(useDashboardStore.getState().boards.map(item => item.id)).toEqual(['a', 'b']);
});
