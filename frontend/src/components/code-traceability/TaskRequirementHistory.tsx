import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { TaskRequirementHistory as History } from '@/types/task-requirement';
import { requirementLabels } from './taskRequirementLabels';

/** Mounted only after the human opens Details; notes and pages are bounded. */
export function TaskRequirementHistory({ boardId, specId, cardId }: { boardId: string; specId: string; cardId: string }) {
  const api = useDashboardApi();
  const [edition, setEdition] = useState<number>();
  const [editionDraft, setEditionDraft] = useState('');
  const [before, setBefore] = useState<number>();
  const [recordId, setRecordId] = useState<string>();
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError('');
    api.getTaskRequirementHistory(boardId, specId, cardId, { edition, beforeRevision: before, recordId }, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        if (value.contract_version !== 'task-requirement-history/v1' || value.board_id !== boardId
          || value.spec_id !== specId || value.card_id !== cardId || (edition !== undefined && value.edition !== edition)) {
          throw new Error('Decision history could not be confirmed.');
        }
        setData(value);
      }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Decision history unavailable.'); });
    return () => controller.abort();
  }, [api, boardId, specId, cardId, edition, before, recordId, retry]);
  return <div className="space-y-3 text-sm">
    <p>Historical decisions do not establish the current requirement. Current requirements are shown above.</p>
    {error && <><p role="alert">{error}</p><button type="button" className="btn btn-secondary" onClick={() => setRetry(value => value + 1)}>Retry decision history</button></>}
    {!data && !error && <p role="status">Loading decision history…</p>}
    {data && <>
      <p>Edition {data.edition} · {data.total} recorded decision{data.total === 1 ? '' : 's'}</p>
      <div className="flex flex-wrap items-center gap-2">
        <label>Edition <input type="number" className="w-24 rounded border bg-transparent p-1" min={1} max={data.current_edition}
          value={editionDraft} onChange={event => setEditionDraft(event.target.value)} placeholder={String(data.edition)} /></label>
        <button type="button" className="btn btn-secondary" disabled={!Number.isInteger(Number(editionDraft)) || Number(editionDraft) < 1 || Number(editionDraft) > data.current_edition}
          onClick={() => { setEdition(Number(editionDraft)); setBefore(undefined); setRecordId(undefined); }}>View edition</button>
      </div>
      {!data.items.length && <p>No recorded decisions for this edition.</p>}
      <ol className="space-y-3">{data.items.map(item => <li key={item.id} className="space-y-1 rounded border p-3">
        <p>{requirementLabels[item.previous_decision]} → {requirementLabels[item.decision]}</p>
        <p>Recorded by {item.actor_id} · <time dateTime={item.recorded_at}>{new Date(item.recorded_at).toLocaleString()}</time></p>
        <p className="whitespace-pre-wrap">{item.note || 'No note provided.'}{item.note_truncated ? '…' : ''}</p>
        {item.note_truncated && <button type="button" className="btn btn-secondary" onClick={() => { setBefore(undefined); setRecordId(item.id); }}>Read full note</button>}
      </li>)}</ol>
      {data.next_before_revision !== null && <button type="button" className="btn btn-secondary" onClick={() => setBefore(data.next_before_revision!)}>Older decisions</button>}
      {(before !== undefined || recordId !== undefined) && <button type="button" className="btn btn-secondary" onClick={() => { setBefore(undefined); setRecordId(undefined); }}>Newest decisions</button>}
    </>}
  </div>;
}
