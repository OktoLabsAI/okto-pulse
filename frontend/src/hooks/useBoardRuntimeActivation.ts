import { useCallback, useEffect, useRef } from 'react';

/** Explicit actions only. A late graph failure must not replace Board content. */
export function useBoardRuntimeActivation(
  activate: (boardId: string) => Promise<unknown>,
  isCurrentBoard: (boardId: string) => boolean,
  onUnavailable: () => void,
) {
  const pending = useRef(new Map<string, Promise<boolean>>());
  const mounted = useRef(true);
  const callbacks = useRef({ activate, isCurrentBoard, onUnavailable });
  callbacks.current = { activate, isCurrentBoard, onUnavailable };
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  return useCallback((boardId: string): Promise<boolean> => {
    const existing = pending.current.get(boardId);
    if (existing) return existing;
    const operation = callbacks.current.activate;
    const request = Promise.resolve().then(() => operation(boardId)).then(
      () => true,
      () => {
        if (mounted.current && callbacks.current.isCurrentBoard(boardId)) {
          callbacks.current.onUnavailable();
        }
        return false;
      },
    ).finally(() => { pending.current.delete(boardId); });
    pending.current.set(boardId, request);
    return request;
  }, []);
}
