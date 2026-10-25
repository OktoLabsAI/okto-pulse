import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isDeliveryReadiness, isPersistedReadinessProjection } from '@/types/delivery-readiness';
import type { DeliveryReadinessState } from './DeliveryReadinessView';
import { isRetriableReadinessError, ReadinessContractError } from './readinessTransport';

export function useDeliveryReadiness(boardId: string, specId: string, cardId?: string, revision?: string | number,
  enabled = true, mode: 'diagnostic' | 'projection' = 'diagnostic'): DeliveryReadinessState {
  const api = useDashboardApi();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const changed = () => setRefresh(value => value + 1);
    const visible = () => { if (document.visibilityState === 'visible') changed(); };
    window.addEventListener('pulse:delivery-evidence-changed', changed);
    window.addEventListener('focus', changed);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('pulse:delivery-evidence-changed', changed);
      window.removeEventListener('focus', changed);
      document.removeEventListener('visibilitychange', visible);
    };
  }, []);
  const scopeKey = JSON.stringify([boardId, specId, cardId, mode]);
  const [result, setResult] = useState<{ api: typeof api; key: string; state: DeliveryReadinessState }>();
  useEffect(() => {
    if (!enabled) { setResult(undefined); return; }
    const controller = new AbortController();
    setResult(previous => ({ api, key: scopeKey, state:
      previous?.api === api && previous.key === scopeKey && mode === 'projection' && previous.state.persisted?.summary
        ? { persisted: { ...previous.state.persisted, freshness: 'updating' } } : { loading: true } }));
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
              throw new ReadinessContractError('Readiness projection unavailable or incompatible.');
            const summary = projected.summaries.find(row => row.card_id === (cardId ?? null));
            if (!summary && projected.projection.freshness === 'current') throw new ReadinessContractError('Readiness scope is unavailable.');
            if (!controller.signal.aborted) setResult({ api, key: scopeKey, state: { persisted: {
              summary, freshness: summary ? projected.projection.freshness : 'pending_initial',
              generationId: projected.projection.generation_id,
              errorCode: projected.projection.error_code } } });
            backoff = 1000;
            if (!['updating', 'pending_initial'].includes(projected.projection.freshness)) return;
            await new Promise(resolve => setTimeout(resolve, 500));
            continue;
          }
          const value = await api.getDeliveryReadiness(boardId, specId, cardId, controller.signal);
          if (!isDeliveryReadiness(value)) throw new ReadinessContractError('Readiness contract unavailable or incompatible.');
          if (value.scope.board_id !== boardId || value.scope.spec_id !== specId || value.scope.card_id !== (cardId ?? null)) throw new ReadinessContractError('Readiness scope mismatch.');
          if (!controller.signal.aborted) setResult({ api, key: scopeKey, state: { value } });
          return;
        } catch (error) {
          if (!controller.signal.aborted) setResult(previous => {
            const retained = mode === 'projection' && isRetriableReadinessError(error)
              && previous?.api === api && previous.key === scopeKey && previous.state.persisted?.summary;
            return { api, key: scopeKey, state: retained
              ? { persisted: { ...previous.state.persisted!, freshness: 'unavailable', errorCode: 'transport_error' } }
              : { error: error instanceof Error ? error.message : 'Readiness could not be loaded.' } };
          });
          if (mode === 'diagnostic' || !isRetriableReadinessError(error)) return;
          await new Promise(resolve => setTimeout(resolve, backoff));
          backoff = backoff === 1000 ? 2000 : 5000;
        }
      }
    })();
    return () => controller.abort();
  }, [api, boardId, specId, cardId, scopeKey, revision, refresh, enabled, mode]);
  return !enabled ? {} : result?.api === api && result.key === scopeKey ? result.state : { loading: true };
}
