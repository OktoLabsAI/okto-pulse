import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardDeliveryEvidenceInput, DeliveryEvidenceInput, DeliveryEvidenceProjection } from '@/types/delivery-evidence';

interface Props {
  boardId: string;
  specId: string;
  canRecord?: boolean;
  canTest?: boolean;
  canCreateWaiver?: boolean;
  canClearWaiver?: boolean;
  onChanged?: () => void;
}
const field = 'w-full rounded border border-slate-300 bg-white p-2 text-sm dark:border-slate-600 dark:bg-slate-900';

export function DeliveryEvidencePanel({ boardId, specId, canRecord = false, canTest = false, canCreateWaiver = false, canClearWaiver = false, onChanged }: Props) {
  const api = useDashboardApi();
  const [data, setData] = useState<DeliveryEvidenceProjection | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [refs, setRefs] = useState<string[]>([]);
  const [kind, setKind] = useState<'implementation' | 'test' | 'waiver'>(canRecord ? 'implementation' : canTest ? 'test' : 'waiver');
  const [choice, setChoice] = useState('');
  const [testedIds, setTestedIds] = useState<string[]>([]);
  const [phase, setPhase] = useState<'implementation' | 'test'>('implementation');
  const [reason, setReason] = useState('');
  const [revocationReason, setRevocationReason] = useState('');
  const identity = `${boardId}:${specId}`;
  const identityRef = useRef(identity);
  const replayRef = useRef<{ payload: string; key: string } | null>(null);
  identityRef.current = identity;
  const canCreateAnyRecord = canRecord || canTest || canCreateWaiver;
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(''); setRefs([]); setChoice(''); setTestedIds([]);
    api.getDeliveryEvidence(boardId, specId, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery proof could not be loaded.'); });
    return () => controller.abort();
  }, [api, boardId, specId, reload]);

  async function submit(recordId?: string) {
    const justification = (recordId ? revocationReason : reason).trim();
    if (!data || busy || !justification) return;
    if ((recordId && !canClearWaiver) || (!recordId && kind === 'waiver' && !canCreateWaiver) || (!recordId && kind === 'test' && !canTest) || (!recordId && kind === 'implementation' && !canRecord)) return;
    const originalIdentity = identity;
    const selected = data.candidates.find(item => `${item.card_id}:${item.id}` === choice);
    const cardInput: CardDeliveryEvidenceInput = {
      expected_card_version: selected?.card_version ?? 1, expected_spec_edition: data.edition,
      idempotency_key: crypto.randomUUID(),
      // Card-scoped surface rejects waivers by contract shape; the waiver
      // branch is routed to legacyInput below and never reaches cardInput.kind.
      kind: (recordId ? 'revoke' : kind === 'waiver' ? 'revoke' : kind) as CardDeliveryEvidenceInput['kind'],
      obligation_refs: recordId ? [] : refs, justification,
      ...(recordId ? { record_id: recordId } : {
        ...(kind === 'implementation' ? { execution_id: selected?.id } : { scenario_id: selected?.id, implementation_ids: testedIds }),
      }),
    };
    // Waivers AND their revocations stay on the legacy spec-rollup surface
    // (human-only, BR-3): there is no card anchor for a waiver.
    const useLegacySurface = Boolean(recordId) || kind === 'waiver';
    const legacyInput: DeliveryEvidenceInput | null = useLegacySurface ? {
      expected_edition: data.edition, expected_version: data.version,
      idempotency_key: cardInput.idempotency_key,
      kind: recordId ? 'revoke' : 'waiver',
      obligation_refs: recordId ? [] : refs, justification,
      ...(recordId ? { record_id: recordId } : { phase }),
    } : null;
    const payload = JSON.stringify({ ...cardInput, idempotency_key: '', board_id: boardId, spec_id: specId });
    if (replayRef.current?.payload === payload) cardInput.idempotency_key = replayRef.current.key;
    else replayRef.current = { payload, key: cardInput.idempotency_key };
    setBusy(true); setError('');
    try {
      if (legacyInput) await api.recordDeliveryEvidence(boardId, specId, legacyInput);
      else if (!selected) throw new Error('Select an accepted current proof first.');
      else await api.recordCardDeliveryEvidence(boardId, selected.card_id, specId, cardInput);
      if (identityRef.current !== originalIdentity) return;
      replayRef.current = null;
      if (recordId) setRevocationReason(''); else setReason('');
      setReload(v => v + 1); onChanged?.();
    } catch (err) { if (identityRef.current === originalIdentity) setError(err instanceof Error ? err.message : 'Delivery evidence was not recorded.'); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4" aria-label="Delivery evidence">
    <div className="flex items-start justify-between gap-4"><div>
      <h3 className="font-semibold">Delivery evidence</h3>
      <p className="text-sm text-slate-500 dark:text-slate-400">What was implemented, and which test verified it? This is separate from the planning Code Evidence Matrix.</p>
    </div><button type="button" onClick={() => setReload(v => v + 1)} disabled={busy} className="rounded border px-3 py-1">Refresh</button></div>
    <details className="rounded border p-3 text-sm"><summary className="cursor-pointer font-medium">How to complete delivery proof</summary>
      <ol className="mt-2 list-decimal space-y-2 pl-5"><li>On a task or bug card, submit an accepted execution receipt for the committed files and complete the card. Then associate it with the obligations below.</li><li>Execute a scenario linked to a <strong>test card</strong>, record its authenticated passing result and complete the test card. Associate that run with the implementation records actually tested.</li><li>Refresh this matrix before completing the Spec. Changed requirements, a new implementation, failed reruns or revoked receipts can invalidate proof.</li></ol>
      <p className="mt-2">Skip flags do not waive delivery. An authorized human can justify a separate implementation or verification exemption. Exemption is not a passing test. For a non-code obligation with no implementation, explicitly decide both phases.</p>
    </details>
    {error && <p role="alert" className="rounded border border-red-500 p-3 text-red-500">{error}</p>}
    {!data && !error && <p role="status">Loading delivery proof…</p>}
    {data && <>
      <p role="status" className={`rounded border p-3 ${data.allowed ? 'border-emerald-500' : 'border-amber-500'}`}>
        {data.allowed ? 'Delivery proof complete' : 'Delivery proof incomplete'} · Edition {data.edition}. {data.status === 'done' ? 'This completed Spec has not been reopened. Missing proof may be recorded retrospectively.' : 'Both columns must be satisfied before this Spec can move to Done.'}
      </p>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th scope="col">Obligation</th><th scope="col">Implementation · task/bug</th><th scope="col">Verification · test card</th></tr></thead><tbody>
        {data.rows.map(row => { const ref = row.obligation.binding.obligation_ref; return <tr key={ref} className="border-t align-top">
          <td className="p-2"><label className="flex gap-2">{canCreateAnyRecord && <input type="checkbox" aria-label={`Select ${ref}`} checked={refs.includes(ref)} onChange={e => setRefs(e.target.checked ? [...refs, ref] : refs.filter(v => v !== ref))} />}<span>{row.obligation.title}<code className="block text-xs text-slate-500">{ref}</code></span></label></td>
          <td className="p-2">{row.implementation_ids.length ? <details><summary className="cursor-pointer">Recorded ({row.implementation_ids.length})</summary>{row.implementation_ids.map(id => { const proof = data.implementations.find(i => i.id === id); return <div key={id} className="mt-2 max-w-sm break-words text-xs"><code className="break-all">{proof?.relative_path}{proof?.symbol ? ` · ${proof.symbol}` : ''}</code><p>{proof?.source_ref}</p><code className="break-all">{proof?.result_revision}</code><p>Task/bug: {proof?.card_id}</p><p>{proof?.explanation}</p><code className="break-all">{id}</code></div>; })}</details> : row.implementation_waiver_ids.length ? 'Explicitly waived — not implemented' : 'Missing / stale'}</td>
          <td className="p-2">{row.test_ids.length ? <details><summary className="cursor-pointer">Verified passing run ({row.test_ids.length})</summary>{row.test_ids.map(id => { const proof = data.tests?.find(t => t.id === id); return <div key={id} className="mt-2 max-w-sm break-words text-xs"><p>Test card: {proof?.card_id || 'See audit record'}</p><p>Scenario: {proof?.scenario_id || 'See audit record'}</p><code className="break-all">{id}</code></div>; })}</details> : row.test_waiver_ids.length ? 'Explicitly waived — not tested' : 'Missing / stale'}</td>
        </tr>; })}
      </tbody></table></div>
      {data.per_card && data.per_card.length > 0 && <div>
        <h4 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Per card</h4>
        <ul className="mt-2 space-y-2">
          {data.per_card.map(card => <li key={card.card_id} className="rounded border p-3">
            <div className="flex items-center justify-between gap-3">
              <span className="min-w-0"><span className="block truncate text-sm font-medium">{card.title}</span><code className="text-xs text-slate-500">{card.card_id}</code></span>
              <span className={card.card_type === 'test' ? 'text-xs text-slate-400' : card.satisfied ? 'text-xs font-medium text-emerald-600' : 'text-xs font-medium text-amber-600'}>
                {card.card_type === 'test' ? 'Test card · authenticates via passed scenario' : card.satisfied ? 'Satisfied' : 'In progress'}
              </span>
            </div>
            {card.obligations.length > 0 && <ul className="mt-2 space-y-1 text-xs">
              {card.obligations.map(ob => <li key={ob.ref} className="flex items-center justify-between gap-3"><span className="min-w-0"><span className="block truncate">{ob.title}</span><code className="text-slate-500">{ob.ref}</code></span><span className={ob.implementation_satisfied ? 'text-emerald-600' : 'text-amber-600'}>{ob.implementation_satisfied ? '✓ Implementation' : '◌ No accepted proof'}</span></li>)}
            </ul>}
          </li>)}
        </ul>
      </div>}
      {canCreateAnyRecord && <form onSubmit={e => { e.preventDefault(); void submit(); }} className="space-y-3 rounded border p-3">
        <h4 className="font-medium">Associate proof with selected obligations</h4>
        <label className="block text-sm">Record type<select className={field} value={kind} onChange={e => { setKind(e.target.value as typeof kind); setChoice(''); }}>
          {canRecord && <option value="implementation">Code delivered by task / bug</option>}{canTest && <option value="test">Passing result from test card</option>}{canCreateWaiver && <option value="waiver">Audited exemption (human authorization)</option>}
        </select></label>
        {kind !== 'waiver' ? <label className="block text-sm">Accepted current proof<select required value={choice} onChange={e => setChoice(e.target.value)} className={field}><option value="">Select a completed card's receipt…</option>{data.candidates.filter(c => c.kind === kind).map(c => <option key={`${c.card_id}:${c.id}`} value={`${c.card_id}:${c.id}`}>{c.label} · {c.card_id}</option>)}</select><span className="text-xs text-slate-500">Only eligible, completed cards appear. If empty, complete the receipt and card workflow described above.</span></label>
          : <label className="block text-sm">Exempt only this phase<select className={field} value={phase} onChange={e => setPhase(e.target.value as typeof phase)}><option value="implementation">Implementation</option><option value="test">Verification</option></select></label>}
        {kind === 'test' && <fieldset><legend className="text-sm font-medium">Which implementation records did this run test?</legend>{data.implementations.filter(i => i.current_accepted_execution && !data.rejected_record_ids.includes(i.id)).map(i => <label key={i.id} className="flex gap-2 text-sm"><input type="checkbox" checked={testedIds.includes(i.id)} onChange={e => setTestedIds(e.target.checked ? [...testedIds, i.id] : testedIds.filter(id => id !== i.id))} />{i.relative_path} @ {i.result_revision} · {i.id}</label>)}</fieldset>}
        <label className="block text-sm">Explanation / audit reason<textarea required maxLength={20000} className={field} value={reason} onChange={e => setReason(e.target.value)} placeholder="Explain how this proof covers the selected obligations, or why the exemption is appropriate." /></label>
        <button type="submit" disabled={busy || !refs.length || !reason.trim() || (kind !== 'waiver' && !choice) || (kind === 'test' && !testedIds.length)} className="rounded bg-cyan-700 px-3 py-2 text-white disabled:opacity-50">{busy ? 'Saving…' : 'Record association'}</button>
      </form>}
      <details className="rounded border p-3"><summary className="cursor-pointer">Audit history ({data.records.length})</summary>{canClearWaiver && <label className="mt-3 block text-sm">Revocation audit reason<textarea required maxLength={20000} className={field} value={revocationReason} onChange={e => setRevocationReason(e.target.value)} placeholder="Explain why this record must be revoked. The historical record is preserved." /></label>}{data.records.map(r => <article key={r.id} className="mt-2 break-words border-t pt-2 text-sm"><strong>{r.kind}{r.revoked ? ' · revoked' : ''}</strong> · {r.actor_id} · {r.created_at}<p>{r.payload.justification}</p><code className="text-xs">{r.id}</code>{canClearWaiver && !r.revoked && r.kind !== 'revoke' && <button type="button" disabled={busy || !revocationReason.trim()} title="Enter a revocation audit reason above. Revocation preserves the historical record." onClick={() => void submit(r.id)} className="ml-2 text-red-500">Revoke</button>}</article>)}</details>
    </>}
  </section>;
}
