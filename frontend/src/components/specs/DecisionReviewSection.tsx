import { useRef, useState } from 'react';
import type { DecisionReviewInput, DecisionReviewProjection, DecisionReviewResult } from '@/types/decision-reviews';

export const decisionStatusLabel = (status: string) => ({
  verified: 'Verified', planning_pending: 'Plan required', obligations_pending: 'Obligation evidence pending',
  inspection_pending: 'Inspection pending', conflict: 'Conflicting observations', passed: 'Passed',
  failed: 'Failed', inconclusive: 'Inconclusive', unavailable: 'Unavailable', revoked: 'Revoked', aborted: 'Aborted',
}[status] || 'Verification pending');

export function DecisionReviewSection({ data, decisionId, canReview, onSubmit }: {
  data: DecisionReviewProjection; decisionId: string; canReview: boolean;
  onSubmit: (body: DecisionReviewInput) => Promise<void>;
}) {
  const [observed, setObserved] = useState('');
  const [result, setResult] = useState<DecisionReviewResult>('inconclusive');
  const [reconcile, setReconcile] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const retry = useRef<{ payload: string; key: string }>();
  const current = data.decisions.find(d => d.decision_id === decisionId);
  const history = data.history.filter(r => r.observations.some(o => o.decision_id === decisionId));
  const prior = history.filter(r => r.edition !== data.edition);
  const currentHistory = history.filter(r => r.edition === data.edition);
  const submit = async () => {
    if (!current?.basis || !observed.trim() || saving) return;
    const request = { expected_edition: data.edition, expected_version: data.version, expected_review_revision: data.review_revision,
      entries: [{ decision_id: decisionId, expected_scope_sha256: current.basis.scope_sha256, observed: observed.trim(), result,
        sources: current.basis.sources, reconciles: reconcile ? current.record_ids : [] }] };
    const payload = JSON.stringify(request);
    if (retry.current?.payload !== payload) retry.current = { payload, key: crypto.randomUUID() };
    setSaving(true); setError('');
    try { await onSubmit({ ...request, idempotency_key: retry.current.key }); setObserved(''); setReconcile(false); retry.current = undefined; }
    catch (e) { setError(e instanceof Error ? e.message : 'Observation could not be recorded. Refresh the current scope and retry.'); }
    finally { setSaving(false); }
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
          <label className="block text-sm">Observed result<textarea aria-label="Observed result" rows={3} maxLength={2000} value={observed} onChange={e => setObserved(e.target.value)} className="mt-1 w-full rounded-lg border p-2 dark:border-gray-600 dark:bg-gray-800" /></label>
          <label className="block text-sm">Conclusion<select aria-label="Inspection conclusion" value={result} onChange={e => setResult(e.target.value as DecisionReviewResult)} className="ml-2 rounded-lg border p-2 dark:border-gray-600 dark:bg-gray-800">
            {(['inconclusive', 'passed', 'failed', 'unavailable', 'aborted'] as const).map(v => <option key={v} value={v}>{decisionStatusLabel(v)}</option>)}
          </select></label>
          {!!current.record_ids.length && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={reconcile} onChange={e => setReconcile(e.target.checked)} />Reconcile all current observations with this conclusion</label>}
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button type="button" className="btn btn-primary text-sm" disabled={saving || !observed.trim()} onClick={() => void submit()}>{saving ? 'Recording…' : 'Record observation'}</button>
        </div>
      </details>}
      {canReview && current.separation && !current.separation.allowed && <p className="text-sm text-amber-700 dark:text-amber-400">An independent reviewer with known authorship is required by the Board.</p>}
    </>}
    {currentHistory.length > 0 && <details><summary className="cursor-pointer text-sm">Observations in this edition ({currentHistory.length})</summary><div className="mt-2 space-y-2">{renderHistory(currentHistory)}</div></details>}
    {prior.length > 0 && <details><summary className="cursor-pointer text-sm">Previous versions</summary><div className="mt-2 space-y-2">{renderHistory(prior)}</div></details>}
  </section>;
}
