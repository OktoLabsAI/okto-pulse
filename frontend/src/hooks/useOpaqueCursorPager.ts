import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

import type {
  CursorCollectionError,
  OpaqueCursorPage,
} from './useOpaqueCursorCollection';

export interface UseOpaqueCursorPagerOptions<
  T,
  TPage extends Omit<OpaqueCursorPage<T>, 'limit'> & { limit?: number },
> {
  enabled: boolean;
  resetKey: string;
  loadPage: (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => Promise<TPage>;
  getItemKey: (item: T) => string;
  classifyError?: (error: unknown) => CursorCollectionError;
  duplicateItemMessage?: string;
  repeatedCursorMessage?: string;
}

export interface OpaqueCursorPager<
  T,
  TPage extends Omit<OpaqueCursorPage<T>, 'limit'> & { limit?: number },
> {
  items: T[];
  page: TPage | null;
  pageNumber: number;
  loading: boolean;
  loaded: boolean;
  error: string | null;
  restartRequired: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  previous: () => void;
  next: () => void;
  retry: () => void;
  restart: () => void;
}

function defaultError(error: unknown): CursorCollectionError {
  return {
    message: error instanceof Error ? error.message : 'Unexpected cursor error.',
    restartRequired: false,
  };
}

interface CursorLocation {
  cursor: string | undefined;
  index: number;
}

/**
 * Discrete Previous/Next navigation for an opaque keyset cursor API.
 *
 * Cursor contents remain server-owned. The client only keeps the already
 * visited cursors so it can revisit a page, aborts stale requests, and fails
 * closed when the server emits duplicate identities or a cursor loop.
 */
export function useOpaqueCursorPager<
  T,
  TPage extends Omit<OpaqueCursorPage<T>, 'limit'> & { limit?: number }
    = OpaqueCursorPage<T>,
>({
  enabled,
  resetKey,
  loadPage,
  getItemKey,
  classifyError = defaultError,
  duplicateItemMessage = 'The server returned a duplicate item. Restart the list before continuing.',
  repeatedCursorMessage = 'The server returned a repeated cursor. Restart the list before continuing.',
}: UseOpaqueCursorPagerOptions<T, TPage>): OpaqueCursorPager<T, TPage> {
  const [page, setPage] = useState<TPage | null>(null);
  const [location, setLocation] = useState<CursorLocation>({
    cursor: undefined,
    index: 0,
  });
  const [history, setHistory] = useState<Array<string | undefined>>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const [loadedResetKey, setLoadedResetKey] = useState(resetKey);

  const epochRef = useRef(0);
  const activeControllerRef = useRef<AbortController | null>(null);
  const enabledRef = useRef(enabled);
  const resetKeyRef = useRef(resetKey);
  const loadPageRef = useRef(loadPage);
  const getItemKeyRef = useRef(getItemKey);
  const classifyErrorRef = useRef(classifyError);
  const duplicateItemMessageRef = useRef(duplicateItemMessage);
  const repeatedCursorMessageRef = useRef(repeatedCursorMessage);
  const locationRef = useRef(location);
  const historyRef = useRef(history);
  const loadedRef = useRef(loaded);
  const loadedResetKeyRef = useRef(loadedResetKey);
  const previousEnabledRef = useRef(enabled);

  enabledRef.current = enabled;
  resetKeyRef.current = resetKey;
  loadPageRef.current = loadPage;
  getItemKeyRef.current = getItemKey;
  classifyErrorRef.current = classifyError;
  duplicateItemMessageRef.current = duplicateItemMessage;
  repeatedCursorMessageRef.current = repeatedCursorMessage;
  locationRef.current = location;
  historyRef.current = history;
  loadedRef.current = loaded;
  loadedResetKeyRef.current = loadedResetKey;

  const load = useCallback(async (
    nextLocation: CursorLocation,
    nextHistory: Array<string | undefined>,
    epoch: number,
  ) => {
    if (!enabledRef.current) return;
    activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    setLoading(true);
    setError(null);
    setRestartRequired(false);

    try {
      const response = await loadPageRef.current(
        nextLocation.cursor,
        controller.signal,
      );
      if (controller.signal.aborted || epoch !== epochRef.current) return;

      const identities = new Set<string>();
      const duplicate = response.items.some((item) => {
        const key = getItemKeyRef.current(item);
        if (identities.has(key)) return true;
        identities.add(key);
        return false;
      });
      if (duplicate) {
        setPage(null);
        setError(duplicateItemMessageRef.current);
        setRestartRequired(true);
        setLoaded(true);
        return;
      }

      const visited = new Set(
        [...nextHistory, nextLocation.cursor]
          .filter((cursor): cursor is string => cursor !== undefined),
      );
      if (
        response.has_more
        && (
          !response.next_cursor
          || response.next_cursor === nextLocation.cursor
          || visited.has(response.next_cursor)
        )
      ) {
        setPage(null);
        setError(repeatedCursorMessageRef.current);
        setRestartRequired(true);
        setLoaded(true);
        return;
      }

      setLocation(nextLocation);
      setHistory(nextHistory);
      setPage(response);
      setLoaded(true);
      loadedRef.current = true;
    } catch (caught) {
      if (controller.signal.aborted || epoch !== epochRef.current) return;
      const classified = classifyErrorRef.current(caught);
      setError(classified.message);
      setRestartRequired(classified.restartRequired);
      setLoaded(true);
    } finally {
      if (!controller.signal.aborted && epoch === epochRef.current) {
        setLoading(false);
      }
    }
  }, []);

  const restart = useCallback(() => {
    activeControllerRef.current?.abort();
    const epoch = epochRef.current + 1;
    epochRef.current = epoch;
    const first = { cursor: undefined, index: 0 };
    locationRef.current = first;
    historyRef.current = [];
    setLocation(first);
    setHistory([]);
    setPage(null);
    setLoaded(false);
    loadedRef.current = false;
    setError(null);
    setRestartRequired(false);
    setLoadedResetKey(resetKeyRef.current);
    loadedResetKeyRef.current = resetKeyRef.current;
    if (enabledRef.current) {
      void load(first, [], epoch);
    } else {
      setLoading(false);
    }
  }, [load]);

  const next = useCallback(() => {
    if (loading || !page?.has_more || !page.next_cursor) return;
    const nextHistory = [...historyRef.current, locationRef.current.cursor];
    const nextLocation = {
      cursor: page.next_cursor,
      index: locationRef.current.index + 1,
    };
    void load(nextLocation, nextHistory, epochRef.current);
  }, [load, loading, page]);

  const previous = useCallback(() => {
    if (loading || historyRef.current.length === 0) return;
    const nextHistory = historyRef.current.slice(0, -1);
    const previousCursor = historyRef.current.at(-1);
    void load(
      {
        cursor: previousCursor,
        index: Math.max(locationRef.current.index - 1, 0),
      },
      nextHistory,
      epochRef.current,
    );
  }, [load, loading]);

  const retry = useCallback(() => {
    if (loading) return;
    if (restartRequired) {
      restart();
      return;
    }
    void load(locationRef.current, historyRef.current, epochRef.current);
  }, [load, loading, restart, restartRequired]);

  useEffect(() => {
    restart();
    return () => activeControllerRef.current?.abort();
  }, [resetKey, restart]);

  useEffect(() => {
    const wasEnabled = previousEnabledRef.current;
    previousEnabledRef.current = enabled;
    if (!enabled) {
      activeControllerRef.current?.abort();
      setLoading(false);
      return;
    }
    if (
      !wasEnabled
      && (
        !loadedRef.current
        || loadedResetKeyRef.current !== resetKeyRef.current
      )
    ) {
      restart();
    }
  }, [enabled, restart]);

  const scopeMatches = loadedResetKey === resetKey;
  const visiblePage = scopeMatches ? page : null;

  return {
    items: visiblePage?.items ?? [],
    page: visiblePage,
    pageNumber: scopeMatches ? location.index + 1 : 1,
    loading: scopeMatches ? loading : enabled,
    loaded: scopeMatches ? loaded : false,
    error: scopeMatches ? error : null,
    restartRequired: scopeMatches ? restartRequired : false,
    hasPrevious: scopeMatches && history.length > 0,
    hasNext: Boolean(scopeMatches && visiblePage?.has_more && visiblePage.next_cursor),
    previous,
    next,
    retry,
    restart,
  };
}

export default useOpaqueCursorPager;
