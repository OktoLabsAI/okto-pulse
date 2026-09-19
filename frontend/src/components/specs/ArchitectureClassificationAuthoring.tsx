import { useEffect, useRef, useState, type ReactNode } from 'react';
import { v4 as uuidv4 } from 'uuid';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { IntegrationRequirement, IntegrationRequirementType } from '@/types';
import type { ArchitectureClassificationBatch, ArchitectureClassificationDecision, ArchitectureClassificationReviewItem, AuthoredArchitectureIR } from '@/types/architecture-classifications';

export interface ArchitectureAuthoringAuthority {
  canClassify: boolean;
  canPromote: boolean;
  canAssociate: boolean;
  specEdition: number;
  requirements: IntegrationRequirement[];
  onApplied: () => Promise<void>;
}

interface Props extends ArchitectureAuthoringAuthority {
  boardId: string; specId: string; specVersion: number;
  sourceReady: boolean; items: ArchitectureClassificationReviewItem[];
  renderItem: (item: ArchitectureClassificationReviewItem, selection: ReactNode) => ReactNode;
}
type Disposition = ArchitectureClassificationDecision['disposition'];
type IRDraft = Record<'title' | 'description' | 'provider' | 'consumer' | 'endpoint' | 'method' | 'contract_ref' | 'notes' | 'data_contract', string> & { integration_type: IntegrationRequirementType | '' };
const emptyIR = (): IRDraft => ({ title: '', integration_type: '', description: '', provider: '', consumer: '', endpoint: '', method: '', contract_ref: '', notes: '', data_contract: '' });
const types: IntegrationRequirementType[] = ['api', 'queue', 'stored_procedure', 'data_contract', 'event', 'file', 'other'];
const dispositionLabels = { promote_to_ir: 'Promote to IR', associate_existing_ir: 'Associate existing IR', context_only: 'Context only' };

function irDraft(proposed: Partial<AuthoredArchitectureIR>): IRDraft {
  const draft = emptyIR();
  for (const field of ['title', 'description', 'provider', 'consumer', 'endpoint', 'method', 'contract_ref', 'notes'] as const) draft[field] = proposed[field] ?? '';
  draft.integration_type = proposed.integration_type ?? '';
  draft.data_contract = proposed.data_contract === undefined ? '' : JSON.stringify(proposed.data_contract, null, 2);
  return draft;
}

