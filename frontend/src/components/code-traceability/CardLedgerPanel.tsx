import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardLedgerPage } from '@/types/delivery-evidence';
import { DeliveryDisclosure, deliveryButton, deliveryError, deliverySection } from './deliveryPresentation';

type Scope = { boardId: string; cardId: string; specId: string; edition: number };

export function CardLedgerPanel(props: Scope) {
  const [edition, setEdition] = useState(props.edition);
  return <section aria-label="Delivery record history" className={deliverySection}>
    <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Delivery record history</h4>
    <label className="block max-w-md space-y-2 font-medium">
      <span className="flex items-center justify-between gap-3"><span>History edition</span><span className="rounded bg-sky-100 px-2 py-0.5 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">Edition {edition}{edition === props.edition ? ' · Current' : ''}</span></span>
      <input className="block w-full cursor-pointer accent-sky-500 disabled:cursor-default disabled:opacity-50" aria-label="History edition" aria-valuetext={`Edition ${edition}${edition === props.edition ? ', current' : ''}`} type="range" min={1} max={props.edition} step={1} value={edition} disabled={props.edition === 1}
        onChange={event => { const value = Number(event.target.value); if (Number.isInteger(value) && value >= 1 && value <= props.edition) setEdition(value); }} />
      <span className="flex justify-between text-[10px] font-normal text-gray-500 dark:text-gray-400"><span>1</span><span>{props.edition === 1 ? 'Only edition' : props.edition}</span></span>
    </label>
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
    <button type="button" className={deliveryButton} disabled={busy} onClick={() => read()}>Browse delivery records</button>
    {error && <p role="alert" className={deliveryError}>{error}</p>}
    {page && <>
      <p>{page.total} records in edition {page.edition}. {page.historical ? 'Previous edition.' : 'Current edition.'} Historical declarations do not establish current proof or completion.</p>
      {page.items.map(row => <DeliveryDisclosure key={row.id} title={row.summary} badge={row.revoked ? 'Revoked' : row.kind}>
        <p>{row.kind} · {row.actor_id} · {row.created_at}</p><p>{row.summary}</p>
        {row.revoked && <p>Revoked; retained as history.</p>}
        <button type="button" className={deliveryButton} disabled={busy} onClick={() => read({ recordId: row.id })}>Read delivery record {row.id}</button>
      </DeliveryDisclosure>)}
      {page.next_cursor && <button type="button" className={deliveryButton} disabled={busy} onClick={() => read({ cursor: page.next_cursor! })}>Older delivery records</button>}
      {!page.next_cursor && <p>End of this edition’s delivery records.</p>}
    </>}
    {detail && <article aria-label="Delivery record detail" className={`${deliverySection} whitespace-pre-wrap break-words`}>
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
