import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  paginationRequestIntent,
  paginationUrlKeys,
  persistPaginationState,
  readPaginationState,
  usePersistedPagination,
} from '../usePersistedPagination';

describe('usePersistedPagination helpers', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState(null, '', '/boards/board-1');
  });

  it('prefers valid per-list URL fields and falls back to localStorage per field', () => {
    window.localStorage.setItem(
      'okto.pagination.specs',
      JSON.stringify({ page: 4, pageSize: 50 }),
    );
    const keys = paginationUrlKeys('specs');
    const search = `?${encodeURIComponent(keys.page)}=2`;

    expect(readPaginationState('specs', { search, storage: window.localStorage })).toEqual({
      page: 2,
      pageSize: 50,
    });
    expect(readPaginationState('stories', { search, storage: window.localStorage })).toEqual({
      page: 1,
      pageSize: 25,
    });
  });

  it('rejects invalid pages and unsupported sizes', () => {
    const keys = paginationUrlKeys('cards');
    const search = `?${encodeURIComponent(keys.page)}=-2&${encodeURIComponent(keys.pageSize)}=500`;
    expect(readPaginationState('cards', { search, storage: null })).toEqual({
      page: 1,
      pageSize: 25,
    });
  });

  it('persists an atomic state in localStorage and the current URL', () => {
    persistPaginationState('refinements', { page: 3, pageSize: 100 });
    const keys = paginationUrlKeys('refinements');
    const params = new URLSearchParams(window.location.search);

    expect(params.get(keys.page)).toBe('3');
    expect(params.get(keys.pageSize)).toBe('100');
    expect(JSON.parse(window.localStorage.getItem('okto.pagination.refinements') ?? '{}'))
      .toEqual({ page: 3, pageSize: 100 });
  });

  it('derives the server offset and limit from the one-based state', () => {
    expect(paginationRequestIntent({ page: 3, pageSize: 50 })).toEqual({
      page: 3,
      pageSize: 50,
      offset: 100,
      limit: 50,
    });
  });
});
describe('usePersistedPagination', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState(null, '', '/boards/board-1');
  });

  it('updates state, request intent, URL and storage once through its setter', () => {
    const { result } = renderHook(() => usePersistedPagination('ideations'));

    act(() => result.current.setPagination({ page: 2, pageSize: 50 }));

    expect(result.current).toMatchObject({
      page: 2,
      pageSize: 50,
      requestIntent: { page: 2, pageSize: 50, offset: 50, limit: 50 },
    });
    expect(readPaginationState('ideations')).toEqual({ page: 2, pageSize: 50 });
  });

  it('reloads the independent preference when the list key changes', () => {
    window.localStorage.setItem(
      'okto.pagination.specs',
      JSON.stringify({ page: 3, pageSize: 100 }),
    );
    const { result, rerender } = renderHook(
      ({ listKey }) => usePersistedPagination(listKey),
      { initialProps: { listKey: 'cards' } },
    );

    rerender({ listKey: 'specs' });

    expect(result.current).toMatchObject({ page: 3, pageSize: 100 });
  });
});
