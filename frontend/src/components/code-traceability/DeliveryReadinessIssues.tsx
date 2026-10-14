import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isReadinessDetail, type ReadinessAction, type ReadinessDetail, type ReadinessEntry,
  type ReadinessFact, type ReadinessIssue, type ReadinessPinpoint, type ReadinessResponse } from '@/types/delivery-readiness';
import { readinessDiagnostic } from './readinessDiagnostic';

export type ReadinessDestination = { type: 'card' | 'spec' | 'scenario' | 'obligation' | 'target' | 'record'; id: string };
type Navigate = (destination: ReadinessDestination) => void;
const button = 'rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 dark:border-gray-600 dark:hover:bg-gray-800';
const label = (value: string) => value.replace(/_/g, ' ');

function Reference({ type, id, onNavigate }: ReadinessDestination & { onNavigate?: Navigate }) {
  return onNavigate ? <button type="button" onClick={() => onNavigate({ type, id })}
    className="break-all text-left text-sky-700 underline underline-offset-2 dark:text-sky-300">{id}</button> : <span className="break-all">{id}</span>;
}
function Fact({ value }: { value: ReadinessFact }) {
  return <dl className="grid min-w-0 gap-x-3 gap-y-1 text-xs sm:grid-cols-[auto_1fr]">
    {Object.entries(value).filter(([, item]) => item != null && item !== '' && (!Array.isArray(item) || item.length))
      .map(([key, item]) => <div key={key} className="contents"><dt className="text-gray-500 dark:text-gray-400">{label(key)}</dt>
        <dd className="min-w-0 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{Array.isArray(item) ? item.join(', ') : String(item)}</dd></div>)}
  </dl>;
}
function Pinpoint({ value, onNavigate }: { value: ReadinessPinpoint; onNavigate?: Navigate }) {
  const type = value.target_id ? 'target' : value.kind === 'scenario' ? 'scenario' : value.entity_type === 'card' ? 'card' : 'obligation';
  return <div className="space-y-1 rounded border border-gray-200 p-3 text-xs dark:border-gray-700">
    <Reference type={type} id={value.target_id ?? value.entity_id} onNavigate={onNavigate} />
    <p>{value.relative_path ?? 'No code location recorded'}{value.qualified_symbol ? ' · ' + value.qualified_symbol : ''}
      {value.line_start !== null ? ` · lines ${value.line_start}–${value.line_end}` : ''}</p>
    <p className="break-all text-gray-500 dark:text-gray-400">{value.source_ref}{value.revision ? ' @ ' + value.revision : ''}</p>
    <p>Precision: {label(value.precision)} · Source: {label(value.origin)}</p>
    {value.revision && <p>Recorded revision; not a claim about the current file location.</p>}
    {(value.record_id || value.execution_id || value.observation_id) && <p className="break-all">Evidence: {[value.record_id, value.execution_id, value.observation_id].filter(Boolean).join(' · ')}</p>}
  </div>;
}
function Action({ value, onNavigate }: { value: ReadinessAction; onNavigate?: Navigate }) {
  return <article className="min-w-0 space-y-2 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
    <p className="text-sm font-medium">{value.message}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">{label(value.state)} · {label(value.execution_surface)}</p>
    {value.responsible_card_ids.length > 0 && <p className="flex flex-wrap gap-2 text-xs">Cards: {value.responsible_card_ids.map(id =>
      <Reference key={id} type="card" id={id} onNavigate={onNavigate} />)}</p>}
    {value.permission && <p className="break-all text-xs">Permission: {value.permission} · checked again on write</p>}
    {value.missing_inputs.length > 0 && <p className="break-words text-xs">Required input: {value.missing_inputs.join(', ')}</p>}
    {value.depends_on_action_ids.length > 0 && <p className="break-all text-xs">Depends on: {value.depends_on_action_ids.join(', ')}</p>}
    <details className="text-xs"><summary className="cursor-pointer">Action context</summary>
      <p className="mt-2 break-all">Action {value.id} · resolves {value.resolves_unit_count} units</p>
      <p className="break-all">{value.context_refs.join(' · ')}</p>
      {value.pulse_tool && <><p className="mt-2 font-medium">{value.pulse_tool}</p>
        <pre className="mt-1 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{JSON.stringify(value.arguments, null, 2)}</pre></>}
    </details>
  </article>;
}
function Actions({ values, onNavigate }: { values: ReadinessAction[]; onNavigate?: Navigate }) {
  const groups = [
    { label: 'Can act now', states: ['ready'] },
    { label: 'Depends on another action', states: ['waiting_dependency'] },
    { label: 'Input, human decision, permission or availability', states: ['requires_input', 'requires_human', 'forbidden', 'unavailable'] },
  ];
  return <div className="space-y-3">{groups.map(group => {
    const actions = values.filter(value => group.states.includes(value.state));
    return actions.length > 0 && <section key={group.label} aria-label={group.label} className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">{group.label}</h4>
      {actions.map(action => <Action key={action.id} value={action} onNavigate={onNavigate} />)}
    </section>;
  })}</div>;
}
function Entry({ entry, onNavigate }: { entry: ReadinessEntry; onNavigate?: Navigate }) {
  switch (entry.section) {
    case 'actions': return <Action value={entry.value} onNavigate={onNavigate} />;
    case 'pinpoints': return <Pinpoint value={entry.value} onNavigate={onNavigate} />;
    case 'expected': case 'observed': return <div className="space-y-2"><p className="font-medium">{label(entry.section)}</p><Fact value={entry.value} /></div>;
    case 'affected_units': return <p><Reference type={entry.value.unit === 'scenario' ? 'scenario' : 'obligation'} id={entry.value.ref} onNavigate={onNavigate} /> · edition {entry.value.spec_edition}</p>;
    case 'evidence_refs': return <p className="break-all">{label(entry.value.kind)}: <Reference type={entry.value.kind === 'target' ? 'target' : entry.value.kind === 'scenario' ? 'scenario' : 'record'} id={entry.value.id} onNavigate={onNavigate} /> · {entry.value.source_ref} {entry.value.revision}</p>;
    case 'relations': return <p className="break-all">{label(entry.value.kind)}: {entry.value.source_id}{entry.value.target_id ? ' → ' + entry.value.target_id : ''}</p>;
  }
}
function Issue({ issue, onNavigate, onSnapshotInvalid }: { issue: ReadinessIssue; onNavigate?: Navigate; onSnapshotInvalid: (message: string) => void }) {
  const api = useDashboardApi();
  const [detail, setDetail] = useState<ReadinessDetail>();
  const [entries, setEntries] = useState<ReadinessEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController>();
  useEffect(() => () => request.current?.abort(), []);
  async function load() {
    if (busy) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError('');
    const q = issue.detail_query;
    try {
      const value = await api.getDeliveryReadinessPage(q.board_id, q.spec_id, {
        card_id: q.card_id ?? undefined, phase: q.phase, issue_id: issue.id, snapshot_id: q.snapshot_id, limit: q.limit,
        ...(detail?.page.next_cursor ? { cursor: detail.page.next_cursor } : {}),
      }, controller.signal);
      if (!isReadinessDetail(value) || value.issue_id !== issue.id || value.snapshot_id !== q.snapshot_id
        || value.scope.board_id !== q.board_id || value.scope.spec_id !== q.spec_id || value.scope.card_id !== q.card_id) throw new Error('Readiness detail scope or snapshot changed. Refresh the assessment.');
      if (!controller.signal.aborted) {
        setDetail(value);
        setEntries(old => [...old, ...value.entries.filter(entry => !old.some(previous => previous.id === entry.id))]);
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        const message = e instanceof Error ? e.message : 'Details unavailable.';
        const code = e && typeof e === 'object' && 'code' in e ? String(e.code) : '';
        if (code === 'readiness_cursor_stale' || code === 'readiness_snapshot_changed' || message.includes('snapshot changed'))
          onSnapshotInvalid('The assessment changed. Refresh before continuing.');
        else setError(message);
      }
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const restricted = issue.detail_state === 'restricted';
  return <details className="min-w-0 rounded-lg border border-gray-200 dark:border-gray-700">
    <summary className="cursor-pointer space-y-1 p-3 text-sm">
      <span className="font-medium">{issue.message}</span>
      <span className="block text-xs text-gray-500 dark:text-gray-400">{label(issue.phase)} · {issue.affected_unit_count} affected units · {label(issue.intrinsic_state)}</span>
    </summary>
    <div className="space-y-4 border-t border-gray-200 p-3 text-sm dark:border-gray-700">
      <p className="break-all text-xs text-gray-500 dark:text-gray-400">{issue.id} · {issue.code}</p>
      <p>Owner: {issue.owner ?? label(issue.owner_state)}</p>
      <p>Proof: {issue.blocks_proof ? 'blocked by this cause' : 'not blocked by this cause'} · Closure: {issue.blocks_target_transition === null ? 'not evaluated' : issue.blocks_target_transition ? 'blocked' : 'not blocked by this cause'}</p>
      <div className="flex flex-wrap gap-2">{issue.affected_units.map(unit => <Reference key={unit.ref} type={unit.unit === 'scenario' ? 'scenario' : 'obligation'} id={unit.ref} onNavigate={onNavigate} />)}</div>
      <div className="space-y-1 text-xs">
        {issue.implementation_card_ids.length > 0 && <p className="flex flex-wrap gap-2">Implementation: {issue.implementation_card_ids.map(id => <Reference key={id} type="card" id={id} onNavigate={onNavigate} />)}</p>}
        {issue.test_card_ids.length > 0 && <p className="flex flex-wrap gap-2">Verification: {issue.test_card_ids.map(id => <Reference key={id} type="card" id={id} onNavigate={onNavigate} />)}</p>}
        {issue.scenario_ids.length > 0 && <p className="flex flex-wrap gap-2">Scenarios: {issue.scenario_ids.map(id => <Reference key={id} type="scenario" id={id} onNavigate={onNavigate} />)}</p>}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">{(['expected', 'observed'] as const).map(section => <section key={section} className="min-w-0 space-y-2">
        <h4 className="font-medium">{section === 'expected' ? 'Expected' : 'Observed'}</h4>
        {issue[section].length ? issue[section].map((fact, i) => <Fact key={i} value={fact} />) : <p className="text-xs text-gray-500">No additional authorized facts.</p>}
      </section>)}</div>
      {issue.pinpoints.map((pinpoint, i) => <Pinpoint key={i} value={pinpoint} onNavigate={onNavigate} />)}
      {!issue.pinpoints.length && <p className="text-xs text-gray-500">No code location recorded for this cause.</p>}
      {issue.evidence_refs.length > 0 && <p className="flex flex-wrap gap-2 text-xs">Evidence: {issue.evidence_refs.map(evidence => <Reference key={evidence.id} type="record" id={evidence.id} onNavigate={onNavigate} />)}</p>}
      {issue.depends_on_issue_ids.length > 0 && <p className="break-all text-xs">Depends on causes: {issue.depends_on_issue_ids.join(', ')}</p>}
      <p className="text-xs text-gray-500">Summary references may be limited. Open all details for complete impacts, evidence and actions.</p>
      {restricted ? <p role="status">Details restricted by your current permissions.</p> : <>
        {entries.length > 0 && <div className="space-y-3 rounded-lg bg-gray-50 p-3 dark:bg-gray-900/60" aria-label="Cause details">
          {entries.filter(entry => entry.section !== 'actions').map(entry => <div key={entry.id} className="min-w-0 text-xs"><Entry entry={entry} onNavigate={onNavigate} /></div>)}
          <Actions values={entries.flatMap(entry => entry.section === 'actions' ? [entry.value] : [])} onNavigate={onNavigate} />
          <p className="text-xs">{entries.length} of {detail?.page.total} detail entries loaded{detail?.page.has_more ? ' · partial' : ''}</p>
        </div>}
        {(!detail || detail.page.has_more) && <button type="button" disabled={busy} onClick={() => void load()} className={button}>{busy ? 'Loading details…' : detail ? 'Load more details' : 'Open all details and actions'}</button>}
      </>}
      {error && <p role="alert" className="text-xs text-red-700 dark:text-red-300">{error}</p>}
    </div>
  </details>;
}

export function DeliveryReadinessIssues({ value, onNavigate, onSnapshotInvalid }: {
  value: ReadinessResponse; onNavigate?: Navigate; onSnapshotInvalid: (message: string) => void;
}) {
  const [copied, setCopied] = useState('');
  async function copy() {
    try { await navigator.clipboard.writeText(readinessDiagnostic(value)); setCopied('Diagnostic copied.'); }
    catch { setCopied('Copy unavailable. Select the diagnostic text below.'); }
  }
  return <div className="min-w-0 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><p className="break-all text-xs text-gray-500 dark:text-gray-400">Edition {value.scope.spec_edition} · snapshot {value.snapshot_id.slice(0, 12)}</p>
      <button type="button" className={button} onClick={() => void copy()}>Copy diagnostic</button></div>
    {copied && <p role="status" className="text-xs">{copied}</p>}
    {copied.startsWith('Copy unavailable') && <textarea readOnly aria-label="Diagnostic text" value={readinessDiagnostic(value)} className="w-full rounded border p-2 text-xs dark:bg-gray-900" rows={8} />}
    <p className="text-xs text-gray-500 dark:text-gray-400">{value.summary.unresolved_units ?? 'Unknown'} pending units · {value.summary.root_causes} causes · {value.summary.lifecycle_pending ?? 'Unknown'} closure items. Exceptions: {value.summary.exceptions}.</p>
    <Actions values={value.next_actions} onNavigate={onNavigate} />
    <div className="space-y-2">{value.issues.map(issue => <Issue key={value.snapshot_id + ':' + issue.id} issue={issue} onNavigate={onNavigate} onSnapshotInvalid={onSnapshotInvalid} />)}</div>
    {value.page.total === 0 && <p>No causes in this assessment. A transition still requires current authorization and gate checks.</p>}
    <p className="text-xs text-gray-500 dark:text-gray-400">{value.issues.length} of {value.page.total} causes shown{value.page.has_more || value.page.response_truncated ? ' · partial summary; more information is available' : ''}. Up to three recommended actions; all actions are available in cause details.</p>
  </div>;
}
