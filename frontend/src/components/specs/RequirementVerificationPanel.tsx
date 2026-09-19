import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { RequirementVerification, RequirementVerificationResponse, RequirementVerificationRow, VerificationInheritanceSelection, VerificationProfile, VerificationRequirementType } from '@/types/requirement-verification';
import type { VerificationRequirementOption } from './CriterionVerificationPanel';

const profiles: VerificationProfile[] = ['functional', 'integration', 'technical', 'operational'];
const keyOf = (type: string, id: string) => JSON.stringify([type, id]);
const blockerLabels: Record<string, string> = {
  verification_profile_required: 'Define the required profiles.',
  verification_path_missing: 'A required profile has no criterion path.',
  verification_inheritance_cycle: 'Inheritance contains a cycle.',
  verification_inheritance_source_unresolved: 'Resolve the source qualification before relying on it.',
  verification_inheritance_source_invalid: 'Review the missing or inactive source requirement.',
  verification_inheritance_terminal_missing: 'A selected criterion is no longer reachable from its source.',
  spec_scope_revision_conflict: 'The inherited definition changed. Review and explicitly update the selection.',
  verification_policy_minimum_required: 'A required policy profile is missing.',
  verification_configuration_invalid: 'Review the unsupported qualification metadata.',
  verification_criterion_incomplete: 'Complete the criterion condition and profile.',
  verification_resolution_limit: 'The path resolution limit was reached; completeness is unknown.',
  verification_method_required: 'Choose a verification method.',
  verification_method_invalid: 'The stored method is unknown.',
  verification_method_unsupported: 'This method has no supported evidence admission path.',
  verification_method_capability_unavailable: 'Supported evidence methods could not be determined.',
  verification_observation_required: 'Complete the Given, When and Then observations.',
  verification_test_card_required: 'Assign this scenario to a Test Card.',
  verification_scenario_required: 'Link a scenario to this criterion.',
  verification_planning_read_restricted: 'Reading method and Test Card planning requires scenario and Card read permissions.',
  implementation_plan_required: 'Declare the implementation contribution for each linked Card.',
  implementation_contribution_links_changed: 'The Card links changed; review the declared contributions.',
  implementation_contribution_card_unavailable: 'An implementation Card is missing, inactive or outside this scope.',
  implementation_contribution_scope_uncovered: 'Some required criteria have no implementation contribution.',
  implementation_contribution_criterion_unresolved: 'Review the criteria selected for this contribution.',
  implementation_inheritance_allocation_ambiguous: 'The inherited responsibility has several possible owners or no owner. Declare a direct allocation.',
  implementation_inheritance_source_unresolved: 'Review the canonical source requirement for inherited responsibility.',
  implementation_qualification_unresolved: 'Resolve requirement qualification before completing the contribution plan.',
};
interface Scope { boardId: string; specId: string; version: number; edition: number }
function sameScope(response: RequirementVerificationResponse, scope: Scope) {
  return response.contract_version === 'requirement-verification/v1' && response.board_id === scope.boardId
    && response.spec_id === scope.specId && response.spec_version === scope.version && response.spec_edition === scope.edition;
}

