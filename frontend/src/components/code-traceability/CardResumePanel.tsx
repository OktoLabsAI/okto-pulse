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
      {data.verification_plan && <>
        {!data.verification_plan.complete && <p>The related verification plan is unavailable or incomplete.</p>}
        {data.verification_plan.items.map(row => <article key={row.scenario_id}>
          <p>Planned scenario: {row.scenario_id} · {row.method || 'Unknown method'} · criteria {row.criterion_ids.join(', ')}</p>
          <p>Responsible Test Cards: {row.test_card_ids.join(', ') || 'Missing'}.</p>
          {!!row.blockers.length && <p>Planning blockers: {row.blockers.join(', ')}</p>}
          {row.links_truncated && <p>This scenario’s references are shortened.</p>}
        </article>)}
      </>}
      <p>{data.tests.total_exact === false ? 'At least ' : ''}{data.tests.total} test records {data.tests.scope === 'card_and_related_obligations' ? 'related to this Card’s obligations' : 'owned by this Card'}.</p>
      {data.tests.total_exact === false && <p>The related test population is incomplete or unavailable; this does not mean zero pending verification.</p>}
      {data.tests.items.map(test => <article key={test.record_id}>
        <p>{test.scenario_id}: {test.result}; authentication {test.current_verified_run ? 'current' : 'not current'}.</p>
        {test.observes_this_card === false && <p>This record does not reference an implementation binding owned by this Card.</p>}
      </article>)}
      {[...new Set(data.tests.items.map(test => test.card_id).filter((id): id is string => !!id && id !== cardId))].map(id => <details key={id}><summary>History of Test Card {id}</summary>
        <CardLedgerPanel boardId={boardId} cardId={id} specId={specId} edition={edition} />
      </details>)}
      <ul>{data.targets.items.map(target => <li key={target.id}>{target.relative_path || target.id} · {target.source_ref} · Target revision {target.revision}</li>)}</ul>
      {(data.response_truncated || data.obligations.truncated || data.implementation_proofs.truncated || data.tests.truncated || data.targets.truncated || data.accumulated_impact.detail_omitted || data.verification_plan?.truncated || data.verification_plan?.test_cards_truncated) && <p>This context is shortened. Use the scoped detail reads and Spec rollup before relying on omitted facts.</p>}
    </>}
  </section>;
}
