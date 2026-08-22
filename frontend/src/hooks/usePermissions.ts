/**
 * usePermissions — React hook that exposes the authenticated agent's
 * effective permission flags for a given board.
 *
 * Historical flags keep the render-through behavior while data is
 * unavailable.  Versioned introduced flags are fail-closed while loading, on
 * errors, and whenever the response omits the leaf.
 *
 * Cache: 60s staleTime to avoid a roundtrip on every gated component.
 */

import { useEffect, useState } from 'react';

import {
  getMyPermissions,
  type PermissionsResponse,
} from '@/services/permissions-api';
import {
  INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES,
  isIntroducedPermissionLeaf,
} from '@/components/permissions/permissionLayers';

export {
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1,
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1_LEAVES,
  CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES,
  PERMISSION_INTRODUCTION_MANIFESTS,
  SKA_PERMISSION_INTRODUCTION_V1_LEAVES,
  SKB_PERMISSION_INTRODUCTION_V1_LEAVES,
} from '@/components/permissions/permissionLayers';

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
  /** True when an invalid preset lineage requires an owner decision. */
  ownerReviewRequired: boolean;
  /**
   * Check whether a flag is effectively enabled.
   *
   * Historical flags render through while data is unavailable. Introduced
   * Versioned flags are denied until the backend returns an explicit True.
   */
  has: (flag: string) => boolean;
}

/**
 * Mirror Core's two-part authorization for mutations on an existing entity.
 *
 * Creation has no existing state and must use ``has(action)`` directly. Every
 * other entity mutation requires both its exact action leaf and the current
 * ``<entity>.interact_in.<status>`` leaf. Keeping this composition next to the
 * hook prevents individual UI surfaces from accidentally checking only one
 * half of the contract.
 */
export function hasPermissionWithState(
  has: (flag: string) => boolean,
  action: string,
  entity: string,
  status: string | null | undefined,
): boolean {
  const actionAllowed = has(action);
  const stateAllowed = Boolean(status)
    && has(`${entity}.interact_in.${status}`);
  return actionAllowed && stateAllowed;
}

export function hasEffectivePermission(
  data: PermissionsResponse | null,
  flag: string,
): boolean {
  const introduced = isIntroducedPermissionLeaf(flag);
  if (!data) return !introduced;
  if (introduced && data.owner_review_required) return false;
  const value = getNested(data.flags, flag);
  if (value === undefined) return !introduced;
  if (value !== true) return false;
  const historicalAuthority =
    data.introduced_historical_authorities?.[flag]
    ?? INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES[flag];
  return (
    historicalAuthority === undefined
    || getNested(data.flags, historicalAuthority) === true
  );
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
    // Never expose a payload from the previous board while the new scope is
    // loading.  Clearing all three states also makes boardId=null a real
    // reset instead of retaining the last successful authorization snapshot.
    setData(null);
    setError(null);
    if (!boardId) {
      setIsLoading(false);
      return;
    }

    const cached = cache.get(boardId);
    if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
      setData(cached.data);
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    fetchWithCache(boardId)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setIsLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setData(null);
          setError(e instanceof Error ? e : new Error(String(e)));
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [boardId]);

  const activeData = (
    !isLoading
    && !error
    && boardId
    && data?.board_id === boardId
  )
    ? data
    : null;

  return {
    preset: activeData?.preset_name ?? null,
    isLoading,
    error,
    ownerReviewRequired: activeData?.owner_review_required ?? false,
    has: (flag: string) => hasEffectivePermission(activeData, flag),
  };
}
