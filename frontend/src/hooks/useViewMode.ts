/**
 * useViewMode — persist a list/grid preference per panel in localStorage.
 *
 * Storage key shape: `okto.view-mode.<panelKey>` → `'list' | 'grid'`.
 * Falls back to the provided `defaultMode` when no record exists.
 */

import { useCallback, useEffect, useState } from 'react';

export type ViewMode = 'list' | 'grid';

const STORAGE_PREFIX = 'okto.view-mode.';

function _key(panelKey: string): string {
  return `${STORAGE_PREFIX}${panelKey}`;
}

function _read(panelKey: string, fallback: ViewMode): ViewMode {
  if (typeof localStorage === 'undefined') return fallback;
  try {
    const raw = localStorage.getItem(_key(panelKey));
    if (raw === 'list' || raw === 'grid') return raw;
    return fallback;
  } catch {
    return fallback;
  }
}

function _write(panelKey: string, value: ViewMode): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(_key(panelKey), value);
  } catch {
    // ignore — quota / private mode
  }
}

export function useViewMode(panelKey: string, defaultMode: ViewMode = 'list'): {
  viewMode: ViewMode;
  setViewMode: (next: ViewMode) => void;
  toggle: () => void;
} {
  const [viewMode, setViewModeState] = useState<ViewMode>(() => _read(panelKey, defaultMode));

  // Re-read whenever panelKey changes (different panel, different stored pref)
  useEffect(() => {
    setViewModeState(_read(panelKey, defaultMode));
  }, [panelKey, defaultMode]);

  const setViewMode = useCallback((next: ViewMode) => {
    setViewModeState(next);
    _write(panelKey, next);
  }, [panelKey]);

  const toggle = useCallback(() => {
    setViewModeState((curr) => {
      const next: ViewMode = curr === 'list' ? 'grid' : 'list';
      _write(panelKey, next);
      return next;
    });
  }, [panelKey]);

  return { viewMode, setViewMode, toggle };
}
