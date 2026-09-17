import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type {
  CardDeliveryEvidenceInput,
  DeliveryEvidenceInput,
  DeliveryEvidenceProjection,
} from '@/types/delivery-evidence';

interface Props {
  boardId: string;
  card: { id: string; card_type: string; spec_id: string };
  canRecord?: boolean;
  canTest?: boolean;
  canWaiver?: boolean;
  onChanged?: () => void;
}

const field = 'w-full rounded border border-gray-300 bg-white p-2 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100';

// Card "Delivery" tab — the per-card DoD surface (mockup sm_c3383586).
// Obligations derive from this card's links; implementation proof is recorded
// HERE against this card's accepted execution receipts. Test cards record
// authenticated passing runs verifying implementations on other cards.
// Waivers stay spec-level and human-only (BR-3): the button routes an
// authorized human to the legacy rollup surface.
export function CardDeliveryDoDPanel({ boardId, card, canRecord = false, canTest = false, canWaiver = false, onChanged }: Props) {
  const api = useDashboardApi();
  const [data, setData] = useState<DeliveryEvidenceProjection | null>(null);
  const [gateMode, setGateMode] = useState<'advisory' | 'blocking'>('blocking');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [formOpen, setFormOpen] = useState(false);
  const [refs, setRefs] = useState<string[]>([]);
  const [choice, setChoice] = useState('');
  const [testedIds, setTestedIds] = useState<string[]>([]);
  const [phase, setPhase] = useState<'implementation' | 'test'>('implementation');
  const [reason, setReason] = useState('');
  const replayRef = useRef<{ payload: string; key: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(''); setRefs([]); setChoice(''); setTestedIds([]);
    api.getDeliveryEvidence(boardId, card.spec_id, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery proof could not be loaded.'); });
    api.getBoard(boardId).then(board => {
      if (!controller.signal.aborted) setGateMode(board.settings?.delivery_evidence_gate ?? 'blocking');
    }).catch(() => {});
    return () => controller.abort();
  }, [api, boardId, card.spec_id, reload]);

  const isTest = card.card_type === 'test';
  const mine = data?.per_card?.find(entry => entry.card_id === card.id) ?? null;
  const obligations = mine?.obligations ?? [];
  const acceptedProofs = (data?.implementations ?? []).filter(i => i.card_id === card.id && i.current_accepted_execution);
  const proofFor = (ref: string) => acceptedProofs.find(p => (p.bindings ?? []).some(b => b.obligation_ref === ref));
  const unproven = obligations.filter(o => !o.implementation_satisfied);
  const canRecordKind = isTest ? canTest : canRecord;
  const candidates = (data?.candidates ?? []).filter(c => c.card_id === card.id && c.kind === (isTest ? 'test' : 'implementation'));
  const selectableRefs = isTest
    ? (data?.rows ?? []).map(row => ({ ref: row.obligation.binding.obligation_ref, title: row.obligation.title, satisfied: row.test_satisfied }))
    : obligations.map(o => ({ ref: o.ref, title: o.title, satisfied: o.implementation_satisfied }));
  const verifiableImpls = (data?.implementations ?? []).filter(i => i.current_accepted_execution && !data?.rejected_record_ids.includes(i.id));

  async function submit() {
    const justification = reason.trim();
    if (!data || busy || !justification || !refs.length) return;
    const selected = candidates.find(c => `${c.card_id}:${c.id}` === choice);
    const key = crypto.randomUUID();
    const input: CardDeliveryEvidenceInput = {
      expected_card_version: selected?.card_version ?? 1,
      expected_spec_edition: data.edition,
      idempotency_key: key,
      kind: isTest ? 'test' : 'implementation',
      obligation_refs: refs,
      justification,
      ...(isTest
        ? { scenario_id: selected?.id, implementation_ids: testedIds }
        : { execution_id: selected?.id }),
    };
    const payload = JSON.stringify({ ...input, idempotency_key: '', card_id: card.id });
    if (replayRef.current?.payload === payload) input.idempotency_key = replayRef.current.key;
    else replayRef.current = { payload, key: input.idempotency_key };
    setBusy(true); setError('');
    try {
      if (!isTest && !selected) throw new Error('Select an accepted execution receipt for this card first.');
      if (isTest && (!selected || !testedIds.length)) throw new Error('Select the passing scenario and the implementation records it verified.');
      await api.recordCardDeliveryEvidence(boardId, card.id, card.spec_id, input);
      replayRef.current = null; setReason(''); setRefs([]); setChoice(''); setTestedIds([]);
      setFormOpen(false); setReload(v => v + 1); onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delivery evidence was not recorded.');
    } finally { setBusy(false); }
  }

  async function submitWaiver() {
    const justification = reason.trim();
    if (!data || busy || !justification || !refs.length) return;
    const input: DeliveryEvidenceInput = {
      expected_edition: data.edition, expected_version: data.version,
      idempotency_key: crypto.randomUUID(), kind: 'waiver',
      obligation_refs: refs, justification, phase,
    };
    setBusy(true); setError('');
    try {
      await api.recordDeliveryEvidence(boardId, card.spec_id, input);
      setReason(''); setRefs([]); setFormOpen(false); setReload(v => v + 1); onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The waiver was not recorded.');
    } finally { setBusy(false); }
  }

  return <section className="space-y-4" aria-label="Delivery evidence (Definition of Done)">
    {error && <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/70 dark:bg-red-950/25 dark:text-red-300">{error}</p>}
    {!data && !error && <p role="status" className="text-sm text-gray-500">Loading card delivery obligations…</p>}
    {data && <>
      <div className="rounded-md border border-gray-200 p-4 dark:border-gray-800">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">Delivery Evidence (Definition of Done)</h3>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${unproven.length ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'}`} data-testid="dod-gate-pill">
            {gateMode === 'advisory' ? 'Advisory' : 'Blocking'} · {unproven.length} of {obligations.length || selectableRefs.length} unproven
          </span>
        </div>
        {obligations.length === 0 && !isTest && (
          <p className="text-sm text-gray-500 dark:text-gray-400">No obligations derived for this card{mine ? '' : ' (unlinked cards receive the fallback card:<id> obligation once the spec ledger is read)'}. Link the card to spec entities to derive its DoD.</p>
        )}
        <ul className="divide-y divide-gray-100 text-sm dark:divide-gray-800" data-testid="dod-obligations">
          {obligations.map(ob => {
            const proof = proofFor(ob.ref);
            const ok = ob.implementation_satisfied || Boolean(proof);
            return <li key={ob.ref} className="flex items-center justify-between gap-3 py-2">
              <span className="min-w-0 text-gray-700 dark:text-gray-200">
                <span className="block truncate">{ob.title}</span>
                <code className="text-[10px] text-gray-400 dark:text-gray-500">{ob.ref}</code>
              </span>
              <span className={`shrink-0 text-xs font-medium ${ok ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                {ok ? `✓ Implementation · ${proof ? proof.id.slice(-12) : 'waived/rollup'}` : '◌ No accepted proof'}
              </span>
            </li>;
          })}
        </ul>
        <p className="mt-3 text-xs text-gray-400 dark:text-gray-500">
          {isTest
            ? 'This test card authenticates via passed scenarios; its records verify implementations recorded on other cards.'
            : "Test-phase verification is aggregated at the Spec rollup; this card's DoD requires implementation proof only."}
        </p>
      </div>

      {gateMode === 'blocking' && mine && !mine.satisfied && obligations.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900/70 dark:bg-red-950/25" data-testid="dod-blocked-banner">
          <span className="mt-0.5 text-sm text-red-500">⚠</span>
          <div className="text-xs text-red-700 dark:text-red-300">
            <div className="font-medium">Move to Done rejected — delivery_evidence_incomplete</div>
            <div>{unproven.length} obligation{unproven.length === 1 ? '' : 's'} lack{unproven.length === 1 ? 's' : ''} accepted proof. Record delivery evidence for <strong>{unproven[0]?.title}</strong>{unproven.length > 1 ? ` and ${unproven.length - 1} more` : ''}, or request a human waiver.</div>
          </div>
        </div>
      )}

      <div className="flex gap-2">
        {canRecordKind && <button type="button" onClick={() => { setFormOpen(v => !v); setError(''); }} className="rounded-md bg-gray-800 px-4 py-2 text-sm text-white dark:bg-gray-100 dark:text-gray-900" data-testid="dod-record-button">
          {formOpen ? 'Close recording form' : 'Record Delivery Evidence'}
        </button>}
        {canWaiver && <button type="button" onClick={() => { setFormOpen(true); setError(''); }} className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-600 dark:border-gray-700 dark:text-gray-300">
          Request Waiver (human)
        </button>}
      </div>

      {formOpen && (canRecordKind || canWaiver) && (
        <form className="space-y-3 rounded-md border p-3" onSubmit={e => { e.preventDefault(); void (canWaiver && !canRecordKind ? submitWaiver() : submit()); }} data-testid="dod-record-form">
          <fieldset>
            <legend className="text-sm font-medium">Which obligations does this proof cover?</legend>
            <div className="mt-2 max-h-44 space-y-1 overflow-y-auto">
              {selectableRefs.map(o => (
                <label key={o.ref} className="flex items-start gap-2 text-sm">
                  <input type="checkbox" checked={refs.includes(o.ref)} onChange={e => setRefs(e.target.checked ? [...refs, o.ref] : refs.filter(v => v !== o.ref))} aria-label={`Select ${o.ref}`} />
                  <span className="min-w-0"><span className="block truncate">{o.title}</span><code className="text-[10px] text-gray-400">{o.ref}</code></span>
                </label>
              ))}
            </div>
          </fieldset>
          {canRecordKind ? <>
            <label className="block text-sm">{isTest ? 'Passing scenario on this test card' : 'Accepted execution receipt (this card)'}
              <select required value={choice} onChange={e => setChoice(e.target.value)} className={`${field} mt-1`}>
                <option value="">Select…</option>
                {candidates.map(c => <option key={`${c.card_id}:${c.id}`} value={`${c.card_id}:${c.id}`}>{c.label}</option>)}
              </select>
              {candidates.length === 0 && <span className="text-xs text-gray-400">No eligible receipts yet. {isTest ? 'Execute the linked scenarios with authenticated evidence and complete the test card.' : 'Submit an accepted execution receipt for the committed files and complete the card.'}</span>}
            </label>
            {isTest && <fieldset>
              <legend className="text-sm font-medium">Implementation records verified by this run</legend>
              <div className="mt-2 max-h-36 space-y-1 overflow-y-auto">
                {verifiableImpls.map(i => (
                  <label key={i.id} className="flex items-start gap-2 text-sm">
                    <input type="checkbox" checked={testedIds.includes(i.id)} onChange={e => setTestedIds(e.target.checked ? [...testedIds, i.id] : testedIds.filter(v => v !== i.id))} />
                    <span className="min-w-0"><span className="block truncate">{i.relative_path}{i.symbol ? ` · ${i.symbol}` : ''}</span><code className="text-[10px] text-gray-400">{i.id}</code></span>
                  </label>
                ))}
              </div>
            </fieldset>}
          </> : canWaiver && (
            <label className="block text-sm">Exempt only this phase
              <select className={`${field} mt-1`} value={phase} onChange={e => setPhase(e.target.value as 'implementation' | 'test')}>
                <option value="implementation">Implementation</option>
                <option value="test">Verification</option>
              </select>
            </label>
          )}
          <label className="block text-sm">Explanation / audit reason
            <textarea required maxLength={20000} className={`${field} mt-1`} value={reason} onChange={e => setReason(e.target.value)} placeholder={isTest ? 'Explain how this passing run verifies the selected obligations.' : 'Explain how this execution receipt covers the selected obligations.'} />
          </label>
          <button type="submit" disabled={busy || !refs.length || !reason.trim() || (canRecordKind && (!choice || (isTest && !testedIds.length)))} className="rounded bg-cyan-700 px-3 py-2 text-sm text-white disabled:opacity-50">
            {busy ? 'Saving…' : canWaiver && !canRecordKind ? 'Record waiver (spec rollup)' : 'Record delivery evidence'}
          </button>
        </form>
      )}
    </>}
  </section>;
}
