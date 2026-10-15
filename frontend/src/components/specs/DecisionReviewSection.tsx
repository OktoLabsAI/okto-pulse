import { useEffect, useRef, useState } from 'react';
import type { DecisionReviewInput, DecisionReviewProjection, DecisionReviewResult } from '@/types/decision-reviews';
import { AuthenticatedFetchError } from '@/lib/authFetch';

export const decisionStatusLabel = (status: string) => ({
  verified: 'Verified', planning_pending: 'Plan required', obligations_pending: 'Obligation evidence pending',
  inspection_pending: 'Inspection pending', conflict: 'Conflicting observations', passed: 'Passed',
  failed: 'Failed', inconclusive: 'Inconclusive', unavailable: 'Unavailable', revoked: 'Revoked', aborted: 'Aborted',
}[status] || 'Verification pending');

/** A review is a batch: retain every observation instead of guessing one Decision. */
export function DecisionInspectionDetails({ data, recordId }: { data: DecisionReviewProjection; recordId: string }) {
  const records = data.history.filter(record => record.id === recordId);
  if (records.length !== 1) return <p role="alert">The referenced inspection is unavailable in this Spec scope.</p>;
  const record = records[0];
  return <article data-readiness-ref={`inspection:${record.id}`} tabIndex={-1} aria-label="Decision inspection record"
    className="space-y-3 rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500">
    <h3 className="font-semibold">Decision inspection</h3>
    <p className="break-all">{record.id} · Edition {record.edition}{record.edition !== data.edition ? ' · Previous version' : ''}{record.revoked ? ' · Revoked' : ''}</p>
    <p className="break-words">{record.actor_id} · {record.actor_kind} · {new Date(record.created_at).toLocaleString()}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">Authenticated reviewer observation. This historical record does not establish current verification by itself.</p>
    {record.observations.map(observation => <details key={observation.decision_id} className="rounded-lg border border-gray-200 dark:border-gray-700">
      <summary className="cursor-pointer break-words px-3 py-2 font-medium">{observation.decision_id} · {decisionStatusLabel(observation.result)}</summary>
      <dl className="space-y-2 border-t border-gray-200 p-3 dark:border-gray-700">
        <div><dt className="font-medium">Expected</dt><dd className="whitespace-pre-wrap break-words">{observation.expected}</dd></div>
        <div><dt className="font-medium">Observed</dt><dd className="whitespace-pre-wrap break-words">{observation.observed}</dd></div>
        <div><dt className="font-medium">Scope fingerprint</dt><dd className="break-all font-mono text-xs">{observation.expected_scope_sha256}</dd></div>
        <div><dt className="font-medium">Sources</dt><dd>{observation.sources.map(source => <p key={source.reference} className="break-all">{source.reference} · {source.revision} · {source.sha256}</p>)}</dd></div>
        {observation.reconciles.length > 0 && <div><dt className="font-medium">Reconciles</dt><dd className="break-all">{observation.reconciles.join(', ')}</dd></div>}
        {observation.separation.warning && <p className="text-amber-700 dark:text-amber-400">Reviewer separation warning was recorded.</p>}
      </dl>
    </details>)}
  </article>;
}

interface DecisionReviewSectionProps {
  data: DecisionReviewProjection; decisionId: string; canReview: boolean;
  onSubmit: (body: DecisionReviewInput) => Promise<void>;
  readCurrent?: boolean;
}

export function DecisionReviewSection(props: DecisionReviewSectionProps) {
  const denied = props.data.decisions.find(d => d.decision_id === props.decisionId)?.separation?.allowed === false;
  return <DecisionReviewEditor key={JSON.stringify([props.data.board_id, props.data.spec_id, props.decisionId, props.canReview, denied])} {...props} />;
}

