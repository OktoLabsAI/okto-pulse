import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardLedgerPage } from '@/types/delivery-evidence';

type Scope = { boardId: string; cardId: string; specId: string; edition: number };

export function CardLedgerPanel(props: Scope) {
  const [edition, setEdition] = useState(props.edition);
  return <section aria-label="Delivery record history" className="space-y-2 border-t pt-2">
    <label>History edition <input aria-label="History edition" type="number" min={1} max={props.edition} value={edition}
      onChange={event => { const value = Number(event.target.value); if (Number.isInteger(value) && value >= 1 && value <= props.edition) setEdition(value); }} /></label>
    <LedgerPage key={`${props.boardId}:${props.cardId}:${props.specId}:${edition}`} {...props} edition={edition} />
  </section>;
}

function LedgerPage({ boardId, cardId, specId, edition }: Scope) {
  const api = useDashboardApi();
  const [page, setPage] = useState<CardLedgerPage | null>(null);
  const [detail, setDetail] = useState<CardLedgerPage['items'][number] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function read(options: { cursor?: string; recordId?: string } = {}) {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(''); setDetail(null);
    try {
      const result = await api.getCardDeliveryLedger(boardId, cardId, specId, edition, options, controller.signal);
      if (controller.signal.aborted) return;
      if (result.board_id !== boardId || result.card_id !== cardId || result.spec_id !== specId || result.edition !== edition) {
        throw new Error('History scope changed. Restart the read.');
      }
      if (options.recordId) setDetail(result.items[0] ?? null);
      else setPage(result);
    } catch (err) {
      if (!controller.signal.aborted) { setPage(null); setError(err instanceof Error ? err.message : 'History unavailable.'); }
    } finally {
      if (!controller.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }
  return <>
    <button type="button" disabled={busy} onClick={() => read()}>Browse delivery records</button>
    {error && <p role="alert">{error}</p>}
    {page && <>
      <p>{page.total} records in edition {page.edition}. {page.historical ? 'Previous edition.' : 'Current edition.'} Historical declarations do not establish current proof or completion.</p>
      {page.items.map(row => <article key={row.id} className="border-t py-2">
        <p>{row.kind} · {row.actor_id} · {row.created_at}</p><p>{row.summary}</p>
        {row.revoked && <p>Revoked; retained as history.</p>}
        <button type="button" disabled={busy} onClick={() => read({ recordId: row.id })}>Read delivery record {row.id}</button>
      </article>)}
      {page.next_cursor && <button type="button" disabled={busy} onClick={() => read({ cursor: page.next_cursor! })}>Older delivery records</button>}
      {!page.next_cursor && <p>End of this edition’s delivery records.</p>}
    </>}
    {detail && <article aria-label="Delivery record detail" className="whitespace-pre-wrap border p-2">
      <p>{detail.kind} · {detail.actor_id} · {detail.created_at}</p><p>{detail.summary}</p>
      {detail.revoked && <p>Revoked; retained as history.</p>}
      {detail.payload?.contributions?.map(row => <p key={row.obligation_ref}>{row.obligation_ref}: declared {row.contribution}.</p>)}
      {detail.payload?.execution_id && <p>Referenced execution: {detail.payload.execution_id}</p>}
      {detail.payload?.scenario_id && <p>Scenario: {detail.payload.scenario_id}. Recorded result: {detail.payload.test_result || 'Unknown'}.</p>}
      {detail.payload?.progress && <><p>Declared remaining work: {detail.payload.progress.remaining}</p><p>Recovery: {detail.payload.progress.source_state.recoverability}.</p></>}
      <p>Currentness is not evaluated by this historical read. Use current delivery context before relying on a proof.</p>
    </article>}
  </>;
}
