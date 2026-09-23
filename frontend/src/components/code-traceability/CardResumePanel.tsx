import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { CardLedgerPanel } from './CardLedgerPanel';
import type { CardDeliveryResume } from '@/types/delivery-evidence';

export function CardResumePanel({ boardId, cardId, specId, edition }: {
  boardId: string; cardId: string; specId: string; edition: number;
}) {
  const api = useDashboardApi();
  const [data, setData] = useState<CardDeliveryResume | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function read() {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setData(null); setError('');
    try {
      const result = await api.getCardDeliveryResume(boardId, cardId, specId, controller.signal);
      if (controller.signal.aborted) return;
      if (result.board_id !== boardId || result.card_id !== cardId || result.spec_id !== specId || result.edition !== edition) {
        throw new Error('The card scope changed. Refresh its delivery context.');
      }
      setData(result);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery context is unknown. Retry the read.');
    } finally {
      if (!controller.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }
  return <section aria-label="Accumulated delivery context" className="space-y-2 border-t pt-2">
    <button type="button" disabled={busy} onClick={read}>Read accumulated delivery context</button>
    <CardLedgerPanel key={`${boardId}:${cardId}:${specId}:${edition}`} boardId={boardId} cardId={cardId} specId={specId} edition={edition} />
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>{data.title} · Edition {data.edition} · {data.status}</p>
      <p>{data.actions.record_progress ? 'Progress recording is available in this state.' : 'Progress recording is unavailable for this reader or state.'} Final transitions require their own gate checks.</p>
      <p>Workspace access and recovery are unknown. Verify your own material before continuing; another author’s proof stays attributed to that author.</p>
      {data.latest_checkpoint && <p>Latest checkpoint by {data.latest_checkpoint.actor_id}: {data.latest_checkpoint.summary}</p>}
      <p>{data.progress.total} progress checkpoints. Earlier pending work is not resolved by a later note; browse the history for details.</p>
      <p>Accumulated impact: {data.accumulated_impact.status} from {data.accumulated_impact.history_count} declarations. Impact declarations do not prove delivery.</p>
      {!data.obligations.complete && <p>Obligation coverage is unknown or incomplete.</p>}
      <ul>{data.obligations.items.map(row => <li key={row.ref}>{row.title}: implementation {row.implementation_satisfied ? 'satisfied' : 'pending'}, test {row.test_satisfied ? 'satisfied' : 'pending'}.</li>)}</ul>
      <p>{data.implementation_proofs.total} implementation records in this Card’s current delivery selection.</p>
      {data.implementation_proofs.items.map(proof => <article key={proof.record_id}>
        <p>{proof.actor_id} · {proof.relative_path} · {proof.source_ref} @ {proof.result_revision}</p>
        <p>Currently admitted obligations: {proof.current_obligation_refs.join(', ') || 'None'}.</p>
        {proof.contributions.map(row => <p key={row.obligation_ref}>{row.obligation_ref}: declared {row.declaration}.</p>)}
        {proof.declaration_origin === 'legacy_unknown' && <p>Legacy contribution declaration is unknown.</p>}
        {proof.bindings_truncated && <p>This proof’s binding list is shortened.</p>}
      </article>)}
      <p>{data.tests.total} test records owned by this Card. Consult the Spec rollup for tests owned by other Cards.</p>
      {data.tests.items.map(test => <p key={test.record_id}>{test.scenario_id}: {test.result}; authentication {test.current_verified_run ? 'current' : 'not current'}.</p>)}
      <ul>{data.targets.items.map(target => <li key={target.id}>{target.relative_path || target.id} · {target.source_ref} · Target revision {target.revision}</li>)}</ul>
      {(data.response_truncated || data.obligations.truncated || data.implementation_proofs.truncated || data.tests.truncated || data.targets.truncated || data.accumulated_impact.detail_omitted) && <p>This context is shortened. Use the scoped detail reads and Spec rollup before relying on omitted facts.</p>}
    </>}
  </section>;
}
