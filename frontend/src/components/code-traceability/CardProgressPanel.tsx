import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { CardProgressHistory } from './CardProgressHistory';
import type { CardDeliveryBatchInput, CardDeliveryBatchDraft, DeliveryPerCard } from '@/types/delivery-evidence';

export function CardProgressPanel({ boardId, specId, edition, card, canWrite, onSaved, onStage }: {
  boardId: string; specId: string; edition: number; card: DeliveryPerCard;
  canWrite: boolean; onSaved: () => void;
  onStage?: (draft: CardDeliveryBatchDraft) => void;
}) {
  const api = useDashboardApi();
  const [summary, setSummary] = useState('');
  const [remaining, setRemaining] = useState('');
  const [dirty, setDirty] = useState(false);
  const [change, setChange] = useState<'' | 'none' | 'targets' | 'unknown'>('');
  const [targets, setTargets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const writable = canWrite && ['started', 'in_progress'].includes(card.status) && !!card.card_version && card.delivery_revision !== undefined;

  async function save() {
    if (!writable || pending.current || !summary.trim() || !remaining.trim() || !change || (change === 'targets' && !targets.length)) return;
    const input: CardDeliveryBatchInput = {
      contract_version: 'card-delivery-batch/v1',
      expected_card_version: card.card_version!, expected_spec_edition: edition,
      expected_delivery_revision: card.delivery_revision!, idempotency_key: '',
      entries: [{ client_ref: 'progress', kind: 'progress', obligation_refs: [], justification: summary.trim(),
        progress: { contract_version: 'delivery-progress/v2', material_change: change, target_ids: change === 'targets' ? targets : [], remaining: remaining.trim(),
          source_state: { workspace_state: dirty ? 'dirty' : 'unknown', recoverability: dirty ? 'external_workspace' : 'unknown' } } }],
    };
    const body = JSON.stringify(input);
    if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() };
    input.idempotency_key = retry.current.key;
    pending.current = true; setBusy(true); setError('');
    try {
      if (onStage) onStage({ contract_version: input.contract_version, expected_card_version: input.expected_card_version,
        expected_spec_edition: input.expected_spec_edition, expected_delivery_revision: input.expected_delivery_revision, entries: input.entries });
      else await api.recordCardDeliveryEvidence(boardId, card.card_id, specId, input);
      if (!mounted.current) return;
      retry.current = null; setSummary(''); setRemaining(''); setDirty(false); setChange(''); setTargets([]); if (!onStage) onSaved();
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : 'Progress could not be saved. Retry with the same content.');
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  }

  return <section aria-label="Recorded progress" className="space-y-2 rounded border p-3 text-sm">
    <h3>Recorded progress</h3>
    <p>Declared work does not complete this card or satisfy delivery gates. Recovery is limited to confirmed records and material you can access.</p>
    {!onStage && card.progress?.items.map(item => <article key={item.id} className="border-t py-2">
      <p>{item.summary}</p><p>Remaining: {item.remaining}</p>
      {item.revoked && <p>Revoked by an authorized reviewer; retained as history.</p>}
      <p className="text-xs">{item.actor_id} · {item.created_at} · {item.source_state.workspace_state} · {item.source_state.recoverability}</p>
      {item.material_change && item.material_change !== 'none' && <p>Code change: {item.material_change}. Earlier observations of the affected work require renewed evidence.</p>}
      {item.change_declaration_origin === 'delivery-progress/v1' && item.material_change !== 'none' && <p>Legacy checkpoint: the change scope is inferred conservatively from its declared workspace and Targets.</p>}
      {item.text_truncated && <p>This record is shortened in the summary.</p>}
    </article>)}
    {card.progress?.truncated && <p>Showing recent records from {card.progress.total} saved checkpoints. This is an incomplete history.</p>}
    {!onStage && !!card.progress?.total && <CardProgressHistory key={`${boardId}:${card.card_id}:${specId}:${edition}:${card.delivery_revision}`} boardId={boardId} cardId={card.card_id} specId={specId} edition={edition} />}
    {error && <p role="alert">{error}</p>}
    {writable ? <div className="space-y-2">
      <label className="block">Work recorded<textarea aria-label="Work recorded" value={summary} maxLength={20000} onChange={e => setSummary(e.target.value)} disabled={busy} className="block w-full border bg-transparent" /></label>
      <label className="block">Remaining work<textarea aria-label="Remaining work" value={remaining} maxLength={8000} onChange={e => setRemaining(e.target.value)} disabled={busy} className="block w-full border bg-transparent" /></label>
      <label className="block"><input type="checkbox" checked={dirty} onChange={e => { setDirty(e.target.checked); if (e.target.checked && (!change || change === 'none')) setChange('unknown'); }} disabled={busy} /> Work is in an external dirty workspace</label>
      <label className="block">Code change in this checkpoint
        <select aria-label="Code change in this checkpoint" value={change} onChange={e => setChange(e.target.value as typeof change)} disabled={busy} className="block border bg-transparent">
          <option value="">Select the effect on code…</option>
          <option value="none">Context only — no code change</option>
          <option value="targets">Code changed for selected Targets</option>
          <option value="unknown">Code changed; affected scope is unknown</option>
        </select>
      </label>
      {change === 'targets' && <fieldset disabled={busy}><legend>Affected Targets</legend>
        {(card.progress?.target_options ?? []).map(target => <label key={target.id} className="block"><input type="checkbox" checked={targets.includes(target.id)} onChange={e => setTargets(current => e.target.checked ? [...current, target.id] : current.filter(id => id !== target.id))} /> {target.label} · {target.source_ref}</label>)}
        {card.progress?.targets_truncated && <p>The Target list is shortened. Use the scoped API for other Targets.</p>}
      </fieldset>}
      {change && change !== 'none' && <p>Earlier observations of {change === 'targets' ? 'these Targets' : 'this card’s work'} stop proving the current result until a new accepted observation. Other notes do not restore proof.</p>}
      <button type="button" onClick={save} disabled={busy || !summary.trim() || !remaining.trim() || !change || (change === 'targets' && !targets.length)}>{onStage ? 'Add progress to report draft' : 'Save progress'}</button>
    </div> : <p>Recording progress requires execution state and permission to write the card’s report.</p>}
  </section>;
}
