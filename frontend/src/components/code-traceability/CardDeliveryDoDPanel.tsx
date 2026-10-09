import { resolveDeliveryGateMode } from '@/components/board/deliveryGateSettings';
import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { ObligationRefText } from './obligationPresentation';
import { CardProgressPanel } from './CardProgressPanel';
import { DeliveryNetImpactPanel } from './DeliveryNetImpactPanel';
import { TestDeliveryOverview } from './TestDeliveryOverview';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { DeliveryDisclosure } from './deliveryPresentation';
import type {
  CardDeliveryEvidenceInput,
  CardDeliveryBatchDraft,
  DeliveryEvidenceInput,
  DeliveryEvidenceProjection,
} from '@/types/delivery-evidence';

interface Props {
  boardId: string;
  card: { id: string; card_type: string; spec_id: string };
  canRecord?: boolean;
  canTest?: boolean;
  canWaiver?: boolean;
  canProgress?: boolean;
  onChanged?: () => void;
  onStage?: (draft: CardDeliveryBatchDraft) => void;
  onOpenTests?: () => void;
}

const field = 'w-full rounded border border-gray-300 bg-white p-2 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100';

// Card "Delivery" tab — the per-card DoD surface (mockup sm_c3383586).
// Obligations derive from this card's links; implementation proof is recorded
// HERE against this card's accepted execution receipts. Test cards record
// authenticated passing runs verifying implementations on other cards.
// Waivers stay spec-level and human-only (BR-3): the button routes an
// authorized human to the Spec rollup surface.
export function CardDeliveryDoDPanel({ boardId, card, canRecord = false, canTest = false, canWaiver = false, canProgress = false, onChanged, onStage, onOpenTests }: Props) {
  const api = useDashboardApi();
  const [data, setData] = useState<DeliveryEvidenceProjection | null>(null);
  const [gateMode, setGateMode] = useState<'advisory' | 'blocking' | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [formOpen, setFormOpen] = useState(false);
  const [refs, setRefs] = useState<string[]>([]);
  const [contributions, setContributions] = useState<Record<string, 'partial' | 'complete'>>({});
  const [composeProofs, setComposeProofs] = useState(false);
  const [executionSets, setExecutionSets] = useState<Record<string, string[]>>({});
  const [choice, setChoice] = useState('');
  const [testedIds, setTestedIds] = useState<string[]>([]);
  const [phase, setPhase] = useState<'implementation' | 'test'>('implementation');
  const [reason, setReason] = useState('');
  const replayRef = useRef<{ payload: string; key: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setGateMode(null); setError(''); setRefs([]); setContributions({}); setComposeProofs(false); setExecutionSets({}); setChoice(''); setTestedIds([]);
    api.getDeliveryEvidence(boardId, card.spec_id, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery proof could not be loaded.'); });
    api.getBoard(boardId).then(board => {
      if (!controller.signal.aborted) setGateMode(resolveDeliveryGateMode(board.settings?.delivery_evidence_gate));
    }).catch(() => { if (!controller.signal.aborted) setGateMode(null); });
    return () => controller.abort();
  }, [api, boardId, card.id, card.spec_id, reload]);

  const isTest = card.card_type === 'test';
  const mine = data?.per_card?.find(entry => entry.card_id === card.id) ?? null;
  const obligations = mine?.obligations ?? [];
  const acceptedProofs = (data?.implementations ?? []).filter(i => i.card_id === card.id
    && i.admitted_obligation_refs.length > 0);
  const proofFor = (ref: string) => acceptedProofs.find(p => p.ready_obligation_refs.includes(ref));
  const partialFor = (ref: string) => acceptedProofs.some(p => p.admitted_obligation_refs.includes(ref)
    && p.contributions.some(c => c.binding.obligation_ref === ref && c.contribution === 'partial'));
  const unproven = obligations.filter(o => !o.implementation_satisfied);
  const canRecordKind = isTest ? canTest : canRecord;
  const candidates = (data?.candidates ?? []).filter(c => c.card_id === card.id && c.kind === (isTest ? 'test' : 'implementation'));
  const selectableRefs = isTest
    ? (data?.rows ?? []).map(row => ({ ref: row.obligation.binding.obligation_ref, title: row.obligation.title, satisfied: row.test_satisfied }))
    : obligations.map(o => ({ ref: o.ref, title: o.title, satisfied: o.implementation_satisfied }));
  const verifiableImpls = (data?.implementations ?? []).filter(i => i.ready_obligation_refs.length > 0 && !data?.rejected_record_ids.includes(i.id));
  const compose = !isTest && composeProofs;
  const setsReady = refs.length > 0 && refs.every(ref => executionSets[ref]?.length
    && executionSets[ref].every(id => candidates.some(candidate => candidate.id === id)));

  async function submit() {
    const justification = reason.trim();
    if (!data || busy || !justification || !refs.length) return;
    const selected = candidates.find(c => `${c.card_id}:${c.id}` === choice);
    const key = crypto.randomUUID();
    const input: CardDeliveryEvidenceInput = {
      expected_card_version: compose ? mine?.card_version ?? candidates[0]?.card_version ?? 1 : selected?.card_version ?? 1,
      expected_spec_edition: data.edition,
      idempotency_key: key,
      kind: isTest ? 'test' : 'implementation',
      obligation_refs: isTest ? refs : [],
      justification,
      ...(isTest
        ? { scenario_id: selected?.id, implementation_ids: testedIds }
        : {
          ...(!compose ? { execution_id: selected?.id } : {}),
          bindings: refs.map(ref => ({ obligation_ref: ref, contribution: contributions[ref] ?? 'partial',
            ...(compose ? { execution_refs: (executionSets[ref] ?? []).map(id => ({ execution_id: id })) } : {}),
          })),
        }),
    };
    const payload = JSON.stringify({ ...input, idempotency_key: '', card_id: card.id });
    if (replayRef.current?.payload === payload) input.idempotency_key = replayRef.current.key;
    else replayRef.current = { payload, key: input.idempotency_key };
    setBusy(true); setError('');
    try {
      if (!isTest && (compose ? !setsReady : !selected)) throw new Error('Select the accepted execution receipts for each obligation first.');
      if (isTest && (!selected || !testedIds.length)) throw new Error('Select the authenticated run and the implementation records it observed.');
      if (onStage) {
        if (!mine?.card_version || mine.delivery_revision === undefined || !['started', 'in_progress'].includes(mine.status)) {
          throw new Error('The current execution state and delivery revision are required. Reload before preparing this report.');
        }
        onStage({ contract_version: 'card-delivery-batch/v1', expected_card_version: mine.card_version,
          expected_spec_edition: data.edition, expected_delivery_revision: mine.delivery_revision,
          entries: [{ client_ref: 'proof', kind: isTest ? 'test' : 'implementation', obligation_refs: input.obligation_refs,
            justification: input.justification, bindings: input.bindings, execution_id: input.execution_id,
            scenario_id: input.scenario_id, implementation_ids: input.implementation_ids }] });
      } else await api.recordCardDeliveryEvidence(boardId, card.id, card.spec_id, input);
      replayRef.current = null; setReason(''); setRefs([]); setChoice(''); setTestedIds([]);
      setFormOpen(false); if (!onStage) { setReload(v => v + 1); onChanged?.(); }
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

  return <section className="space-y-4" aria-label={isTest ? "Test verification evidence" : "Delivery evidence (Definition of Done)"}>
    {isTest && !onStage && <div className="flex justify-end"><button type="button" disabled={!data && !error} onClick={() => setReload(value => value + 1)} className="rounded-md border border-gray-300 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800">Refresh evidence</button></div>}
    {error && <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/70 dark:bg-red-950/25 dark:text-red-300">{error}</p>}
    {!data && !error && <PulseLoader label="Loading delivery evidence…" />}
    {data && <>
      {isTest && !onStage && <TestDeliveryOverview data={data} cardId={card.id} />}
      {mine && !isTest && <CardProgressPanel key={`${boardId}:${card.id}:${data.edition}:${canProgress}`} boardId={boardId} specId={card.spec_id} edition={data.edition} card={mine} canWrite={canProgress} onStage={onStage} onSaved={() => { setReload(v => v + 1); onChanged?.(); }} />}
      {!onStage && <>
      {!isTest && mine?.accumulated_impact && <DeliveryNetImpactPanel value={mine.accumulated_impact} />}
      {!isTest && mine?.report_impact?.source === 'accumulated' && <p role="status" className="text-sm">
        {mine.report_impact.current ? 'Submitted impact matches the known source bases.' : 'Submitted impact needs a new current basis before required impact validation can pass.'}
      </p>}
      {!isTest && <div className="rounded-md border border-gray-200 p-4 dark:border-gray-800">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">Delivery Evidence (Definition of Done)</h3>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${unproven.length ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'}`} data-testid="dod-gate-pill">
            {gateMode === null ? 'Unknown' : gateMode === 'advisory' ? 'Advisory' : 'Blocking'} · {unproven.length} of {obligations.length || selectableRefs.length} unproven
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
                <ObligationRefText value={ob.ref} />
              </span>
              <span className={`shrink-0 text-xs font-medium ${ok ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                {ok ? '✓ Implementation' : partialFor(ob.ref) ? '◌ Partial contribution' : '◌ No accepted proof'}
              </span>
            </li>;
          })}
        </ul>
        <p className="mt-3 text-xs text-gray-400 dark:text-gray-500">
          {isTest
            ? 'Save authenticated passed or failed results during execution. Delivery credit requires current passing evidence for every required criterion and completed cards.'
            : "Test-phase verification is aggregated at the Spec rollup; this card's DoD requires implementation proof only."}
        </p>
      </div>}

      {!isTest && gateMode === 'blocking' && mine && !mine.satisfied && obligations.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900/70 dark:bg-red-950/25" data-testid="dod-blocked-banner">
          <span className="mt-0.5 text-sm text-red-500">⚠</span>
          <div className="text-xs text-red-700 dark:text-red-300">
            <div className="font-medium">Move to Done rejected — delivery_evidence_incomplete</div>
            <div>{unproven.length} obligation{unproven.length === 1 ? '' : 's'} lack{unproven.length === 1 ? 's' : ''} accepted proof. Record delivery evidence for <strong>{unproven[0]?.title}</strong>{unproven.length > 1 ? ` and ${unproven.length - 1} more` : ''}, or request a human waiver.</div>
          </div>
        </div>
      )}

      </>}
      <div className="flex gap-2">
        {canRecordKind && <button type="button" onClick={() => { setFormOpen(v => !v); setError(''); }} className="rounded-md bg-gray-800 px-4 py-2 text-sm text-white dark:bg-gray-100 dark:text-gray-900" data-testid="dod-record-button">
          {formOpen ? 'Close recording form' : onStage ? 'Prepare delivery evidence for report' : isTest ? 'Record test evidence' : 'Record Delivery Evidence'}
        </button>}
        {canWaiver && !onStage && <button type="button" onClick={() => { setFormOpen(true); setError(''); }} className="rounded-md border border-gray-300 px-4 py-2 text-sm text-gray-600 dark:border-gray-700 dark:text-gray-300">
          Request Waiver (human)
        </button>}
      </div>

      {formOpen && (canRecordKind || canWaiver) && (
        <form className="space-y-3 rounded-md border p-3" onSubmit={e => { e.preventDefault(); void (canWaiver && !canRecordKind ? submitWaiver() : submit()); }} data-testid="dod-record-form">
          {isTest && canRecordKind && <div className="border-b border-gray-200 pb-3 dark:border-gray-700">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Connect a test result to delivery</h3>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Select only the obligations and implementation this run actually checked. The passed or failed result comes from the authenticated run.</p>
          </div>}
          {!isTest && canRecordKind && <label className="block text-sm"><input type="checkbox" checked={composeProofs} disabled={busy} onChange={e => setComposeProofs(e.target.checked)} /> Select receipts separately for each obligation</label>}
          <fieldset disabled={busy}>
            <legend className="text-sm font-medium">{isTest ? 'Obligations checked by this run' : 'Which obligations does this proof cover?'}</legend>
            {isTest && <p className="mt-1 text-xs text-gray-500">Available obligations belong to the Spec; they are not all assigned to this card.</p>}
            {selectableRefs.length === 0 && <p className="mt-2 text-sm text-gray-500">No obligations available to associate. Review the requirements and coverage in the Spec.</p>}
            <div className="mt-2 max-h-44 space-y-1 overflow-y-auto">
              {selectableRefs.map(o => (
                <div key={o.ref}>
                <label className="flex items-start gap-2 text-sm">
                  <input type="checkbox" checked={refs.includes(o.ref)} onChange={e => setRefs(e.target.checked ? [...refs, o.ref] : refs.filter(v => v !== o.ref))} aria-label={`Select ${o.ref}`} />
                  <span className="min-w-0"><span className="block truncate">{o.title}</span><ObligationRefText value={o.ref} /></span>
                </label>
                {!isTest && canRecordKind && refs.includes(o.ref) && <fieldset className="ml-6 flex gap-3 text-xs">
                  <legend>Contribution to {o.title}</legend>
                  {(['partial', 'complete'] as const).map(value => <label key={value}>
                    <input type="radio" name={`contribution-${o.ref}`} aria-label={`${value === 'partial' ? 'Partial' : 'Complete'} contribution for ${o.ref}`} checked={(contributions[o.ref] ?? 'partial') === value} onChange={() => setContributions(current => ({ ...current, [o.ref]: value }))} /> {value === 'partial' ? 'Partial' : 'Complete'}
                  </label>)}
                </fieldset>}
                {compose && canRecordKind && refs.includes(o.ref) && <fieldset className="ml-6 space-y-1 text-xs">
                  <legend>Receipts supporting {o.title}</legend>
                  {candidates.map(candidate => <label key={candidate.id} className="block">
                    <input type="checkbox" aria-label={`Receipt ${candidate.id} for ${o.ref}`} checked={(executionSets[o.ref] ?? []).includes(candidate.id)} onChange={e => setExecutionSets(current => ({ ...current,
                      [o.ref]: e.target.checked ? [...(current[o.ref] ?? []), candidate.id] : (current[o.ref] ?? []).filter(id => id !== candidate.id),
                    }))} /> {candidate.label}
                  </label>)}
                </fieldset>}
                </div>
              ))}
            </div>
          </fieldset>
          {!isTest && canRecordKind && <p className="text-xs">Declare the contribution separately for each obligation. Partial records do not add up to completion. Complete still requires accepted proof and the existing review.</p>}
          {compose && <p className="text-xs">Each set must share the same observed source and immutable revision. A newer commit does not establish that it contains another receipt's changes.</p>}
          {canRecordKind ? <>
            {!compose && <label className="block text-sm">{isTest ? 'Authenticated run on this test card' : 'Accepted execution receipt (this card)'}
              <select required disabled={busy || candidates.length === 0} value={choice} onChange={e => setChoice(e.target.value)} className={`${field} mt-1 disabled:opacity-60`}>
                <option value="">{isTest && !candidates.length ? 'No eligible authenticated run' : 'Select…'}</option>
                {candidates.map(c => <option key={`${c.card_id}:${c.id}`} value={`${c.card_id}:${c.id}`}>{c.label}</option>)}
              </select>
              {candidates.length === 0 && <span className="text-xs text-gray-500">{isTest ? 'No authenticated runs available. Planned scenarios do not appear here: a passed or failed result with valid authenticated evidence is required.' : 'No eligible receipts yet. Submit an accepted execution receipt for the committed files in the Implementation Targets tab — it becomes pickable here immediately, before completion.'}</span>}
            </label>}
            {isTest && candidates.length === 0 && <div className="space-y-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs text-sky-800 dark:border-sky-900 dark:bg-sky-950/20 dark:text-sky-300">
              {mine && !['started', 'in_progress', 'done'].includes(mine.status) && <p>This card is {mine.status.replace(/_/g, ' ')}. Runs become eligible in Started, In Progress or Done.</p>}
              <p>Review the linked scenarios in Tests. Execute them and record authenticated evidence, then use Refresh evidence here.</p>
              {onOpenTests && <button type="button" className="btn btn-secondary text-xs" onClick={onOpenTests}>Open Tests</button>}
            </div>}
            {isTest && <fieldset>
              <legend className="text-sm font-medium">Implementation records verified by this run</legend>
              <p className="mt-1 text-xs text-gray-500">Choose the implementation evidence from the cards whose work was tested.</p>
              {verifiableImpls.length === 0 && <p className="mt-2 rounded-md bg-amber-50 p-3 text-sm text-amber-800 dark:bg-amber-950/30 dark:text-amber-300">No implementation evidence is ready to verify. Record accepted implementation evidence on the corresponding development card first.</p>}
              <div className="mt-2 max-h-36 space-y-1 overflow-y-auto">
                {verifiableImpls.map(i => (
                  <label key={i.id} className="flex items-start gap-2 text-sm">
                    <input type="checkbox" checked={testedIds.includes(i.id)} onChange={e => setTestedIds(e.target.checked ? [...testedIds, i.id] : testedIds.filter(v => v !== i.id))} />
                    <span className="min-w-0"><span className="block font-medium">{data.per_card?.find(owner => owner.card_id === i.card_id)?.title ?? i.card_id}</span><span className="block break-words text-gray-500">{i.executions.map(proof => `${proof.relative_path}${proof.symbol ? ` · ${proof.symbol}` : ''}`).join(', ')}</span><code className="text-[10px] text-gray-400">{i.id}</code></span>
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
            <textarea required maxLength={20000} className={`${field} mt-1`} value={reason} onChange={e => setReason(e.target.value)} placeholder={isTest ? 'Explain what this run observed for the selected obligations, including failures.' : 'Explain how this execution receipt covers the selected obligations.'} />
          </label>
          <button type="submit" disabled={busy || !refs.length || !reason.trim() || (canRecordKind && (compose ? !setsReady : !choice || (isTest && !testedIds.length)))} className="rounded bg-cyan-700 px-3 py-2 text-sm text-white disabled:opacity-50">
            {busy ? 'Saving…' : onStage ? 'Add evidence to report draft' : canWaiver && !canRecordKind ? 'Record waiver (spec rollup)' : 'Record delivery evidence'}
          </button>
        </form>
      )}
      {isTest && mine && <DeliveryDisclosure title="Progress & recovery" badge={`${mine.progress?.total ?? 0} checkpoints`}>
          <p className="text-xs text-gray-500">Use checkpoints to resume unfinished work. Test results are recorded separately above.</p>
          <CardProgressPanel key={`${boardId}:${card.id}:${data.edition}:${canProgress}`} boardId={boardId} specId={card.spec_id} edition={data.edition} card={mine} canWrite={canProgress} onStage={onStage} onSaved={() => { setReload(v => v + 1); onChanged?.(); }} />
          {!onStage && mine.accumulated_impact && <DeliveryNetImpactPanel value={mine.accumulated_impact} />}
          {!onStage && mine.report_impact?.source === 'accumulated' && <p role="status" className="text-sm">{mine.report_impact.current ? 'Submitted impact matches the known source bases.' : 'Submitted impact needs a new current basis before required impact validation can pass.'}</p>}
      </DeliveryDisclosure>}
    </>}
  </section>;
}
