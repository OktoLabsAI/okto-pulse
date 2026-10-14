import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardSummary } from '@/types';
import { isDeliveryReadiness } from '@/types/delivery-readiness';
import type { DeliveryReadinessState } from './DeliveryReadinessView';

/** One request and shared backend evaluation per visible Spec, for all Card types. */
export function useBoardDeliveryReadiness(boardId: string, cards: CardSummary[], enabled: boolean, refreshKey: string): Record<string, DeliveryReadinessState> {
  const api = useDashboardApi();
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener('focus', refresh);
    window.addEventListener('pulse:delivery-evidence-changed', refresh);
    return () => { window.removeEventListener('focus', refresh); window.removeEventListener('pulse:delivery-evidence-changed', refresh); };
  }, []);
  const [result, setResult] = useState<{ cards: CardSummary[]; boardId: string; refreshKey: string; revision: number; values: Record<string, DeliveryReadinessState> }>();
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const values: Record<string, DeliveryReadinessState> = Object.fromEntries(cards.map(card => [card.id,
      card.spec_id ? { loading: true } : { error: 'No Spec scope is available for this card.' }]));
    const publish = () => { if (!controller.signal.aborted) setResult({ cards, boardId, refreshKey, revision, values: { ...values } }); };
    publish();
    const specs = [...new Set(cards.map(card => card.spec_id).filter((id): id is string => !!id))];
    let next = 0;
    async function worker() {
      while (next < specs.length && !controller.signal.aborted) {
        const specId = specs[next++];
        const selected = cards.filter(card => card.spec_id === specId);
        try {
          const data = await api.getDeliveryReadinessSummary(boardId, specId, controller.signal);
          if (data.contract_version !== 'delivery-readiness/v1' || data.metrics_version !== 2
            || !isDeliveryReadiness(data.delivery_readiness) || !Array.isArray(data.cards)) throw new Error('Readiness contract unavailable or incompatible.');
          const scope = data.delivery_readiness.scope;
          if (scope.board_id !== boardId || scope.spec_id !== specId || scope.card_id !== null) throw new Error('Readiness scope mismatch.');
          const rows = new Map(data.cards.map(row => [row.card_id, row.delivery_readiness]));
          if (rows.size !== data.cards.length) throw new Error('Readiness card scope is ambiguous.');
          for (const card of selected) {
            const value = rows.get(card.id);
            if (!isDeliveryReadiness(value) || value.scope.card_id !== card.id || value.scope.board_id !== boardId
              || value.scope.spec_id !== specId || value.snapshot_id !== data.delivery_readiness.snapshot_id) throw new Error('Readiness card scope is unavailable.');
            values[card.id] = { value };
          }
        } catch (error) {
          for (const card of selected) values[card.id] = { error: error instanceof Error ? error.message : 'Readiness could not be loaded.' };
        }
        publish();
      }
    }
    void Promise.all(Array.from({ length: Math.min(3, specs.length) }, worker));
    return () => controller.abort();
  }, [api, boardId, cards, enabled, refreshKey, revision]);
  if (!enabled) return {};
  if (result?.cards === cards && result.boardId === boardId && result.refreshKey === refreshKey && result.revision === revision) return result.values;
  return Object.fromEntries(cards.map(card => [card.id, { loading: true }]));
}
