import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { Spec } from '@/types';
import { isReadinessResponse, type ReadinessResponse } from '@/types/delivery-readiness';
import { TaskDeliveryIssuesTable } from './TaskDeliveryIssuesTable';
import { useNativeReadinessNavigation } from './useNativeReadinessNavigation';
import type { ReadinessCardLocation } from './readinessNavigation';

export function TaskDeliveryDiagnosis({ boardId, specId, cardId, spec, active, revision, generationId, onOpenReadinessCard }: {
  boardId: string; specId: string; cardId: string; spec?: Spec | null; active: boolean; revision: string; generationId?: string | null;
  onOpenReadinessCard?: (id: string, target?: ReadinessCardLocation) => void;
}) {
  const api = useDashboardApi();
  const navigation = useNativeReadinessNavigation(boardId, specId, undefined, onOpenReadinessCard);
  const scopeKey = JSON.stringify([boardId, specId, cardId, revision, generationId]);
  const [page, setPage] = useState<{ scope: string; cursor?: string; snapshot?: string }>({ scope: scopeKey });
  const cursor = page.scope === scopeKey ? page.cursor : undefined;
  const snapshot = cursor ? page.snapshot : undefined;
  const [retry, setRetry] = useState(0);
  const requestKey = JSON.stringify([scopeKey, cursor, retry]);
  const [loaded, setLoaded] = useState<{ key: string; value?: ReadinessResponse; error?: string }>();
  const value = loaded?.key === requestKey ? loaded.value : undefined;
  const error = loaded?.key === requestKey ? loaded.error : undefined;
  const needsRead = loaded?.key !== requestKey;

  useEffect(() => {
    // Opening Details reads one bounded page. Reopening the same current view
    // reuses it; failed reads require an explicit retry or modal refresh.
    if (!active || !needsRead) return;
    const controller = new AbortController();
    async function read() {
      try {
        const result = await api.getDeliveryReadinessPage(boardId, specId, { card_id: cardId, limit: 20, ...(cursor ? { cursor } : {}) }, controller.signal);
        if (controller.signal.aborted) return;
        if (!isReadinessResponse(result) || result.scope.board_id !== boardId || result.scope.spec_id !== specId || result.scope.card_id !== cardId
          || snapshot && result.snapshot_id !== snapshot
          || generationId && result.projection?.generation_id !== generationId
          || result.projection?.freshness && result.projection.freshness !== 'current')
          throw new Error('The delivery assessment changed. Use Refresh card to reload the current checks.');
        setLoaded({ key: requestKey, value: result });
      } catch (failure) {
        if (!controller.signal.aborted) setLoaded({ key: requestKey, error: failure instanceof Error ? failure.message : 'Delivery checks could not be loaded.' });
      }
    }
    void read();
    return () => controller.abort();
  }, [api, boardId, specId, cardId, active, needsRead, requestKey, cursor, snapshot, generationId]);

  function retryRead() { setPage({ scope: scopeKey }); setRetry(n => n + 1); }
  return <section aria-label="Pending work and verification" className="space-y-3">
    <h4 className="font-semibold">Pending work and verification</h4>
    {active && needsRead && <p role="status">Loading delivery checks…</p>}
    {error && <p role="alert">{error} <button type="button" className="btn btn-secondary" onClick={retryRead}>Retry delivery checks</button></p>}
    {navigation.error && <p role="alert">{navigation.error}</p>}
    {active && value && <>
      <TaskDeliveryIssuesTable key={`${scopeKey}:${value.snapshot_id}:${cursor ?? ''}`} value={value} spec={spec} onNavigate={navigation.navigator}
        onSnapshotInvalid={message => setLoaded({ key: requestKey, error: message })} />
      <div className="flex flex-wrap gap-3">
        {cursor && <button type="button" className="btn btn-secondary" onClick={() => setPage({ scope: scopeKey })}>First checks</button>}
        {value.page.has_more && <button type="button" className="btn btn-secondary"
          onClick={() => setPage({ scope: scopeKey, cursor: value.page.next_cursor!, snapshot: value.snapshot_id })}>Next checks</button>}
      </div>
    </>}
  </section>;
}
