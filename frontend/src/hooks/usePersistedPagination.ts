/**
 * Small, framework-agnostic pagination state contract plus a React adapter.
 *
 * The URL is authoritative when it contains valid values. localStorage is the
 * fallback, so each list remembers its last position even after its query
 * parameters are removed. Both stores are updated together without requiring
 * react-router.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { SetStateAction } from 'react';

export const PAGINATION_PAGE_SIZES = [25, 50, 100] as const;
export type PaginationPageSize = (typeof PAGINATION_PAGE_SIZES)[number];

export const DEFAULT_PAGINATION_PAGE_SIZE: PaginationPageSize = 25;

export interface PaginationState {
  /** One-based page number. */
  page: number;
  pageSize: PaginationPageSize;
}
export interface PaginationRequestIntent extends PaginationState {
  offset: number;
  limit: PaginationPageSize;
}

interface PaginationEnvironment {
  search?: string;
  storage?: Pick<Storage, 'getItem'> | null;
}

interface PaginationPersistenceEnvironment {
  url?: string;
  history?: Pick<History, 'replaceState' | 'state'> | null;
  storage?: Pick<Storage, 'setItem'> | null;
}

const STORAGE_PREFIX = 'okto.pagination.';
const URL_PREFIX = 'okto.pagination.';

function positiveInteger(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export function normalizePageSize(
  value: unknown,
  fallback: PaginationPageSize = DEFAULT_PAGINATION_PAGE_SIZE,
): PaginationPageSize {
  const parsed = typeof value === 'number' ? value : Number(value);
  return PAGINATION_PAGE_SIZES.includes(parsed as PaginationPageSize)
    ? (parsed as PaginationPageSize)
    : fallback;
}

export function normalizePaginationState(
  value: Partial<PaginationState> | null | undefined,
  fallback: PaginationState = { page: 1, pageSize: DEFAULT_PAGINATION_PAGE_SIZE },
): PaginationState {
  return {
    page: positiveInteger(value?.page, fallback.page),
    pageSize: normalizePageSize(value?.pageSize, fallback.pageSize),
  };
}

export function paginationRequestIntent(state: PaginationState): PaginationRequestIntent {
  const normalized = normalizePaginationState(state);
  return {
    ...normalized,
    offset: (normalized.page - 1) * normalized.pageSize,
    limit: normalized.pageSize,
  };
}

function storageKey(listKey: string): string {
  return `${STORAGE_PREFIX}${listKey}`;
}

export function paginationUrlKeys(listKey: string): { page: string; pageSize: string } {
  return {
    page: `${URL_PREFIX}${listKey}.page`,
    pageSize: `${URL_PREFIX}${listKey}.page_size`,
  };
}

function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readStoredState(
  listKey: string,
  storage: Pick<Storage, 'getItem'> | null,
): Partial<PaginationState> {
  if (!storage) return {};
  try {
    const raw = storage.getItem(storageKey(listKey));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<PaginationState>;
    return typeof parsed === 'object' && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

/** Read one list's state. Valid URL fields override their stored counterparts. */
export function readPaginationState(
  listKey: string,
  environment: PaginationEnvironment = {},
): PaginationState {
  const storage = environment.storage === undefined ? browserStorage() : environment.storage;
  const stored = normalizePaginationState(readStoredState(listKey, storage));
  const search = environment.search
    ?? (typeof window === 'undefined' ? '' : window.location.search);

  try {
    const params = new URLSearchParams(search);
    const keys = paginationUrlKeys(listKey);
    const page = params.get(keys.page);
    const pageSize = params.get(keys.pageSize);
    return {
      page: page === null ? stored.page : positiveInteger(page, stored.page),
      pageSize: pageSize === null ? stored.pageSize : normalizePageSize(pageSize, stored.pageSize),
    };
  } catch {
    return stored;
  }
}

/** Persist one atomic request intent to both durable surfaces. */
export function persistPaginationState(
  listKey: string,
  value: PaginationState,
  environment: PaginationPersistenceEnvironment = {},
): PaginationState {
  const normalized = normalizePaginationState(value);
  const storage = environment.storage === undefined ? browserStorage() : environment.storage;

  try {
    storage?.setItem(storageKey(listKey), JSON.stringify(normalized));
  } catch {
    // Storage can be unavailable in private mode or when quota is exhausted.
  }

  const history = environment.history === undefined
    ? (typeof window === 'undefined' ? null : window.history)
    : environment.history;
  const currentUrl = environment.url
    ?? (typeof window === 'undefined' ? undefined : window.location.href);

  if (history && currentUrl) {
    try {
      const url = new URL(currentUrl);
      const keys = paginationUrlKeys(listKey);
      url.searchParams.set(keys.page, String(normalized.page));
      url.searchParams.set(keys.pageSize, String(normalized.pageSize));
      history.replaceState(history.state, '', url.toString());
    } catch {
      // Some embedded/test environments expose an unusable History API.
    }
  }

  return normalized;
}

export interface UsePersistedPaginationResult extends PaginationState {
  setPagination: (next: SetStateAction<PaginationState>) => void;
  requestIntent: PaginationRequestIntent;
}

/**
 * Persist a page and page size independently for each list key.
 *
 * Consumers can pass `setPagination` directly to the shared paginator. A
 * single component interaction therefore becomes one state update and one
 * request intent (`offset`/`limit`).
 */
export function usePersistedPagination(listKey: string): UsePersistedPaginationResult {
  const [state, setState] = useState<PaginationState>(() => readPaginationState(listKey));
  const stateRef = useRef(state);

  useEffect(() => {
    const next = readPaginationState(listKey);
    stateRef.current = next;
    setState(next);
  }, [listKey]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handlePopState = () => {
      const next = readPaginationState(listKey);
      stateRef.current = next;
      setState(next);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [listKey]);

  const setPagination = useCallback((next: SetStateAction<PaginationState>) => {
    const resolved = typeof next === 'function' ? next(stateRef.current) : next;
    const persisted = persistPaginationState(listKey, resolved);
    stateRef.current = persisted;
    setState(persisted);
  }, [listKey]);

  return {
    ...state,
    setPagination,
    requestIntent: paginationRequestIntent(state),
  };
}
