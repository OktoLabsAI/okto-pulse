/**
 * ModalStackContext — drill-down navigation stack for entity modals.
 *
 * Ideação c13f7bd3. Previously, clicking a "Find similar" result inside a
 * modal (e.g. NodeDetailModal) either did nothing or replaced the current
 * modal's content in place — the user lost the path they were tracing and
 * had no way back. This context gives every modal a shared stack:
 *
 *   - `push({type, id})` opens a new modal layered on top of the current
 *     one. The previous entry is kept intact.
 *   - `pop()` dismisses the top modal, revealing the one beneath. Wired
 *     to the "← back" button rendered by `ModalStackRenderer` whenever
 *     `stack.length > 1`.
 *   - `clear()` closes the entire stack. Wired to every modal's own
 *     close/X control — "X fecha todas as modais, independente do nível
 *     de drill down".
 *
 * Entities supported today: card, spec, ideation, refinement, sprint,
 * kg_node. New entity types just need an entry in ModalStackRenderer.
 */

import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

export type ModalStackEntry =
  | { type: 'card'; id: string }
  | { type: 'spec'; id: string }
  | { type: 'ideation'; id: string }
  | { type: 'refinement'; id: string }
  | { type: 'sprint'; id: string }
  | { type: 'kg_node'; id: string };

interface ModalStackContextValue {
  stack: ModalStackEntry[];
  push: (entry: ModalStackEntry) => void;
  pop: () => void;
  clear: () => void;
}

const ModalStackContext = createContext<ModalStackContextValue | null>(null);

export function ModalStackProvider({ children }: { children: ReactNode }) {
  const [stack, setStack] = useState<ModalStackEntry[]>([]);

  const push = useCallback((entry: ModalStackEntry) => {
    setStack((prev) => {
      // Dedupe: if the top of the stack is the same entity, don't push
      // twice (avoids double-click creating phantom layers).
      const top = prev[prev.length - 1];
      if (top && top.type === entry.type && top.id === entry.id) return prev;
      return [...prev, entry];
    });
  }, []);

  const pop = useCallback(() => {
    setStack((prev) => prev.slice(0, -1));
  }, []);

  const clear = useCallback(() => {
    setStack([]);
  }, []);

  const value = useMemo(
    () => ({ stack, push, pop, clear }),
    [stack, push, pop, clear],
  );

  return (
    <ModalStackContext.Provider value={value}>
      {children}
    </ModalStackContext.Provider>
  );
}

export function useModalStack(): ModalStackContextValue {
  const ctx = useContext(ModalStackContext);
  if (!ctx) {
    throw new Error(
      'useModalStack must be used inside <ModalStackProvider>. ' +
        'Wrap the app (App.tsx) with the provider.',
    );
  }
  return ctx;
}

/** Non-throwing variant — used by components that may render outside the
 * provider (e.g. on standalone pages where drill-down is not expected). */
export function useOptionalModalStack(): ModalStackContextValue | null {
  return useContext(ModalStackContext);
}
