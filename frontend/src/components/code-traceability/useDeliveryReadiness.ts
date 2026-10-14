import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isDeliveryReadiness } from '@/types/delivery-readiness';
import type { DeliveryReadinessState } from './DeliveryReadinessView';

export function useDeliveryReadiness(boardId: string, specId: string, cardId?: string, revision?: string | number, enabled = true): DeliveryReadinessState {
  const api = useDashboardApi();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const changed = () => setRefresh(value => value + 1);
    window.addEventListener('pulse:delivery-evidence-changed', changed);
    return () => window.removeEventListener('pulse:delivery-evidence-changed', changed);
  }, []);
  const key = JSON.stringify([boardId, specId, cardId, revision, refresh]);
  const [result, setResult] = useState<{ key: string; state: DeliveryReadinessState }>();
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setResult({ key, state: { loading: true } });
    void (async () => {
      try {
        const value = await api.getDeliveryReadiness(boardId, specId, cardId, controller.signal);
        if (!isDeliveryReadiness(value)) throw new Error('Readiness contract unavailable or incompatible.');
        if (value.scope.board_id !== boardId || value.scope.spec_id !== specId || value.scope.card_id !== (cardId ?? null)) throw new Error('Readiness scope mismatch.');
        if (!controller.signal.aborted) setResult({ key, state: { value } });
      } catch (error) {
        if (!controller.signal.aborted) setResult({ key, state: { error: error instanceof Error ? error.message : 'Readiness could not be loaded.' } });
      }
    })();
    return () => controller.abort();
  }, [api, boardId, specId, cardId, key, enabled]);
  return !enabled ? {} : result?.key === key ? result.state : { loading: true };
}
