import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isPersistedReadinessProjection, type PersistedReadinessProjection } from '@/types/delivery-readiness';
import { isTaskDeliverySummary } from '@/types/task-delivery';

type PageRequest = { cursor: string; generation: string };

function validTaskUnits(value: PersistedReadinessProjection, cardId: string) {
  return Array.isArray(value.units) && value.units.every(unit => unit && unit.card_id === cardId
    && ['obligation', 'scenario'].includes(unit.kind) && typeof unit.ref === 'string'
    && typeof unit.semantic_sha256 === 'string' && unit.spec_edition === value.scope.spec_edition
    && typeof unit.implementation_state === 'string' && typeof unit.verification_state === 'string')
    && value.page && Number.isInteger(value.page.offset) && value.page.offset >= 0
    && Number.isInteger(value.page.total) && value.page.total >= value.page.offset + value.units.length
    && typeof value.page.has_more === 'boolean' && (!value.page.has_more || typeof value.page.next_cursor === 'string' && !!value.page.next_cursor);
}

/** One owner for the card projection. Events coalesce; failed reads need explicit retry. */
export function useTaskDelivery(boardId: string, specId: string, cardId: string, active: boolean, revision: number, refreshRequest = 0) {
  const api = useDashboardApi();
  const [value, setValue] = useState<PersistedReadinessProjection | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const requestRefresh = useRef<(explicit?: boolean, page?: PageRequest) => void>(() => {});
  const retry = useCallback(() => requestRefresh.current(true), []);
  useEffect(() => {
    setValue(null); setError('');
  }, [api, boardId, specId, cardId]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false, again = false, failed = false, lastStart = 0;
    let requestedPage: PageRequest | undefined;
    function schedule() {
      if (controller.signal.aborted || failed || document.visibilityState === 'hidden') return;
      if (inFlight) { again = true; return; }
      if (timer) return;
      timer = setTimeout(() => { timer = undefined; void read(); }, Math.max(0, 1000 - (Date.now() - lastStart)));
    }
    async function read() {
      if (controller.signal.aborted || document.visibilityState === 'hidden') return;
      const page = requestedPage; requestedPage = undefined;
      inFlight = true; lastStart = Date.now(); setLoading(true); setError('');
      // Keep the draft and Details mounted, but never keep a stale green verdict.
      setValue(previous => previous ? { ...previous, projection: { ...previous.projection, freshness: 'updating' } } : null);
      try {
        const result = await api.getPersistedReadinessProjection(boardId, specId, { cardId, unitLimit: 20, ...(page ? { cursor: page.cursor } : {}) }, controller.signal);
        if (controller.signal.aborted) return;
        if (!isPersistedReadinessProjection(result) || !isTaskDeliverySummary(result.task_delivery)
          || result.scope.board_id !== boardId || result.scope.spec_id !== specId || result.scope.card_id !== cardId
          || result.task_delivery.board_id !== boardId || result.task_delivery.spec_id !== specId || result.task_delivery.card_id !== cardId
          || !validTaskUnits(result, cardId))
          throw new Error('Delivery is unavailable. Refresh to load a compatible assessment.');
        if (page && result.projection.generation_id !== page.generation)
          throw new Error('The assessment changed. Use Refresh card to load the current requirements.');
        setValue(result);
        again ||= ['pending_initial', 'updating'].includes(result.projection.freshness);
      } catch (e) {
        // Retain the last summary to keep a human draft mounted. The error and
        // non-current freshness prevent it from authorizing another submission.
        if (!controller.signal.aborted) { failed = true; setError(e instanceof Error ? e.message : 'Delivery could not be loaded.'); }
      } finally {
        inFlight = false;
        if (!controller.signal.aborted) { setLoading(false); if (again) { again = false; schedule(); } }
      }
    }
    requestRefresh.current = (explicit = false, page) => { if (explicit) failed = false; requestedPage = page; schedule(); };
    const visible = () => { if (document.visibilityState === 'visible') schedule(); };
    window.addEventListener('pulse:delivery-evidence-changed', schedule);
    document.addEventListener('visibilitychange', visible);
    void read();
    return () => {
      controller.abort(); clearTimeout(timer); requestRefresh.current = () => {};
      window.removeEventListener('pulse:delivery-evidence-changed', schedule);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [api, boardId, specId, cardId, active]);
  const previousRevision = useRef(revision);
  useEffect(() => {
    if (previousRevision.current !== revision) { previousRevision.current = revision; requestRefresh.current(); }
  }, [revision]);
  const previousRefreshRequest = useRef(refreshRequest);
  useEffect(() => {
    if (previousRefreshRequest.current !== refreshRequest) {
      previousRefreshRequest.current = refreshRequest;
      requestRefresh.current(true);
    }
  }, [refreshRequest]);
  return { value, error, loading, retry, refresh: useCallback(() => requestRefresh.current(), []),
    nextPage: () => {
      if (!loading && !error && value?.projection.freshness === 'current' && value.page.next_cursor && value.projection.generation_id)
        requestRefresh.current(true, { cursor: value.page.next_cursor, generation: value.projection.generation_id });
    } };
}
