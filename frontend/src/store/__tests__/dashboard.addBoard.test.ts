/**
 * Dashboard store — addBoard action tests
 *
 * AC4: After a successful createBoard call the new board is prepended to the
 *      store's boards array and therefore visible in the sidebar.
 *
 * AC5 (race-safe): The addBoard action uses a functional Zustand update
 *      `set(state => ...)` so it reads the CURRENT state at dispatch time,
 *      not a stale closure captured at render time.
 *
 *      Proof: start with [A], concurrently prepend B via setBoards([A,B]),
 *      then call addBoard(C).  Expected result: [C, A, B].
 *      With the old `setBoards([board, ...boardsClosure])` bug the closure
 *      would still hold [A], producing [C, A] — losing B.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { useDashboardStore } from '../dashboard';
import type { BoardSummary } from '@/types';

function makeBoard(id: string, name: string): BoardSummary {
  return {
    id,
    name,
    description: null,
    owner_id: 'owner-1',
    settings: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  };
}

describe('useDashboardStore — addBoard', () => {
  beforeEach(() => {
    // Reset store to a known state before each test
    useDashboardStore.setState({ boards: [] });
  });

  // ------------------------------------------------------------------ AC4
  it('AC4: prepends the new board so the sidebar reflects it immediately', () => {
    const boardA = makeBoard('a', 'Board A');
    const boardB = makeBoard('b', 'Board B');

    useDashboardStore.setState({ boards: [boardA] });

    useDashboardStore.getState().addBoard(boardB);

    const { boards } = useDashboardStore.getState();
    expect(boards[0].id).toBe('b');
    expect(boards[1].id).toBe('a');
    expect(boards).toHaveLength(2);
  });

  // ------------------------------------------------------------------ AC5
  it('AC5: functional update reads current state — survives concurrent setBoards (race-safe)', () => {
    const boardA = makeBoard('a', 'Board A');
    const boardB = makeBoard('b', 'Board B');
    const boardC = makeBoard('c', 'Board C');

    // Step 1 — store starts with [A]
    useDashboardStore.setState({ boards: [boardA] });

    // Step 2 — simulate a CONCURRENT update that adds B (e.g. a background
    //          refresh arriving while createBoard is in-flight).
    //          This changes the store to [A, B].
    useDashboardStore.setState({ boards: [boardA, boardB] });

    // Step 3 — addBoard(C) is called.  With the old closure bug the component
    //          would have captured `boards = [A]` at render time and would
    //          call setBoards([C, A]), losing B.
    //          With the functional update, it reads state.boards = [A, B]
    //          and produces [C, A, B].
    useDashboardStore.getState().addBoard(boardC);

    const { boards } = useDashboardStore.getState();
    expect(boards[0].id).toBe('c'); // new board is at the front
    expect(boards[1].id).toBe('a');
    expect(boards[2].id).toBe('b'); // B was NOT lost — proves race-safety
    expect(boards).toHaveLength(3);
  });

  it('AC5 (contrast — stale closure would lose B): manual simulation confirms bug hypothesis', () => {
    const boardA = makeBoard('a', 'Board A');
    const boardB = makeBoard('b', 'Board B');
    const boardC = makeBoard('c', 'Board C');

    // Capture the closure the same way the old code did
    const boardsClosure = [boardA]; // captured at render time

    // Concurrent update happens (store moves to [A, B]) — closure is now stale
    useDashboardStore.setState({ boards: [boardA, boardB] });

    // Old code path: setBoards([board, ...boardsClosure])
    useDashboardStore.getState().setBoards([boardC, ...boardsClosure]);

    const { boards } = useDashboardStore.getState();
    // B is LOST — demonstrates the bug the new addBoard action fixes
    expect(boards).toHaveLength(2);
    expect(boards.some((b) => b.id === 'b')).toBe(false);
  });
});
