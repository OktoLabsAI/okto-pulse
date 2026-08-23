import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useOpaqueCursorPager } from '../useOpaqueCursorPager';

interface Row { id: string }

function page(items: Row[], nextCursor?: string) {
  return {
    items,
    limit: 1,
    has_more: Boolean(nextCursor),
    next_cursor: nextCursor,
  };
}

describe('useOpaqueCursorPager', () => {
  it('does not load until enabled and navigates with opaque cursor history', async () => {
    const loadPage = vi.fn(async (cursor: string | undefined) => (
      cursor === 'cursor-2'
        ? page([{ id: 'two' }])
        : page([{ id: 'one' }], 'cursor-2')
    ));
    const { result, rerender } = renderHook(
      ({ enabled }) => useOpaqueCursorPager({
        enabled,
        resetKey: 'scope',
        loadPage,
        getItemKey: (item: Row) => item.id,
      }),
      { initialProps: { enabled: false } },
    );

    expect(loadPage).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.items).toEqual([{ id: 'one' }]);
    expect(result.current.pageNumber).toBe(1);

    act(() => result.current.next());
    await waitFor(() => expect(result.current.items).toEqual([{ id: 'two' }]));
    expect(result.current.pageNumber).toBe(2);
    expect(loadPage).toHaveBeenLastCalledWith('cursor-2', expect.any(AbortSignal));

    act(() => result.current.previous());
    await waitFor(() => expect(result.current.items).toEqual([{ id: 'one' }]));
    expect(result.current.pageNumber).toBe(1);
    expect(loadPage).toHaveBeenLastCalledWith(undefined, expect.any(AbortSignal));
  });

  it('fails closed on duplicate rows and repeated cursors', async () => {
    const duplicateLoader = vi.fn(async () => page([
      { id: 'duplicate' },
      { id: 'duplicate' },
    ]));
    const duplicate = renderHook(() => useOpaqueCursorPager({
      enabled: true,
      resetKey: 'duplicates',
      loadPage: duplicateLoader,
      getItemKey: (item: Row) => item.id,
    }));

    await waitFor(() => expect(duplicate.result.current.restartRequired).toBe(true));
    expect(duplicate.result.current.items).toEqual([]);

    const loopLoader = vi.fn(async (cursor: string | undefined) => (
      cursor
        ? page([{ id: 'two' }], cursor)
        : page([{ id: 'one' }], 'loop')
    ));
    const loop = renderHook(() => useOpaqueCursorPager({
      enabled: true,
      resetKey: 'loop',
      loadPage: loopLoader,
      getItemKey: (item: Row) => item.id,
    }));
    await waitFor(() => expect(loop.result.current.hasNext).toBe(true));
    act(() => loop.result.current.next());
    await waitFor(() => expect(loop.result.current.restartRequired).toBe(true));
    expect(loop.result.current.error).toMatch(/repeated cursor/i);
  });

  it('retries the exact cursor navigation that failed', async () => {
    let cursorAttempts = 0;
    const loadPage = vi.fn(async (cursor: string | undefined) => {
      if (!cursor) return page([{ id: 'one' }], 'cursor-2');
      cursorAttempts += 1;
      if (cursorAttempts === 1) throw new Error('temporary failure');
      return page([{ id: 'two' }]);
    });
    const { result } = renderHook(() => useOpaqueCursorPager({
      enabled: true,
      resetKey: 'retry-navigation',
      loadPage,
      getItemKey: (item: Row) => item.id,
    }));

    await waitFor(() => expect(result.current.hasNext).toBe(true));
    act(() => result.current.next());
    await waitFor(() => expect(result.current.error).toBe('temporary failure'));
    expect(result.current.items).toEqual([{ id: 'one' }]);

    act(() => result.current.retry());
    await waitFor(() => expect(result.current.items).toEqual([{ id: 'two' }]));
    expect(result.current.pageNumber).toBe(2);
    expect(loadPage).toHaveBeenLastCalledWith('cursor-2', expect.any(AbortSignal));
  });

  it('aborts and ignores an older response after the filter scope changes', async () => {
    let resolveOld!: (value: ReturnType<typeof page>) => void;
    let resolveNew!: (value: ReturnType<typeof page>) => void;
    const oldPromise = new Promise<ReturnType<typeof page>>((resolve) => {
      resolveOld = resolve;
    });
    const newPromise = new Promise<ReturnType<typeof page>>((resolve) => {
      resolveNew = resolve;
    });
    const loadPage = vi.fn()
      .mockReturnValueOnce(oldPromise)
      .mockReturnValueOnce(newPromise);
    const { result, rerender } = renderHook(
      ({ resetKey }) => useOpaqueCursorPager({
        enabled: true,
        resetKey,
        loadPage,
        getItemKey: (item: Row) => item.id,
      }),
      { initialProps: { resetKey: 'old' } },
    );

    await waitFor(() => expect(loadPage).toHaveBeenCalledTimes(1));
    rerender({ resetKey: 'new' });
    await waitFor(() => expect(loadPage).toHaveBeenCalledTimes(2));
    act(() => resolveOld(page([{ id: 'stale' }])));
    expect(result.current.items).toEqual([]);
    act(() => resolveNew(page([{ id: 'fresh' }])));
    await waitFor(() => expect(result.current.items).toEqual([{ id: 'fresh' }]));
  });
});