function DecisionReviewEditor({ data, decisionId, canReview, onSubmit, readCurrent = true }: DecisionReviewSectionProps) {
  const [observed, setObserved] = useState('');
  const [result, setResult] = useState<DecisionReviewResult>('inconclusive');
  const [reconcile, setReconcile] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [base, setBase] = useState<{ edition: number; version: number; review: string; decision: DecisionReviewProjection['decisions'][number] } | null>(null);
  const retry = useRef<{ payload: string; body: DecisionReviewInput }>();
  const mounted = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const current = data.decisions.find(d => d.decision_id === decisionId);
  const snapshot = current ? { edition: data.edition, version: data.version, review: data.review_revision, decision: current } : null;
  const changed = Boolean(base && JSON.stringify(base) !== JSON.stringify(snapshot));
  const ready = readCurrent && data.complete && Boolean(current?.basis && current.separation?.allowed);
  const begin = () => { if (!base && snapshot && ready) setBase(snapshot); };
  const history = data.history.filter(r => r.observations.some(o => o.decision_id === decisionId));
  const prior = history.filter(r => r.edition !== data.edition);
  const currentHistory = history.filter(r => r.edition === data.edition);
  const submit = async () => {
    if (!canReview || inFlight.current || (!uncertain && (!ready || changed || !observed.trim()))) return;
    const original = base ?? snapshot;
    if (!uncertain) {
      if (!original?.decision.basis) return;
      const request = { expected_edition: original.edition, expected_version: original.version, expected_review_revision: original.review,
        entries: [{ decision_id: decisionId, expected_scope_sha256: original.decision.basis.scope_sha256, observed: observed.trim(), result,
          sources: original.decision.basis.sources, reconciles: reconcile ? original.decision.record_ids : [] }] };
      const payload = JSON.stringify(request);
      if (retry.current?.payload !== payload) retry.current = { payload, body: { ...request, idempotency_key: crypto.randomUUID() } };
    }
    if (!retry.current) return;
    inFlight.current = true;
    setSaving(true); setError('');
    try {
      await onSubmit(retry.current.body);
      if (!mounted.current) return;
      setObserved(''); setReconcile(false); setBase(null); setUncertain(false); retry.current = undefined;
    } catch (e) {
      if (!mounted.current) return;
      const definite = e instanceof AuthenticatedFetchError && [400, 401, 403, 404, 409, 413, 422].includes(e.status);
      if (uncertain || !definite) {
        setUncertain(true); setError('The observation outcome is unknown. Retry the original observation before editing or starting another one.');
      } else setError(e instanceof Error ? e.message : 'Observation could not be recorded. Refresh the current scope and retry.');
    } finally { inFlight.current = false; if (mounted.current) setSaving(false); }
  };
  const renderHistory = (rows: typeof history) => rows.map(record => {
    const observation = record.observations.find(o => o.decision_id === decisionId)!;
    return <details key={record.id} className="rounded-lg border border-gray-200 dark:border-gray-700">
      <summary className="cursor-pointer px-3 py-2 text-sm">{record.revoked ? 'Revoked' : decisionStatusLabel(observation.result)} · {record.actor_id} · {new Date(record.created_at).toLocaleString()}</summary>
      <dl className="space-y-2 border-t border-gray-200 p-3 text-sm dark:border-gray-700">
        <div><dt className="font-medium">Expected</dt><dd className="whitespace-pre-wrap">{observation.expected}</dd></div>
        <div><dt className="font-medium">Observed</dt><dd className="whitespace-pre-wrap">{observation.observed}</dd></div>
        <div><dt className="font-medium">Sources</dt><dd>{observation.sources.map(s => <p key={s.reference} className="break-all">{s.reference} · {s.revision} · {s.sha256.slice(0, 12)}</p>)}</dd></div>
        {observation.reconciles.length > 0 && <div><dt className="font-medium">Reconciles</dt><dd className="break-all">{observation.reconciles.join(', ')}</dd></div>}
        {observation.separation.warning && <p className="text-amber-700 dark:text-amber-400">Reviewer separation warning was recorded.</p>}
      </dl>
    </details>;
  });
  return <section className="space-y-3 border-t border-gray-200 pt-3 dark:border-gray-700" aria-label="Decision verification">
    <h4 className="text-sm font-semibold">{decisionStatusLabel(current?.status || 'planning_pending')}</h4>
    {changed && <div className="space-y-2 rounded-lg border border-amber-300 p-3 text-sm dark:border-amber-700">
      <p>Your observation belongs to an earlier inspection base. Review the current condition and sources before recording it.</p>
      <p className="whitespace-pre-wrap">Original condition: {base?.decision.basis?.expected}</p>
      <button type="button" className="btn btn-secondary text-xs" disabled={!ready || saving || uncertain} onClick={() => {
        setBase(snapshot); setReconcile(false); retry.current = undefined; setError('');
      }}>Review inspection base</button>
    </div>}
    {current?.basis && <>
      <p className="text-sm">{current.basis.expected}</p>
      <details className="rounded-lg border border-gray-200 dark:border-gray-700"><summary className="cursor-pointer px-3 py-2 text-sm">Observed scope</summary>
        <div className="space-y-1 p-3 text-xs">{current.basis.sources.map(s => <p key={s.reference} className="break-all">{s.reference} · {s.revision} · {s.sha256.slice(0, 12)}</p>)}</div>
      </details>
      {canReview && current.separation?.allowed && <details className="rounded-lg border border-gray-200 dark:border-gray-700">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Record inspection</summary>
        <div className="space-y-3 border-t border-gray-200 p-3 dark:border-gray-700">
          <p className="text-xs text-gray-500">Record what you inspected against this scope. Your authenticated observation is preserved; Pulse does not execute the inspection.</p>
          {current.separation.warning && <p role="status" className="text-sm text-amber-700 dark:text-amber-400">The Board permits this review with a separation warning.</p>}
          <label className="block text-sm">Observed result<textarea disabled={!ready || saving || uncertain} aria-label="Observed result" rows={3} maxLength={2000} value={observed} onChange={e => { begin(); setObserved(e.target.value); }} className="mt-1 w-full rounded-lg border p-2 dark:border-gray-600 dark:bg-gray-800" /></label>
          <label className="block text-sm">Conclusion<select disabled={!ready || saving || uncertain} aria-label="Inspection conclusion" value={result} onChange={e => { begin(); setResult(e.target.value as DecisionReviewResult); }} className="ml-2 rounded-lg border p-2 dark:border-gray-600 dark:bg-gray-800">
            {(['inconclusive', 'passed', 'failed', 'unavailable', 'aborted'] as const).map(v => <option key={v} value={v}>{decisionStatusLabel(v)}</option>)}
          </select></label>
          {!!current.record_ids.length && <label className="flex items-center gap-2 text-sm"><input disabled={!ready || saving || uncertain || changed} type="checkbox" checked={reconcile} onChange={e => { begin(); setReconcile(e.target.checked); }} />Reconcile all current observations with this conclusion</label>}
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button type="button" className="btn btn-primary text-sm" disabled={saving || uncertain || changed || !ready || !observed.trim()} onClick={() => void submit()}>{saving ? 'Recording…' : 'Record observation'}</button>
          {uncertain && <button type="button" className="btn btn-secondary text-sm" disabled={saving} onClick={() => void submit()}>Retry original observation</button>}
        </div>
      </details>}
      {canReview && current.separation && !current.separation.allowed && <p className="text-sm text-amber-700 dark:text-amber-400">An independent reviewer with known authorship is required by the Board.</p>}
    </>}
    {currentHistory.length > 0 && <details><summary className="cursor-pointer text-sm">Observations in this edition ({currentHistory.length})</summary><div className="mt-2 space-y-2">{renderHistory(currentHistory)}</div></details>}
    {prior.length > 0 && <details><summary className="cursor-pointer text-sm">Previous versions</summary><div className="mt-2 space-y-2">{renderHistory(prior)}</div></details>}
  </section>;
}
