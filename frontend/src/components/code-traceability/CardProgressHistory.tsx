import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { DeliveryProgressHistory } from '@/types/delivery-evidence';

export function CardProgressHistory({ boardId, cardId, specId, edition }: {
  boardId: string; cardId: string; specId: string; edition: number;
}) {
  const api = useDashboardApi();
  const [page, setPage] = useState<DeliveryProgressHistory | null>(null);
  const [detail, setDetail] = useState<DeliveryProgressHistory['items'][number] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => { pending.current?.abort(); }, []);

  async function read(options: { cursor?: string; recordId?: string } = {}) {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(''); setDetail(null);
    try {
      const result = await api.getCardProgressHistory(boardId, cardId, specId, options, controller.signal);
      if (controller.signal.aborted) return;
      if (result.board_id !== boardId || result.card_id !== cardId || result.spec_id !== specId || result.edition !== edition) {
        throw new Error('The card scope changed. Refresh its delivery context.');
      }
      if (options.recordId) setDetail(result.items[0] ?? null);
      else setPage(result);
    } catch (err) {
      if (!controller.signal.aborted) {
        setPage(null); setDetail(null);
        setError(err instanceof Error ? err.message : 'History is unavailable. Restart the read.');
      }
    } finally {
      if (!controller.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }

  return <section aria-label="Progress history" className="space-y-2 border-t pt-2">
    <button type="button" disabled={busy} onClick={() => read()}>{page ? 'Restart history' : 'Browse progress history'}</button>
    {error && <p role="alert">{error}</p>}
    {page && <>
      <p>Newest first. {page.total} saved checkpoints. Earlier pending work is not resolved by a newer note.</p>
      {page.items.map(item => <article key={item.id} className="border-t py-2">
        <p>{item.summary}</p><p>Remaining: {item.remaining}</p>
        <p>{item.actor_id} · {item.created_at} · {item.source_state.workspace_state} · {item.source_state.recoverability}</p>
        {item.revoked && <p>Revoked; retained as history.</p>}
        <button type="button" disabled={busy} onClick={() => read({ recordId: item.id })}>Read full checkpoint {item.id}</button>
      </article>)}
      {page.next_cursor && <button type="button" disabled={busy} onClick={() => read({ cursor: page.next_cursor! })}>Older checkpoints</button>}
      {!page.next_cursor && <p>End of this edition’s progress history.</p>}
    </>}
    {detail && <article aria-label="Full checkpoint" className="whitespace-pre-wrap border p-2">
      <p>{detail.summary}</p><p>Remaining: {detail.remaining}</p>
      <p>{detail.actor_id} · {detail.created_at}</p>
      {detail.revoked && <p>Revoked; retained as history.</p>}
      <p>Workspace: {detail.source_state.workspace_state}. Recovery: {detail.source_state.recoverability}.</p>
      <p>Targets: {detail.target_ids.join(', ') || 'Not declared'}</p>
      <p>Another author’s note does not verify your workspace or transfer their proof.</p>
    </article>}
  </section>;
}
