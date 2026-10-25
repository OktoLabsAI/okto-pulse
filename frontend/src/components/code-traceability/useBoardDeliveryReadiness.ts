import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardSummary } from '@/types';
import { isPersistedReadinessProjection } from '@/types/delivery-readiness';
import { isDeliveryReadiness } from '@/types/delivery-readiness';
import { readinessReadMode } from '@/lib/readinessMode';
import type { DeliveryReadinessState } from './DeliveryReadinessView';

/** One indexed generation read per visible Spec; delayed projections are polled. */
export function useBoardDeliveryReadiness(boardId: string, cards: CardSummary[], enabled: boolean, refreshKey: string): Record<string, DeliveryReadinessState> {
  const api = useDashboardApi();
  const mode = readinessReadMode();
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener('focus', refresh);
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('pulse:delivery-evidence-changed', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', visible); window.removeEventListener('pulse:delivery-evidence-changed', refresh); };
  }, []);
  const [result, setResult] = useState<{ api: typeof api; cards: CardSummary[]; boardId: string; refreshKey: string; revision: number; values: Record<string, DeliveryReadinessState> }>();
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const values: Record<string, DeliveryReadinessState> = Object.fromEntries(cards.map(card => [card.id,
      card.spec_id ? { loading: true } : { error: 'No Spec scope is available for this card.' }]));
    const publish = () => { if (!controller.signal.aborted) setResult({ api, cards, boardId, refreshKey, revision, values: { ...values } }); };
    publish();
    const specs = [...new Set(cards.map(card => card.spec_id).filter((id): id is string => !!id))];
    const pending = new Set<string>();
    const inFlight = new Set<string>();
    const retryAt = new Map<string, number>();
    const retryDelay = new Map<string, number>();
    async function loadSpec(specId: string) {
      if (controller.signal.aborted || inFlight.has(specId)) return;
      inFlight.add(specId);
      const selected = cards.filter(card => card.spec_id === specId);
      try {
        if (mode === 'legacy') {
          const legacy = await api.getDeliveryReadinessSummary(boardId, specId, controller.signal);
          if (!isDeliveryReadiness(legacy.delivery_readiness)
            || legacy.delivery_readiness.scope.board_id !== boardId
            || legacy.delivery_readiness.scope.spec_id !== specId)
            throw new Error('Legacy readiness unavailable or incompatible.');
          const byCard = new Map(legacy.cards.map(row => [row.card_id, row.delivery_readiness]));
          for (const card of selected) {
            const value = byCard.get(card.id);
            values[card.id] = value && isDeliveryReadiness(value) ? { value }
              : { error: 'Readiness card scope is unavailable.' };
          }
          pending.delete(specId);
          return;
        }
        const data = await api.getPersistedReadinessProjection(boardId, specId, undefined, controller.signal);
        if (!isPersistedReadinessProjection(data) || data.scope.board_id !== boardId || data.scope.spec_id !== specId
          || data.scope.card_id !== null) throw new Error('Readiness projection unavailable or incompatible.');
        const rows = new Map(data.summaries.map(row => [row.card_id, row]));
        if (rows.size !== data.summaries.length) throw new Error('Readiness card scope is ambiguous.');
        for (const card of selected) {
          const summary = rows.get(card.id);
          if (!summary && data.projection.freshness === 'current') throw new Error('Readiness card scope is unavailable.');
          values[card.id] = { persisted: { summary, freshness: summary ? data.projection.freshness : 'pending_initial',
            generationId: data.projection.generation_id, errorCode: data.projection.error_code } };
        }
        if (data.projection.freshness === 'updating' || data.projection.freshness === 'pending_initial') pending.add(specId);
        else pending.delete(specId);
        retryAt.delete(specId);
        retryDelay.delete(specId);
      } catch (error) {
        if (controller.signal.aborted) return;
        for (const card of selected) values[card.id] = { error: error instanceof Error ? error.message : 'Readiness could not be loaded.' };
        pending.add(specId);
        const delay = Math.min(5000, (retryDelay.get(specId) ?? 500) * 2);
        retryDelay.set(specId, delay);
        retryAt.set(specId, Date.now() + delay);
      } finally {
        inFlight.delete(specId);
        publish();
      }
    }
    let next = 0;
    async function worker() {
      while (next < specs.length && !controller.signal.aborted) {
        const specId = specs[next++];
        await loadSpec(specId);
      }
    }
    void Promise.all(Array.from({ length: Math.min(3, specs.length) }, worker));
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      for (const specId of [...pending].filter(id => !inFlight.has(id) && (retryAt.get(id) ?? 0) <= Date.now()).slice(0, 3)) {
        void loadSpec(specId);
      }
    }, 500);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [api, boardId, cards, enabled, refreshKey, revision, mode]);
  if (!enabled) return {};
  if (result?.api === api && result.cards === cards && result.boardId === boardId && result.refreshKey === refreshKey && result.revision === revision) return result.values;
  return Object.fromEntries(cards.map(card => [card.id, { loading: true }]));
}
