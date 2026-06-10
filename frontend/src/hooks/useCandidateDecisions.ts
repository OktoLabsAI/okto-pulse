/**
 * Hook that fetches candidate decisions for the review panel
 * (KG-03A.6 / api_2d0d274d).
 *
 * Mirrors useCognitivePendingBadges:
 *   - ONE HTTP request per (boardId, status, sourceRef) change.
 *   - AbortController cancels in-flight requests.
 *   - Errors propagate through ``error``.
 *
 * The hook is purely a read-only adapter over GET
 * /api/v1/kg/cognitive-pending/candidate-decisions. Mutations belong to
 * the command endpoint (KG-03A.5) — call
 * ``submitCandidateDecisionCommand`` from the panel and then invoke
 * ``refresh()`` to re-read the list.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  type CandidateDecisionItem,
  type CandidateDecisionCounts,
  type CandidateDecisionStatus,
  listCandidateDecisions,
} from '@/services/candidate-decisions-api';

const EMPTY_COUNTS: CandidateDecisionCounts = {
  proposed: 0,
  promoted: 0,
  linked: 0,
  dismissed: 0,
  no_action_required: 0,
  total: 0,
};

export interface UseCandidateDecisionsResult {
  items: CandidateDecisionItem[];
  counts: CandidateDecisionCounts;
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

export interface UseCandidateDecisionsOptions {
  status?: CandidateDecisionStatus | null;
  sourceRef?: string | null;
  limit?: number;
}

export function useCandidateDecisions(
  boardId: string | null,
  options: UseCandidateDecisionsOptions = {},
): UseCandidateDecisionsResult {
  const [items, setItems] = useState<CandidateDecisionItem[]>([]);
  const [counts, setCounts] =
    useState<CandidateDecisionCounts>(EMPTY_COUNTS);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const abortRef = useRef<AbortController | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const status = options.status ?? null;
  const sourceRef = options.sourceRef ?? null;
  const limit = options.limit ?? 100;

  useEffect(() => {
    abortRef.current?.abort();
    if (!boardId) {
      setItems([]);
      setCounts(EMPTY_COUNTS);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    listCandidateDecisions(
      boardId,
      { status, sourceRef, limit },
      controller.signal,
    )
      .then((resp) => {
        setItems(resp.items);
        setCounts(resp.counts);
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
  }, [boardId, status, sourceRef, limit, reloadToken]);

  const refresh = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return { items, counts, loading, error, refresh };
}
