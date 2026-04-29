/**
 * useListSearch — generic client-side search/filter hook for list panels.
 *
 * Filters an in-memory array by a debounced query against a configurable set
 * of fields (or a custom matcher). Returns the filtered list, the current
 * query, and a setter. Pairs with `<SearchInput />` for UI; both are
 * independent so the hook can be used headless (e.g. in tests).
 *
 * URL persistence is delegated to the caller — pass `urlParam` to read the
 * initial query from the URL and mirror updates back via History API. The
 * hook keeps things in sync without depending on react-router.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

export type ListSearchMatcher<T> = (item: T, query: string) => boolean;

export interface UseListSearchOptions<T> {
  /** Field names whose stringified value is matched. Ignored if `matcher` is set. */
  fields?: Array<keyof T>;
  /** Optional custom predicate. Overrides `fields`. */
  matcher?: ListSearchMatcher<T>;
  /** Debounce in ms before applying the query. Default: 200. */
  debounceMs?: number;
  /** URL search param key to mirror the query into. Empty disables URL sync. */
  urlParam?: string;
  /** Initial query (used when `urlParam` is empty or absent from URL). */
  initial?: string;
}

export interface UseListSearchResult<T> {
  query: string;
  setQuery: (q: string) => void;
  filtered: T[];
  clear: () => void;
}

const DEFAULT_DEBOUNCE_MS = 200;

function _readUrlParam(name: string): string {
  if (typeof window === 'undefined' || !name) return '';
  try {
    return new URLSearchParams(window.location.search).get(name) ?? '';
  } catch {
    return '';
  }
}

function _writeUrlParam(name: string, value: string): void {
  if (typeof window === 'undefined' || !name) return;
  try {
    const url = new URL(window.location.href);
    if (value) url.searchParams.set(name, value);
    else url.searchParams.delete(name);
    window.history.replaceState(window.history.state, '', url.toString());
  } catch {
    // No-op on environments without a usable history API (e.g. some tests).
  }
}

function _stringify(v: unknown): string {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.map(_stringify).join(' ');
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return '';
}

function _buildDefaultMatcher<T>(fields: Array<keyof T>): ListSearchMatcher<T> {
  const lowered = fields;
  return (item, q) => {
    const needle = q.trim().toLowerCase();
    if (!needle) return true;
    for (const f of lowered) {
      if (_stringify(item[f]).toLowerCase().includes(needle)) return true;
    }
    return false;
  };
}

export function useListSearch<T>(
  items: readonly T[],
  options: UseListSearchOptions<T> = {},
): UseListSearchResult<T> {
  const {
    fields,
    matcher,
    debounceMs = DEFAULT_DEBOUNCE_MS,
    urlParam = '',
    initial = '',
  } = options;

  const initialQuery = urlParam ? _readUrlParam(urlParam) || initial : initial;
  const [query, setQueryState] = useState<string>(initialQuery);
  const [debouncedQuery, setDebouncedQuery] = useState<string>(initialQuery);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setQuery = (q: string) => {
    setQueryState(q);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      setDebouncedQuery(q);
      if (urlParam) _writeUrlParam(urlParam, q);
    }, debounceMs);
  };

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const effectiveMatcher = useMemo<ListSearchMatcher<T>>(() => {
    if (matcher) return matcher;
    if (fields && fields.length > 0) return _buildDefaultMatcher<T>(fields);
    return () => true;
  }, [matcher, fields]);

  const filtered = useMemo(() => {
    const q = debouncedQuery.trim();
    if (!q) return [...items];
    return items.filter((it) => effectiveMatcher(it, q));
  }, [items, debouncedQuery, effectiveMatcher]);

  const clear = () => setQuery('');

  return { query, setQuery, filtered, clear };
}
