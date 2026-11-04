import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isReadinessResponse, type ReadinessResponse } from '@/types/delivery-readiness';
import { DeliveryReadinessIssues } from './DeliveryReadinessIssues';
import { useNativeReadinessNavigation } from './useNativeReadinessNavigation';
import type { ReadinessCardLocation } from './readinessNavigation';

export function TaskDeliveryDiagnosis({ boardId, specId, cardId, active, revision, onOpenReadinessCard }: {
  boardId: string; specId: string; cardId: string; active: boolean; revision: string;
  onOpenReadinessCard?: (id: string, target?: ReadinessCardLocation) => void;
}) {
  const api = useDashboardApi();
  const navigation = useNativeReadinessNavigation(boardId, specId, undefined, onOpenReadinessCard);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<ReadinessResponse>();
  const [error, setError] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [retry, setRetry] = useState(0);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string[]>([]);
  useEffect(() => { setCursor(undefined); setValue(undefined); }, [revision, boardId, specId, cardId]);
  useEffect(() => {
    if (!active || !open) return;
    const controller = new AbortController(); setBusy(true); setError('');
    api.getDeliveryReadinessPage(boardId, specId, { card_id: cardId, limit: 20, cursor }, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        if (!isReadinessResponse(result) || result.scope.board_id !== boardId || result.scope.spec_id !== specId || result.scope.card_id !== cardId)
          throw new Error('Delivery diagnosis is unavailable.');
        setValue(result);
      }).catch(e => { if (!controller.signal.aborted) { setValue(undefined); setError(e instanceof Error ? e.message : 'Diagnosis unavailable.'); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [api, boardId, specId, cardId, active, open, cursor, revision, retry]);
  return <section className="space-y-3">
    <button type="button" className="btn btn-secondary" aria-expanded={open} onClick={() => setOpen(v => !v)}>Verification and pending work</button>
    {open && <>
      {busy && <p role="status">Loading current diagnosis…</p>}
      {error && <p role="alert">{error} <button type="button" className="underline" onClick={() => { setCursor(undefined); setRetry(n => n + 1); }}>Retry diagnosis</button></p>}
      {navigation.error && <p role="alert">{navigation.error}</p>}
      {value && !busy && <>
        <DeliveryReadinessIssues value={value} onNavigate={navigation.navigator} onSnapshotInvalid={setError}
          expandedIssueIds={expanded} onIssueToggle={(id, show) => setExpanded(ids => show ? [...ids, id] : ids.filter(v => v !== id))} />
        {value.page.has_more && <button type="button" className="btn btn-secondary" onClick={() => setCursor(value.page.next_cursor!)}>Next pending items</button>}
        {cursor && <button type="button" className="btn btn-secondary" onClick={() => setCursor(undefined)}>First pending items</button>}
      </>}
    </>}
  </section>;
}
