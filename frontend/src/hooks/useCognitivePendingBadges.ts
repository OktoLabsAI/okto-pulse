/**
 * Hook that batches cognitive-pending badge state for a list of
 * source_refs (KG-03.6 / api_28a22fec).
 *
 * Invariants the implementation enforces:
 *
 *   - ONE HTTP request per (boardId, deduped sourceRefs, kgGenerationId)
 *     change. The hook never fans out per-card requests.
 *   - Empty source_refs short-circuits without an HTTP call.
 *   - Errors propagate via the returned ``error`` so the consumer can
 *     either hide the badge surface or render a fallback.
 *   - AbortController cancels in-flight requests when source_refs or
 *     boardId change.
 *
 * Cognitive mutation never flows through this hook. The badge is a
 * read-only signal sourced from the REST endpoint.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  getKGCognitivePendingBadges,
  type KGCognitivePendingBadgesResponse,
  type KGCognitivePendingBadgeView,
} from '@/services/kg-health-api';

export interface UseCognitivePendingBadgesResult {
  badges: Record<string, KGCognitivePendingBadgeView>;
  selectedKgGenerationId: string | null;
  eligibleEntityTypes: string[];
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

export function useCognitivePendingBadges(
  boardId: string | null,
  sourceRefs: string[],
  kgGenerationId: string | null = null,
): UseCognitivePendingBadgesResult {
  const dedupedRefs = useMemo(
    () =>
      Array.from(new Set(sourceRefs.filter((ref): ref is string => !!ref))),
    [sourceRefs],
  );
  const refsKey = dedupedRefs.join('|');
  const [data, setData] = useState<KGCognitivePendingBadgesResponse | null>(
    null,
  );
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const abortRef = useRef<AbortController | null>(null);
  const reloadTokenRef = useRef<number>(0);

  useEffect(() => {
    abortRef.current?.abort();
    if (!boardId || dedupedRefs.length === 0) {
      setData(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    getKGCognitivePendingBadges(
      boardId,
      dedupedRefs,
      { kgGenerationId },
      controller.signal,
    )
      .then((resp) => {
        setData(resp);
        setLoading(false);
      })
      .catch((err) => {
        if ((err as DOMException)?.name === 'AbortError') return;
        setError(err as Error);
        setLoading(false);
      });
    return () => {
      controller.abort();
    };
    // refsKey + kgGenerationId + boardId + reloadTokenRef.current
    // re-trigger the effect. refsKey is the stable join of dedupedRefs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, refsKey, kgGenerationId, reloadTokenRef.current]);

  return {
    badges: data?.badges ?? {},
    selectedKgGenerationId: data?.selected_kg_generation_id ?? null,
    eligibleEntityTypes: data?.eligible_entity_types ?? [],
    loading,
    error,
    refresh: () => {
      reloadTokenRef.current += 1;
    },
  };
}