function QualificationEditor({ scope, row, options, criterionLabels, onSaved }: {
  scope: Scope; row: RequirementVerificationRow; options: VerificationRequirementOption[];
  criterionLabels: Record<string, string>; onSaved: () => Promise<void>;
}) {
  const api = useDashboardApi();
  const [draft, setDraft] = useState<RequirementVerification>(() => structuredClone(row.verification || { mode: 'explicit', required_profiles: [], inheritance: [], evidence_policy_ref: 'pulse-verification/v1' }));
  const [sourceKey, setSourceKey] = useState('');
  const [source, setSource] = useState<RequirementVerificationRow | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [aspect, setAspect] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const live = useRef(true);
  const writing = useRef(false);
  const request = useRef<{ controller?: AbortController; generation: number }>({ generation: 0 });
  useEffect(() => { const pending = request.current; live.current = true; return () => { live.current = false; pending.controller?.abort(); }; }, []);
  const choices = options.filter(option => keyOf(option.type, option.id) !== keyOf(row.requirement_type, row.requirement_id)
    && options.filter(item => keyOf(item.type, item.id) === keyOf(option.type, option.id)).length === 1);
  const sourceCriteria = [...new Set(source?.criteria_paths.map(path => path.criterion_id) || [])];

  function resetSource(value: string) {
    request.current.controller?.abort(); request.current.generation += 1;
    setSourceKey(value); setSource(null); setSelected([]); setAspect(''); setLoading(false); setError('');
  }
  async function loadSource(more = false) {
    const option = choices.find(item => keyOf(item.type, item.id) === sourceKey);
    if (!option || (more && source?.next_paths_offset == null)) return;
    request.current.controller?.abort();
    const controller = new AbortController();
    const generation = ++request.current.generation;
    request.current.controller = controller;
    setLoading(true); setError('');
    try {
      const response = await api.getRequirementVerification(scope.boardId, scope.specId, controller.signal, {
        requirementType: option.type, requirementId: option.id, pathsOffset: more ? source?.next_paths_offset ?? 0 : 0,
      });
      if (!live.current || generation !== request.current.generation) return;
      const next = response.items.find(item => item.requirement_type === option.type && item.requirement_id === option.id);
      if (!sameScope(response, scope) || !next?.source_digest || !response.population_complete) throw new Error('Source changed or is unavailable. Reload the Spec before authoring.');
      if (more && next.source_digest !== source?.source_digest) throw new Error('The source changed while loading criteria. Reload the Spec.');
      setSource(more && source ? { ...next, criteria_paths: [...source.criteria_paths, ...next.criteria_paths] } : next);
    } catch (cause) {
      if (live.current && generation === request.current.generation) setError(cause instanceof Error ? cause.message : 'Could not load the source.');
    } finally {
      if (live.current && generation === request.current.generation) setLoading(false);
    }
  }
  function adoptSelection() {
    if (!source?.source_digest || !selected.length || !aspect.trim()) { setError('Select a source, at least one criterion and the covered aspect.'); return; }
    if (selected.some(id => !sourceCriteria.includes(id))) { setError('Review every selected criterion against the current source.'); return; }
    if (source.criteria_paths.some(path => selected.includes(path.criterion_id) && path.path.some(step => keyOf(step.requirement_type, step.requirement_id) === keyOf(row.requirement_type, row.requirement_id)))) {
      setError('This selection would point back to the current requirement.'); return;
    }
    const value: VerificationInheritanceSelection = { source: { requirement_type: source.requirement_type, requirement_id: source.requirement_id },
      source_digest: source.source_digest, criterion_ids: selected, covered_aspect: aspect };
    const remaining = (draft.inheritance || []).filter(item => keyOf(item.source.requirement_type, item.source.requirement_id) !== sourceKey);
    if (remaining.length >= 20) { setError('A qualification supports at most 20 source selections.'); return; }
    setDraft({ ...draft, inheritance: [...remaining, value] }); resetSource('');
  }
  async function reload() {
    try { await onSaved(); } catch { if (live.current) setError('Saved. Refresh failed; reload the Spec to see the current qualification.'); }
  }
  async function save() {
    if (writing.current || saved) return;
    if (!draft.required_profiles.length || (draft.mode === 'inherited' && !draft.inheritance?.length)) {
      setError('Choose the required profiles and complete any inheritance selections.'); return;
    }
    writing.current = true; setBusy(true); setError('');
    request.current.controller?.abort(); request.current.generation += 1; setLoading(false);
    try {
      const response = await api.updateSpecEntity(scope.specId, row.requirement_type, row.requirement_id, { verification: draft }, scope.version);
      if (!live.current) return;
      if (!response.success) throw new Error(response.error_message || 'Qualification was refused. Review the current Spec.');
      setSaved(true); await reload();
    } catch (cause) {
      if (live.current) setError(cause instanceof Error ? cause.message : 'Could not save. Refresh before retrying.');
    } finally { writing.current = false; if (live.current) setBusy(false); }
  }
  return <div className="space-y-3 rounded border border-slate-600 p-3">
    <fieldset disabled={busy || saved} className="space-y-3">
      {row.default_proposal && <button type="button" onClick={() => { setDraft(structuredClone(row.default_proposal!.verification)); resetSource(''); }}>Use proposed default ({row.default_proposal.version})</button>}
      <label className="block text-sm">Qualification mode
        <select aria-label="Qualification mode" className="ml-2 rounded bg-slate-800 p-1" value={draft.mode} onChange={event => { setDraft({ ...draft, mode: event.target.value as 'explicit' | 'inherited', inheritance: [] }); resetSource(''); }}>
          <option value="explicit">Explicit criteria</option><option value="inherited">Selected inheritance</option>
        </select>
      </label>
      <div className="flex flex-wrap gap-3">{profiles.map(profile => <label key={profile} className="text-sm"><input type="checkbox" checked={draft.required_profiles.includes(profile)} onChange={event => setDraft({ ...draft, required_profiles: event.target.checked ? [...draft.required_profiles, profile] : draft.required_profiles.filter(item => item !== profile) })} /> {profile}</label>)}</div>
      {draft.mode === 'inherited' && <>
        {(draft.inheritance || []).map((item, index) => <div key={keyOf(item.source.requirement_type, item.source.requirement_id)} className="rounded border border-slate-700 p-2 text-sm">
          <p>{item.source.requirement_id} · {item.criterion_ids.join(', ')} · {item.covered_aspect}</p>
          <button type="button" onClick={() => { resetSource(keyOf(item.source.requirement_type, item.source.requirement_id)); setSelected([...item.criterion_ids]); setAspect(item.covered_aspect); }}>Review source selection {index + 1}</button>
          <button type="button" className="ml-3" onClick={() => setDraft({ ...draft, inheritance: draft.inheritance?.filter((_, i) => i !== index) })}>Remove source {index + 1}</button>
        </div>)}
        <label className="block text-sm">Inheritance source
          <select aria-label="Inheritance source" className="block w-full rounded bg-slate-800 p-2" value={sourceKey} onChange={event => resetSource(event.target.value)}>
            <option value="">Select a requirement</option>
            {choices.map(option => <option key={keyOf(option.type, option.id)} value={keyOf(option.type, option.id)}>{option.title} ({option.id})</option>)}
          </select>
        </label>
        <button type="button" disabled={!sourceKey || loading} onClick={() => void loadSource()}>Load source criteria</button>
        {loading && <p role="status">Loading source criteria…</p>}
        {source && <div className="space-y-2">
          {!source.qualification_resolved && <p>Source qualification has pending issues; selecting it does not resolve them.</p>}
          {sourceCriteria.map(id => <label key={id} className="block text-sm"><input type="checkbox" checked={selected.includes(id)} onChange={event => setSelected(event.target.checked ? [...selected, id] : selected.filter(item => item !== id))} /> {criterionLabels[id] || id} ({id})</label>)}
          {selected.filter(id => !sourceCriteria.includes(id)).map(id => <p key={id}>Selected criterion not loaded or unavailable: {id} <button type="button" onClick={() => setSelected(selected.filter(item => item !== id))}>Remove {id}</button></p>)}
          {source.next_paths_offset != null && <button type="button" disabled={loading} onClick={() => void loadSource(true)}>Load more source criteria</button>}
          {source.paths_unavailable && <p role="alert">Some paths could not be displayed within the response limit.</p>}
          <label className="block text-sm">Covered aspect<textarea aria-label="Covered aspect" maxLength={2000} className="block w-full rounded bg-slate-800 p-2" value={aspect} onChange={event => setAspect(event.target.value)} /></label>
          <button type="button" disabled={loading} onClick={adoptSelection}>Use selected criteria</button>
        </div>}
      </>}
      <button type="button" onClick={() => void save()}>{busy ? 'Saving…' : 'Save qualification'}</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    {saved && <button type="button" onClick={() => void reload()}>Reload saved qualification</button>}
  </div>;
}

