/**
 * useListSearch — TC-1 (TS1) covering substring filter across multi-panel
 * shapes (Ideation/Refinement/Spec/Sprint/Card-like). Pins URL persistence,
 * debouncing and clear behavior.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useListSearch } from '../useListSearch';

interface Item {
  id: string;
  title: string;
  description: string | null;
  labels: string[] | null;
}

const items: Item[] = [
  { id: '1', title: 'Universal search bar', description: 'all panels', labels: ['ui', 'search'] },
  { id: '2', title: 'KB lifecycle', description: 'card knowledge base', labels: ['backend'] },
  { id: '3', title: 'Aceite de termos', description: 'onboarding modal', labels: ['legal'] },
  { id: '4', title: 'List/grid view', description: 'hierarchical grouping', labels: ['ui'] },
  { id: '5', title: 'Bug fix copy_knowledge', description: null, labels: ['bug'] },
];

describe('useListSearch', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => {
    vi.useRealTimers();
    window.history.replaceState({}, '', '/');
  });

  it('returns all items when query is empty', () => {
    const { result } = renderHook(() => useListSearch(items, { fields: ['title'] }));
    expect(result.current.filtered).toHaveLength(5);
  });

  it('filters by title (substring, case-insensitive)', () => {
    const { result } = renderHook(() => useListSearch(items, { fields: ['title'] }));
    act(() => result.current.setQuery('SEARCH'));
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.filtered.map((i) => i.id)).toEqual(['1']);
  });

  it('matches across multiple fields including arrays', () => {
    const { result } = renderHook(() =>
      useListSearch(items, { fields: ['title', 'description', 'labels'] }),
    );
    act(() => result.current.setQuery('ui'));
    act(() => vi.advanceTimersByTime(250));
    const ids = result.current.filtered.map((i) => i.id).sort();
    expect(ids).toEqual(['1', '4']);
  });

  it('honors a custom matcher (overrides fields)', () => {
    const { result } = renderHook(() =>
      useListSearch(items, {
        matcher: (it, q) => it.id === q,
      }),
    );
    act(() => result.current.setQuery('3'));
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.filtered).toHaveLength(1);
    expect(result.current.filtered[0].id).toBe('3');
  });

  it('debounces — query is held until debounceMs elapses', () => {
    const { result } = renderHook(() =>
      useListSearch(items, { fields: ['title'], debounceMs: 200 }),
    );
    act(() => result.current.setQuery('Bug'));
    // Before timer, filtered list is still all (no apply yet)
    expect(result.current.filtered).toHaveLength(5);
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.filtered.map((i) => i.id)).toEqual(['5']);
  });

  it('persists query to URL when urlParam is set', () => {
    const { result } = renderHook(() =>
      useListSearch(items, { fields: ['title'], urlParam: 'q_test' }),
    );
    act(() => result.current.setQuery('Bug'));
    act(() => vi.advanceTimersByTime(250));
    const params = new URLSearchParams(window.location.search);
    expect(params.get('q_test')).toBe('Bug');
  });

  it('reads initial query from URL when present', () => {
    window.history.replaceState({}, '', '/?q_test=Bug');
    const { result } = renderHook(() =>
      useListSearch(items, { fields: ['title'], urlParam: 'q_test' }),
    );
    expect(result.current.query).toBe('Bug');
    expect(result.current.filtered.map((i) => i.id)).toEqual(['5']);
  });

  it('clear() resets the query', () => {
    const { result } = renderHook(() =>
      useListSearch(items, { fields: ['title'], urlParam: 'q_test' }),
    );
    act(() => result.current.setQuery('Bug'));
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.filtered).toHaveLength(1);
    act(() => result.current.clear());
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.filtered).toHaveLength(5);
    expect(result.current.query).toBe('');
    expect(new URLSearchParams(window.location.search).get('q_test')).toBeNull();
  });
});
