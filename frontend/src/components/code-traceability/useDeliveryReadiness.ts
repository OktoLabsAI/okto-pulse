import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isDeliveryReadiness, isPersistedReadinessProjection } from '@/types/delivery-readiness';
import type { DeliveryReadinessState } from './DeliveryReadinessView';

export function useDeliveryReadiness(boardId: string, specId: string, cardId?: string, revision?: string | number,
  enabled = true, mode: 'diagnostic' | 'projection' = 'diagnostic'): DeliveryReadinessState {
  const api = useDashboardApi();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const changed = () => setRefresh(value => value + 1);
    window.addEventListener('pulse:delivery-evidence-changed', changed);
    window.addEventListener('focus', changed);
    return () => {
      window.removeEventListener('pulse:delivery-evidence-changed', changed);
      window.removeEventListener('focus', changed);
    };
  }, []);
  const key = JSON.stringify([boardId, specId, cardId, revision, refresh, mode]);
  const [result, setResult] = useState<{ key: string; state: DeliveryReadinessState }>();
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setResult({ key, state: { loading: true } });
    void (async () => {
      let backoff = 1000;
      while (!controller.signal.aborted) {
        if (document.visibilityState !== 'visible') {
          await new Promise(resolve => setTimeout(resolve, 500));
          continue;
        }
        try {
          if (mode === 'projection') {
            const projected = await api.getPersistedReadinessProjection(boardId, specId,
              cardId ? { cardId } : undefined, controller.signal);
            if (!isPersistedReadinessProjection(projected) || projected.scope.board_id !== boardId
              || projected.scope.spec_id !== specId || projected.scope.card_id !== (cardId ?? null))
              throw new Error('Readiness projection unavailable or incompatible.');
            const summary = projected.summaries.find(row => row.card_id === (cardId ?? null));
            if (!summary && projected.projection.freshness === 'current') throw new Error('Readiness scope is unavailable.');
            if (!controller.signal.aborted) setResult({ key, state: { persisted: {
              summary, freshness: summary ? projected.projection.freshness : 'pending_initial',
              generationId: projected.projection.generation_id,
              errorCode: projected.projection.error_code } } });
            if (!['updating', 'pending_initial'].includes(projected.projection.freshness)) return;
            await new Promise(resolve => setTimeout(resolve, 500));
            continue;
          }
          const value = await api.getDeliveryReadiness(boardId, specId, cardId, controller.signal);
          if (!isDeliveryReadiness(value)) throw new Error('Readiness contract unavailable or incompatible.');
          if (value.scope.board_id !== boardId || value.scope.spec_id !== specId || value.scope.card_id !== (cardId ?? null)) throw new Error('Readiness scope mismatch.');
          if (!controller.signal.aborted) setResult({ key, state: { value } });
          return;
        } catch (error) {
          if (!controller.signal.aborted) setResult({ key, state: { error: error instanceof Error ? error.message : 'Readiness could not be loaded.' } });
          if (mode === 'diagnostic') return;
          await new Promise(resolve => setTimeout(resolve, backoff));
          backoff = Math.min(5000, backoff * 2);
        }
      }
    })();
    return () => controller.abort();
  }, [api, boardId, specId, cardId, key, enabled, mode]);
  return !enabled ? {} : result?.key === key ? result.state : { loading: true };
}
