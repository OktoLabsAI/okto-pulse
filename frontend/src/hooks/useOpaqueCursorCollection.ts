import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

export interface OpaqueCursorPage<T> {
  items: T[];
  limit: number;
  has_more: boolean;
  next_cursor?: string;
}

export interface CursorCollectionError {
  message: string;
  restartRequired: boolean;
}

export interface UseOpaqueCursorCollectionOptions<T> {
  enabled: boolean;
  resetKey: string;
  loadPage: (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => Promise<OpaqueCursorPage<T>>;
  getItemKey: (item: T) => string;
  classifyError?: (error: unknown) => CursorCollectionError;
  duplicateItemMessage?: string;
  repeatedCursorMessage?: string;
}

export interface OpaqueCursorCollection<T> {
  items: T[];
  loading: boolean;
  loaded: boolean;
  error: string | null;
  restartRequired: boolean;
  hasMore: boolean;
  loadMore: () => void;
  retry: () => void;
  restart: () => void;
}

interface LastRequest {
  cursor: string | undefined;
  replace: boolean;
}

function defaultError(error: unknown): CursorCollectionError {
  return {
    message: error instanceof Error ? error.message : 'Unexpected cursor error.',
    restartRequired: false,
  };
}

/**
 * Generic append-only loader for opaque keyset cursor APIs.
 *
 * The hook never interprets cursor contents, ignores stale responses after a
 * reset, rejects repeated cursors and duplicate item identities, and offers an
 * explicit restart path for invalid cursor recovery.
 */
export function useOpaqueCursorCollection<T>({
  enabled,
  resetKey,
  loadPage,
  getItemKey,
  classifyError = defaultError,
  duplicateItemMessage = 'The server returned a duplicate item. Restart the list before continuing.',
  repeatedCursorMessage = 'The server returned a repeated cursor. Restart the list before continuing.',
}: UseOpaqueCursorCollectionOptions<T>): OpaqueCursorCollection<T> {
  const [items, setItems] = useState<T[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const [collectionResetKey, setCollectionResetKey] = useState(resetKey);

  const epochRef = useRef(0);
  const resetKeyRef = useRef(resetKey);
  const activeControllerRef = useRef<AbortController | null>(null);
  const seenCursorsRef = useRef(new Set<string>());
  const seenItemKeysRef = useRef(new Set<string>());
  const lastRequestRef = useRef<LastRequest>({
    cursor: undefined,
    replace: true,
  });
  const enabledRef = useRef(enabled);
  const loadPageRef = useRef(loadPage);
  const getItemKeyRef = useRef(getItemKey);
  const classifyErrorRef = useRef(classifyError);
  const duplicateItemMessageRef = useRef(duplicateItemMessage);
  const repeatedCursorMessageRef = useRef(repeatedCursorMessage);

  enabledRef.current = enabled;
  loadPageRef.current = loadPage;
  getItemKeyRef.current = getItemKey;
  classifyErrorRef.current = classifyError;
  duplicateItemMessageRef.current = duplicateItemMessage;
  repeatedCursorMessageRef.current = repeatedCursorMessage;
  resetKeyRef.current = resetKey;

  const performLoad = useCallback(async (
    request: LastRequest,
    epoch: number,
  ) => {
    if (!enabledRef.current) return;

    if (
      request.cursor !== undefined
      && seenCursorsRef.current.has(request.cursor)
    ) {
      setNextCursor(null);
      setError(repeatedCursorMessageRef.current);
      setRestartRequired(true);
      return;
    }

    if (request.replace) {
      seenCursorsRef.current.clear();
      seenItemKeysRef.current.clear();
    }
    activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    lastRequestRef.current = request;
    setLoading(true);
    setError(null);
    setRestartRequired(false);

    try {
      const page = await loadPageRef.current(
        request.cursor,
        controller.signal,
      );
      if (controller.signal.aborted || epoch !== epochRef.current) return;

      const pageKeys = new Set<string>();
      const duplicate = page.items.some((item) => {
        const key = getItemKeyRef.current(item);
        if (
          pageKeys.has(key)
          || (!request.replace && seenItemKeysRef.current.has(key))
        ) {
          return true;
        }
        pageKeys.add(key);
        return false;
      });
      if (duplicate) {
        setNextCursor(null);
        setError(duplicateItemMessageRef.current);
        setRestartRequired(true);
        setLoaded(true);
        return;
      }

      if (
        page.has_more
        && (
          !page.next_cursor
          || page.next_cursor === request.cursor
          || seenCursorsRef.current.has(page.next_cursor)
        )
      ) {
        setNextCursor(null);
        setError(repeatedCursorMessageRef.current);
        setRestartRequired(true);
        setLoaded(true);
        return;
      }

      if (request.cursor !== undefined) {
        seenCursorsRef.current.add(request.cursor);
      }
      if (request.replace) {
        seenItemKeysRef.current = pageKeys;
        setItems(page.items);
      } else {
        pageKeys.forEach((key) => seenItemKeysRef.current.add(key));
        setItems((current) => [...current, ...page.items]);
      }
      setNextCursor(
        page.has_more && page.next_cursor ? page.next_cursor : null,
      );
      setLoaded(true);
    } catch (caught) {
      if (controller.signal.aborted || epoch !== epochRef.current) return;
      const classified = classifyErrorRef.current(caught);
      setError(classified.message);
      setRestartRequired(classified.restartRequired);
      if (classified.restartRequired) setNextCursor(null);
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
    seenCursorsRef.current.clear();
    seenItemKeysRef.current.clear();
    setItems([]);
    setNextCursor(null);
    setLoaded(false);
    setError(null);
    setRestartRequired(false);
    setCollectionResetKey(resetKeyRef.current);
    if (enabledRef.current) {
      void performLoad({ cursor: undefined, replace: true }, epoch);
    } else {
      setLoading(false);
    }
  }, [performLoad]);

  const loadMore = useCallback(() => {
    if (loading || !nextCursor) return;
    void performLoad(
      { cursor: nextCursor, replace: false },
      epochRef.current,
    );
  }, [loading, nextCursor, performLoad]);

  const retry = useCallback(() => {
    if (loading) return;
    if (restartRequired) {
      restart();
      return;
    }
    void performLoad(lastRequestRef.current, epochRef.current);
  }, [loading, performLoad, restart, restartRequired]);

  useEffect(() => {
    restart();
    return () => activeControllerRef.current?.abort();
  }, [enabled, resetKey, restart]);

  const scopeMatches = collectionResetKey === resetKey;

  return {
    items: scopeMatches ? items : [],
    loading: scopeMatches ? loading : enabled,
    loaded: scopeMatches ? loaded : false,
    error: scopeMatches ? error : null,
    restartRequired: scopeMatches ? restartRequired : false,
    hasMore: scopeMatches && nextCursor !== null,
    loadMore,
    retry,
    restart,
  };
}

export default useOpaqueCursorCollection;