type PanelProps = {
  scope: Scope; canRead: boolean; canEdit: (type: VerificationRequirementType) => boolean;
  canReadPlanning?: boolean;
  options: VerificationRequirementOption[]; criteria: unknown[]; onSaved: () => Promise<void>;
};

export function RequirementVerificationPanel(props: PanelProps) {
  const key = JSON.stringify([props.scope, props.canRead, props.canReadPlanning, ...(['functional_requirement', 'technical_requirement', 'integration_requirement', 'observability_requirement', 'business_rule'] as const).map(props.canEdit)]);
  return <RequirementVerificationContent key={key} {...props} />;
}

function RequirementVerificationContent({ scope, canRead, canReadPlanning = false, canEdit, options, criteria, onSaved }: PanelProps) {
  const api = useDashboardApi();
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [editor, setEditor] = useState<RequirementVerificationRow | null>(null);
  const [result, setResult] = useState<{ scope: string; data?: RequirementVerificationResponse; error?: string } | null>(null);
  const stamp = JSON.stringify([scope, canRead, offset, refresh]);
  const current = result?.scope === stamp ? result : null;
  const criterionLabels = Object.fromEntries(criteria.flatMap(value => {
    if (!value || typeof value !== 'object') return [];
    const item = value as Record<string, unknown>;
    return typeof item.id === 'string' ? [[item.id, String(item.text || item.title || item.id)]] : [];
  }));
  useEffect(() => {
    if (!open || !canRead) return;
    const controller = new AbortController(); let live = true;
    void (async () => {
      try {
        const data = await api.getRequirementVerification(scope.boardId, scope.specId, controller.signal, { offset, limit: 25 });
        if (!sameScope(data, scope)) throw new Error('The Spec changed. Reload it before reviewing qualification.');
        if (live) setResult({ scope: stamp, data });
      } catch {
        if (live) setResult({ scope: stamp, error: 'Qualification could not be loaded for this Spec version. Its completeness is unknown; reload the Spec.' });
      }
    })();
    return () => { live = false; controller.abort(); };
  }, [api, scope, canRead, open, offset, stamp]);
  if (!canRead) return <p className="text-xs text-slate-400">Requirement qualification needs Spec, IR and OR read permissions.</p>;
  return <section aria-label="Requirement qualification" className="space-y-3 rounded border border-slate-700 p-3">
    <button type="button" aria-expanded={open} onClick={() => { setOpen(!open); setEditor(null); }}>Review requirement qualification</button>
    {open && <>
      <p className="text-xs text-slate-400">Declared criteria, methods and Test Cards describe planned verification. Implementation responsibilities, dependencies, semantic review and delivery evidence are evaluated separately.</p>
      {!current && <p role="status">Loading qualification…</p>}
      {current?.error && <p role="alert">{current.error}</p>}
      {current?.data && <>
        <p>{current.data.resolved_count} with resolved criterion paths · {current.data.population_total ?? 'unknown'} requirements in scope · {current.data.issue_count} criterion/population issue(s)</p>
        {!current.data.population_complete && <p role="alert">The requirement population is incomplete. Counts describe only observed requirements.</p>}
        {canReadPlanning && current.data.methods_evaluated ? <>
          <p>Method planning: {current.data.method_plan_complete ? 'complete' : 'pending'} · Test Card planning: {current.data.verification_work_complete ? 'complete' : 'pending'}</p>
          <p className="text-xs">Planning does not require a passing run and does not grant delivery credit.</p>
          {!current.data.planning_population_complete && <p role="alert">The planning population is incomplete; readiness is unknown.</p>}
        </> : <p>Method and Test Card planning is unavailable.</p>}
        {!canReadPlanning && <p>{blockerLabels.verification_planning_read_restricted}</p>}
        {canReadPlanning && current.data.planning_issues?.map((issue, i) => <p key={i} className="text-amber-400">{blockerLabels[issue.code] || 'Review the incomplete or invalid verification planning facts.'}</p>)}
        {canReadPlanning && current.data.planning_issues_truncated && <p>Additional planning issues are not shown.</p>}
        {canReadPlanning && current.data.implementation_plan_evaluated && <>
          <p>Declared implementation scope: {current.data.implementation_plan_complete ? 'complete' : 'pending'}</p>
          <p className="text-xs">This summary covers functional, technical, integration and observability requirements, plus Business Rules.</p>
          <p className="text-xs">Contribution scope does not approve or complete the work. Dependencies and delivery remain separate.</p>
          {current.data.implementation_issues?.map(code => <p key={code}>{blockerLabels[code] || 'Implementation responsibility is unavailable or incomplete.'}</p>)}
        </>}
        {canReadPlanning && current.data.effective_inventory && <div className="rounded border border-slate-700 p-2 text-sm">
          <p>Full delivery scope: {current.data.effective_inventory.population_complete ? `${current.data.effective_inventory.total} obligations` : 'unavailable'}</p>
          {current.data.effective_inventory.population_complete
            ? <p>{current.data.effective_inventory.pending_count} pending · {current.data.effective_inventory.unassigned_count} without allocation</p>
            : <p role="alert">The complete obligation population is unavailable; readiness is unknown.</p>}
          <p className="text-xs">Includes acceptance criteria, API contracts, decisions and Cards without requirement links. This planning inventory does not change the adopted delivery contract or grant credit.</p>
        </div>}
        {current.data.items.map(row => <div key={keyOf(row.requirement_type, row.requirement_id)} className="space-y-2 rounded border border-slate-700 p-2 text-sm">
          <p>{row.title} · {row.requirement_id} · {row.verification?.mode || 'Not qualified'}</p>
          <p>{row.verification?.required_profiles.join(', ') || 'Profiles not defined'}</p>
          {canReadPlanning && row.contribution_blockers?.map(code => <p key={code} className="text-amber-400">{blockerLabels[code] || 'Review the declared implementation scope.'}</p>)}
          {canReadPlanning && row.implementation_contributions?.map(contribution => <div key={contribution.card_id} className="text-xs">
            <p>Implementation Card: {contribution.card_id} · {contribution.origin} · {contribution.scope === 'whole_requirement' ? 'whole requirement' : 'selected criteria'}</p>
            {contribution.summary && <p>{contribution.summary}</p>}
            <p>Criteria: {contribution.criterion_ids.join(', ') || 'not resolved'}</p>
            {contribution.sources.length > 0 && <p>Inherited from: {contribution.sources.map(source => source.requirement_id).join(', ')}</p>}
            {(contribution.criteria_truncated || contribution.sources_truncated) && <p>Additional contribution details are omitted from this summary.</p>}
          </div>)}
          {canReadPlanning && row.contributions_truncated && <p>Additional contributions are omitted; the result considers all {row.contribution_count} contributions.</p>}
          {row.blockers.map((blocker, i) => <p key={i} className="text-amber-400">{blockerLabels[blocker.code] || 'Review this qualification.'}{blocker.profile ? ` (${blocker.profile})` : ''}</p>)}
          {row.criteria_paths.map((path, i) => <div key={i} className="space-y-1 text-xs">
            <p>{path.criterion_id} · {path.profile} · {path.path.map(step => step.requirement_id).join(' → ')}</p>
            {canReadPlanning && path.planning_blockers?.map(code => <p key={code}>{blockerLabels[code] || 'Review this verification plan.'}</p>)}
            {canReadPlanning && path.scenario_plans?.map(plan => <div key={plan.scenario_id} className="ml-3">
              <p>{plan.scenario_id} · {plan.method || 'Method not defined'} · Test Cards: {plan.test_card_ids.join(', ') || 'none'}</p>
              {[...plan.blockers, ...plan.work_blockers].map(code => <p key={code} className="text-amber-400">{blockerLabels[code] || 'Review this scenario plan.'}</p>)}
              {plan.test_cards_truncated && <p>Showing {plan.test_card_ids.length} of {plan.test_card_count} Test Cards.</p>}
            </div>)}
            {canReadPlanning && path.scenarios_truncated && <p>Additional scenarios are omitted from this summary; planning considers all {path.scenario_count} scenarios.</p>}
          </div>)}
          {(row.paths_has_more || row.blockers_truncated) && <p>Additional paths or issues are not shown in this summary.</p>}
          {canEdit(row.requirement_type) && <button type="button" onClick={() => setEditor(row)}>Edit qualification {row.requirement_id}</button>}
        </div>)}
        {offset > 0 && <button type="button" onClick={() => { setOffset(0); setEditor(null); }}>First requirements</button>}
        {current.data.next_offset != null && <button type="button" onClick={() => { setOffset(current.data!.next_offset!); setEditor(null); }}>Next requirements</button>}
      </>}
      <button type="button" onClick={() => { setEditor(null); setRefresh(refresh + 1); }}>Refresh qualification</button>
      {editor && canEdit(editor.requirement_type) && <QualificationEditor key={keyOf(editor.requirement_type, editor.requirement_id)} scope={scope} row={editor} options={options} criterionLabels={criterionLabels} onSaved={onSaved} />}
    </>}
  </section>;
}
