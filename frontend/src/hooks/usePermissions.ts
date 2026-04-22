/**
 * usePermissions — React hook that exposes the authenticated agent's
 * effective permission flags for a given board.
 *
 * Fail-open contract: `has(flag)` returns `true` whenever the hook has no
 * data yet (loading, error, or before first fetch). Backend gates the
 * action via 403, so the worst outcome is a toast — much better than
 * blocking the UI while permissions load.
 *
 * Cache: 60s staleTime to avoid a roundtrip on every gated component.
 */

import { useEffect, useState } from 'react';

import {
  getMyPermissions,
  type PermissionsResponse,
} from '@/services/permissions-api';

const CACHE_TTL_MS = 60_000;

interface CacheEntry {
  data: PermissionsResponse;
  fetchedAt: number;
}

const cache = new Map<string, CacheEntry>();
const pending = new Map<string, Promise<PermissionsResponse>>();

function getNested(obj: unknown, path: string): unknown {
  if (!obj || typeof obj !== 'object') return undefined;
  const parts = path.split('.');
  let current: unknown = obj;
  for (const part of parts) {
    if (current && typeof current === 'object' && part in current) {
      current = (current as Record<string, unknown>)[part];
    } else {
      return undefined;
    }
  }
  return current;
}

async function fetchWithCache(boardId: string): Promise<PermissionsResponse> {
  const cached = cache.get(boardId);
  if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
    return cached.data;
  }
  const inflight = pending.get(boardId);
  if (inflight) return inflight;

  const p = getMyPermissions(boardId)
    .then((data) => {
      cache.set(boardId, { data, fetchedAt: Date.now() });
      return data;
    })
    .finally(() => {
      pending.delete(boardId);
    });
  pending.set(boardId, p);
  return p;
}

export interface UsePermissionsResult {
  /** The matched built-in preset name, or null for custom/unknown. */
  preset: string | null;
  /** True while the first fetch is in flight. */
  isLoading: boolean;
  /** Last network error, if any. */
  error: Error | null;
  /**
   * Check whether a flag is effectively enabled.
   *
   * Fail-open: returns `true` while loading or on network error. This lets
   * the UI render its full shape before the fetch completes — the backend
   * is still the real gate and will 403 any unauthorised mutation.
   *
   * Absent flags default to `true` as well (backward compat with agents
   * that predate newer flags — mirrors PermissionSet.has on the backend).
   */
  has: (flag: string) => boolean;
}

export function usePermissions(boardId: string | null | undefined): UsePermissionsResult {
  const [data, setData] = useState<PermissionsResponse | null>(() => {
    if (!boardId) return null;
    const cached = cache.get(boardId);
    return cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS
      ? cached.data
      : null;
  });
  const [isLoading, setIsLoading] = useState<boolean>(() => {
    if (!boardId) return false;
    const cached = cache.get(boardId);
    return !cached || Date.now() - cached.fetchedAt >= CACHE_TTL_MS;
  });
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!boardId) return;
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    fetchWithCache(boardId)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setIsLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e : new Error(String(e)));
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [boardId]);

  return {
    preset: data?.preset_name ?? null,
    isLoading,
    error,
    has: (flag: string) => {
      // Fail-open: render-through when data is unavailable.
      if (!data) return true;
      const value = getNested(data.flags, flag);
      // Absent flag = default True (backward compat — matches backend).
      if (value === undefined) return true;
      return Boolean(value);
    },
  };
}