function authoredIR(draft: IRDraft): AuthoredArchitectureIR {
  if (!draft.title.trim() || !draft.integration_type) throw new Error('Each proposed IR needs a title and an explicit integration type.');
  const ir: AuthoredArchitectureIR = { title: draft.title.trim(), integration_type: draft.integration_type };
  for (const key of ['description', 'provider', 'consumer', 'endpoint', 'method', 'contract_ref', 'notes'] as const) {
    if (draft[key].trim()) ir[key] = draft[key].trim();
  }
  if (draft.data_contract.trim()) {
    let parsed: unknown;
    try { parsed = JSON.parse(draft.data_contract, (_key, value) => { if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Non-finite JSON number'); return value; }); } catch { throw new Error('Data contract must be a valid JSON object.'); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Data contract must be a valid JSON object.');
    ir.data_contract = parsed as Record<string, unknown>;
  }
  return ir;
}

export function ArchitectureClassificationAuthoring({ boardId, specId, specVersion, specEdition, canClassify, canPromote, canAssociate, requirements, onApplied, sourceReady, items, renderItem }: Props) {
  const api = useDashboardApi();
  const [selected, setSelected] = useState<Record<string, ArchitectureClassificationReviewItem>>({});
  const [queue, setQueue] = useState<{ name: string; source: ArchitectureClassificationReviewItem; decision: ArchitectureClassificationDecision }[]>([]);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [rejectionEditable, setRejectionEditable] = useState(false);
  const [disposition, setDisposition] = useState<Disposition | ''>('');
  const [partial, setPartial] = useState(false);
  const [paths, setPaths] = useState('');
  const [remainder, setRemainder] = useState('');
  const [reason, setReason] = useState('');
  const [irs, setIRs] = useState<IRDraft[]>([emptyIR()]);
  const [refs, setRefs] = useState<string[]>([]);
  const [message, setMessage] = useState('');
  const [mode, setMode] = useState<'editing' | 'submitting' | 'unknown' | 'rejected' | 'saved'>('editing');
  const [suggesting, setSuggesting] = useState(false);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const attempt = useRef<ArchitectureClassificationBatch | null>(null);
  const uncertain = useRef(false);
  const suggestion = useRef<AbortController | null>(null);
  const sourcePrefill = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; suggestion.current?.abort(); };
  }, []);
  const chosen = Object.values(selected);
  const editable = mode === 'editing' && canClassify;
  const irCounts = new Map<string, number>();
  requirements.forEach(ir => irCounts.set(ir.id, (irCounts.get(ir.id) ?? 0) + 1));
  const activeIRs = requirements.filter(ir => ir.status === 'active' && irCounts.get(ir.id) === 1);
  const cancelSuggestion = () => { suggestion.current?.abort(); suggestion.current = null; setSuggesting(false); };
  const discardSourcePrefill = () => {
    if (!sourcePrefill.current) return;
    sourcePrefill.current = false;
    setIRs(value => [emptyIR(), ...value.slice(1)]);
    setMessage('The whole-contract proposal was cleared after changing its source or scope. Author the content for the selected parts.');
  };
  const updateIR = (index: number, patch: Partial<IRDraft>) => {
    sourcePrefill.current = false;
    cancelSuggestion(); setIRs(value => value.map((ir, position) => position === index ? { ...ir, ...patch } : ir));
  };

  async function loadSourceSuggestion() {
    if (chosen.length !== 1 || partial || !editable || !canPromote) return;
    cancelSuggestion();
    const controller = new AbortController(); suggestion.current = controller;
    setSuggesting(true); setMessage('');
    try {
      const item = chosen[0];
      const response = await api.getArchitectureClassifications(boardId, specId, controller.signal, { candidateId: item.candidate_id, sourceDigest: item.current_source_digest! });
      const detail = response.items.find(row => row.candidate_id === item.candidate_id && row.current_source_digest === item.current_source_digest);
      if (response.board_id !== boardId || response.spec_id !== specId || response.spec_version !== specVersion || response.spec_edition !== specEdition || response.profile !== 'detail' || !detail?.promotion_suggestion || detail.promotion_suggestion.scope_paths.join() !== '' || detail.promotion_suggestion.requires_author_review !== true) throw new Error('Suggestion unavailable');
      if (!mounted.current || controller.signal.aborted) return;
      const proposed = irDraft(detail.promotion_suggestion.proposed_ir);
      setIRs(value => [proposed, ...value.slice(1)]);
      sourcePrefill.current = true;
      setMessage('Source suggestion loaded. Review the proposed IR; no requirement has been created.');
    } catch {
      if (mounted.current && !controller.signal.aborted) setMessage('The source suggestion is unavailable or changed. Refresh the Spec and review the current contract.');
    } finally { if (mounted.current && !controller.signal.aborted) setSuggesting(false); }
  }

  function addDecisions() {
    if (!editable || !sourceReady || !chosen.length || !disposition) return;
    try {
      if (queue.length + chosen.length - (editingIndex === null ? 0 : 1) > 50) throw new Error('A batch can contain at most 50 decisions.');
      if (disposition === 'promote_to_ir' && (!canPromote || chosen.length !== 1)) throw new Error('Select one candidate to author its IRs, then queue other candidates for the same batch.');
      if (disposition === 'associate_existing_ir' && (!canAssociate || !refs.length || refs.some(ref => !activeIRs.some(ir => ir.id === ref)))) throw new Error('Select active IRs from this Spec.');
      if (disposition === 'context_only' && !reason.trim()) throw new Error('Explain why this scope is context only.');
      // JSON member names may contain spaces. Preserve the authored pointer;
      // only line separators/empty lines are presentation syntax.
      const scopePaths = partial ? paths.split(/\r?\n/).filter(path => path !== '') : [''];
      if (partial && (!scopePaths.length || !remainder.trim())) throw new Error('Select named contract paths and explain the remaining context.');
      const content = disposition === 'promote_to_ir' ? irs.map(authoredIR) : undefined;
      const authored = chosen.map(item => ({ name: item.name || item.interface_id, source: item, decision: {
        candidate_ref: item.candidate_id, expected_source_digest: item.current_source_digest!, disposition,
        scope_paths: scopePaths, ...(partial ? { remainder_reason: remainder.trim() } : {}),
        ...(disposition === 'context_only' ? { reason: reason.trim() } : {}),
        ...(disposition === 'associate_existing_ir' ? { integration_requirement_refs: [...refs] } : {}),
        ...(content ? { integration_requirements: content } : {}),
      } }));
      setQueue(value => editingIndex === null ? [...value, ...authored] : value.flatMap((row, index) => index === editingIndex ? authored : [row]));
      resetForm(); setMessage('');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Review the authored decision.'); }
  }

  function resetForm() {
    cancelSuggestion(); sourcePrefill.current = false; setEditingIndex(null); setSelected({}); setDisposition(''); setIRs([emptyIR()]); setRefs([]); setReason(''); setPartial(false); setPaths(''); setRemainder('');
  }

  function editQueued(index: number) {
    if (!editable) return;
    cancelSuggestion(); sourcePrefill.current = false;
    const row = queue[index], decision = row.decision;
    setEditingIndex(index); setSelected({ [row.source.candidate_id]: row.source });
    setDisposition(decision.disposition); setPartial(!decision.scope_paths.includes(''));
    setPaths(decision.scope_paths.join('\n')); setRemainder(decision.remainder_reason ?? '');
    setReason(decision.reason ?? ''); setRefs(decision.integration_requirement_refs ?? []);
    setIRs(decision.integration_requirements?.map(irDraft) ?? [emptyIR()]); setMessage('');
  }

  async function reloadSaved() {
    try { await onApplied(); } catch { if (mounted.current) setMessage('Classifications were saved. Refresh the Spec to load its current requirements; do not submit again.'); }
  }

  async function submit() {
    if (inFlight.current || !canClassify || (mode !== 'unknown' && (!sourceReady || mode !== 'editing' || !queue.length || editingIndex !== null))) return;
    if (!attempt.current) {
      const batch: ArchitectureClassificationBatch = { expected_spec_version: specVersion, expected_spec_edition: specEdition, idempotency_key: uuidv4(), decisions: queue.map(row => row.decision) };
      if (new TextEncoder().encode(JSON.stringify(batch)).length > 256 * 1024) { setMessage('The batch exceeds 256 KiB. Reduce the authored content before submitting.'); return; }
      attempt.current = JSON.parse(JSON.stringify(batch)) as ArchitectureClassificationBatch;
    }
    const batch = attempt.current;
    inFlight.current = true; setMode('submitting'); setMessage('');
    try {
      const receipt = await api.classifyArchitectureCandidates(boardId, specId, batch);
      if (receipt.contract_version !== 'architecture-classification/v1' || receipt.board_id !== boardId || receipt.spec_id !== specId || receipt.spec_edition !== specEdition || receipt.idempotency_key !== batch.idempotency_key || receipt.spec_version !== specVersion + 1) throw new Error('Unconfirmed receipt');
      if (!mounted.current) return;
      setMode('saved'); setQueue([]); setSelected({});
      setMessage(receipt.replayed ? 'The original classification was confirmed. No duplicate IRs were created.' : 'Classifications saved. Requirement readiness and Spec execution gates still apply.');
      await reloadSaved();
    } catch (error) {
      if (!mounted.current) return;
      const definiteRejection = error instanceof AuthenticatedFetchError && [401, 403, 404, 409, 413, 422].includes(error.status);
      if (uncertain.current || !definiteRejection) {
        uncertain.current = true; setMode('unknown');
        setMessage('The submission outcome is unknown. Retry this exact request to confirm it before creating another batch.');
      } else {
        setMode('rejected');
        setRejectionEditable([413, 422].includes(error.status));
        setMessage(error.status === 409 ? 'The source, Spec version, lock or workflow state changed. Refresh and review before preparing a new batch.'
          : [401, 403].includes(error.status) ? 'Permission to classify is missing. No new submission is available until access is restored.'
            : error.status === 404 ? 'This Spec is no longer available.' : 'The batch was rejected. Review the scopes, IR fields and references; no partial classification was saved.');
      }
    } finally { inFlight.current = false; }
  }

  return <div aria-label="Classification authoring" className="mt-3 space-y-3">
    <p className="text-xs">Select explicit contracts and queue your decisions. Saving applies the whole batch once; it does not start or approve the Spec.</p>
    {!sourceReady && <p role="status" className="text-xs">Refresh the Spec and wait for current sources before preparing a submission.</p>}
    {items.map(item => renderItem(item, <label className="text-xs"><input type="checkbox" aria-label={`Select ${item.name || item.interface_id}`} checked={Boolean(selected[item.candidate_id])}
      disabled={!editable || editingIndex !== null || !sourceReady || !item.current_source_digest || item.source_variant_count !== 1 || !['pending', 'current', 'review_required'].includes(item.state)}
      onChange={event => { cancelSuggestion(); discardSourcePrefill(); setSelected(previous => { const next = { ...previous }; if (event.target.checked) next[item.candidate_id] = item; else delete next[item.candidate_id]; return next; }); }} /> Include in authored decision</label>))}
    <p className="text-xs">{chosen.length} selected across pages · {queue.length} queued decisions</p>
    <fieldset disabled={!editable || !chosen.length} className="space-y-2 rounded border p-3">
      <legend className="text-sm">Author a decision</legend>
      <label className="block text-xs">Decision <select value={disposition} onChange={event => { cancelSuggestion(); setDisposition(event.target.value as Disposition | ''); }}>
        <option value="">Choose a decision</option>
        <option value="promote_to_ir" disabled={!canPromote || chosen.length !== 1}>Promote to IR</option>
        <option value="associate_existing_ir" disabled={!canAssociate}>Associate existing IR</option>
        <option value="context_only">Context only</option>
      </select></label>
      {chosen.length > 1 && <p className="text-xs">Context and association can cover several selected candidates. For promotion, author each candidate's IRs and queue them in this same batch.</p>}
      <label className="block text-xs"><input type="checkbox" checked={partial} onChange={event => { cancelSuggestion(); discardSourcePrefill(); setPartial(event.target.checked); }} /> Adopt only selected contract parts</label>
      {partial && <>
        <label className="block text-xs">Named contract paths (JSON Pointer, one per line)<textarea value={paths} onChange={event => setPaths(event.target.value)} className="block w-full border dark:bg-gray-800" /></label>
        <p className="text-xs">Use named members, such as /event_schema/publish. Array positions cannot identify stable contract parts.</p>
        <label className="block text-xs">Remaining context reason<textarea value={remainder} onChange={event => setRemainder(event.target.value)} className="block w-full border dark:bg-gray-800" /></label>
      </>}
      {disposition === 'context_only' && <label className="block text-xs">Context reason<textarea value={reason} onChange={event => setReason(event.target.value)} className="block w-full border dark:bg-gray-800" /></label>}
      {disposition === 'associate_existing_ir' && <fieldset className="text-xs"><legend>Active IRs in this Spec</legend>
        {!activeIRs.length && <p>No active, uniquely identified IRs are available.</p>}
        {activeIRs.map(ir => <label key={ir.id} className="block"><input type="checkbox" checked={refs.includes(ir.id)} onChange={event => setRefs(value => event.target.checked ? [...value, ir.id] : value.filter(id => id !== ir.id))} />{ir.title} ({ir.id})</label>)}
      </fieldset>}
      {disposition === 'promote_to_ir' && <>
        <button type="button" disabled={partial || suggesting} onClick={() => void loadSourceSuggestion()} className="text-xs text-blue-600 dark:text-blue-400">{suggesting ? 'Loading source suggestion…' : 'Replace first IR with source suggestion'}</button>
        {partial && <p className="text-xs">Author the IR content for the selected parts. The full source stays preserved in provenance.</p>}
        {irs.map((ir, index) => <fieldset key={index} className="space-y-1 border p-2"><legend>Proposed IR {index + 1}</legend>
          <label className="block text-xs">IR title<input value={ir.title} onChange={event => updateIR(index, { title: event.target.value })} className="block w-full border dark:bg-gray-800" /></label>
          <label className="block text-xs">Integration type<select value={ir.integration_type} onChange={event => updateIR(index, { integration_type: event.target.value as IntegrationRequirementType | '' })}><option value="">Choose a type</option>{types.map(type => <option key={type} value={type}>{type}</option>)}</select></label>
          {(['description', 'provider', 'consumer', 'endpoint', 'method', 'contract_ref', 'notes'] as const).map(field => <label key={field} className="block text-xs">{({ description: 'IR description', provider: 'Provider', consumer: 'Consumer', endpoint: 'Endpoint', method: 'Method (only if applicable)', contract_ref: 'Contract reference', notes: 'IR notes' })[field]}<input value={ir[field]} onChange={event => updateIR(index, { [field]: event.target.value })} className="block w-full border dark:bg-gray-800" /></label>)}
          <label className="block text-xs">Data contract (JSON object)<textarea value={ir.data_contract} onChange={event => updateIR(index, { data_contract: event.target.value })} className="block w-full border font-mono dark:bg-gray-800" rows={4} /></label>
          {irs.length > 1 && <button type="button" onClick={() => { cancelSuggestion(); setIRs(value => value.filter((_, position) => position !== index)); }}>Remove proposed IR {index + 1}</button>}
        </fieldset>)}
        <button type="button" onClick={() => { cancelSuggestion(); setIRs(value => [...value, emptyIR()]); }}>Add another proposed IR</button>
      </>}
      <button type="button" disabled={!disposition || !sourceReady || suggesting} onClick={addDecisions} className="btn btn-secondary text-xs">{editingIndex === null ? 'Queue decision' : 'Update queued decision'}</button>
      {editingIndex !== null && <button type="button" onClick={resetForm}>Cancel queued edit</button>}
    </fieldset>
    {queue.length > 0 && <section aria-label="Queued classifications" className="text-xs">
      {queue.map((row, index) => <div key={index} className="mb-2 border p-2"><p>{row.name} · {dispositionLabels[row.decision.disposition]}</p>
        <p>Scope: {row.decision.scope_paths.map(path => path || 'Whole contract').join(', ')}</p>
        <details><summary>Review queued content</summary><pre className="max-h-60 overflow-auto whitespace-pre-wrap">{JSON.stringify(row.decision, null, 2)}</pre></details>
        <button type="button" disabled={!editable || editingIndex !== null} onClick={() => editQueued(index)}>Edit queued decision {index + 1}</button>
        <button type="button" disabled={!editable || editingIndex !== null} onClick={() => setQueue(value => value.filter((_, position) => position !== index))}>Remove queued decision {index + 1}</button>
      </div>)}
    </section>}
    {message && <p role={mode === 'saved' ? 'status' : 'alert'} className="text-sm">{message}</p>}
    {mode === 'editing' && <button type="button" disabled={!canClassify || !queue.length || !sourceReady || suggesting || editingIndex !== null} onClick={() => void submit()} className="btn btn-primary text-xs">Save queued classifications</button>}
    {mode === 'submitting' && <p role="status">Saving classifications…</p>}
    {mode === 'unknown' && <button type="button" disabled={!canClassify} onClick={() => void submit()} className="btn btn-primary text-xs">Retry exact submission</button>}
    {mode === 'rejected' && <button type="button" onClick={() => { attempt.current = null; setQueue([]); setSelected({}); setMessage(''); setMode('editing'); }}>Discard rejected batch</button>}
    {mode === 'rejected' && rejectionEditable && <button type="button" onClick={() => { attempt.current = null; resetForm(); setMode('editing'); setMessage('Correct the rejected content, then submit a new intent. The original batch made no partial changes.'); }}>Edit rejected batch</button>}
    {mode === 'saved' && <button type="button" onClick={() => void reloadSaved()}>Reload Spec after saving</button>}
  </div>;
}
