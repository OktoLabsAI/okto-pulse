import { useEffect, useMemo, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardSummary } from '@/types';
import type { DeliveryCompletenessState } from './DeliveryCompletenessView';

/** One projection per visible Spec, never one request per task. No persistent scores. */
export function useTaskDeliveryCompleteness(boardId: string, cards: CardSummary[], enabled: boolean, refreshKey: string): Record<string, DeliveryCompletenessState> {
  const api = useDashboardApi();
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener('focus', refresh);
    window.addEventListener('pulse:delivery-evidence-changed', refresh);
    return () => { window.removeEventListener('focus', refresh); window.removeEventListener('pulse:delivery-evidence-changed', refresh); };
  }, []);
  const tasks = useMemo(() => cards.filter(card => !card.card_type || card.card_type === 'normal'), [cards]);
  const [result, setResult] = useState<{ tasks: CardSummary[]; boardId: string; refreshKey: string; revision: number; values: Record<string, DeliveryCompletenessState> }>();
  useEffect(() => {
    if (!enabled) { setResult(undefined); return; }
    const controller = new AbortController();
    const values: Record<string, DeliveryCompletenessState> = {};
    for (const card of tasks) values[card.id] = card.spec_id ? { loading: true } : { value: { percent: null, completed: 0, total: 0, reason: 'scope_missing' } };
    const publish = () => { if (!controller.signal.aborted) setResult({ tasks, boardId, refreshKey, revision, values: { ...values } }); };
    publish();
    const specs = [...new Set(tasks.map(card => card.spec_id).filter((id): id is string => !!id))];
    let next = 0;
    async function worker() {
      while (next < specs.length && !controller.signal.aborted) {
        const specId = specs[next++];
        try {
          const data = await api.getDeliveryEvidence(boardId, specId, controller.signal);
          if (data.board_id !== boardId || data.spec_id !== specId) throw new Error('Delivery scope mismatch');
          for (const card of tasks.filter(card => card.spec_id === specId)) values[card.id] = { value: data.per_card?.find(row => row.card_id === card.id)?.delivery_completeness };
        } catch {
          for (const card of tasks.filter(card => card.spec_id === specId)) values[card.id] = {};
        }
        publish();
      }
    }
    void Promise.all(Array.from({ length: Math.min(3, specs.length) }, worker));
    return () => controller.abort();
  }, [api, boardId, tasks, enabled, refreshKey, revision]);
  if (!enabled) return {};
  if (result?.tasks === tasks && result.boardId === boardId && result.refreshKey === refreshKey && result.revision === revision) return result.values;
  return Object.fromEntries(tasks.map(card => [card.id, { loading: true }]));
}
