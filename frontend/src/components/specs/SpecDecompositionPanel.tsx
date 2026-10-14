import { useEffect, useRef, useState } from 'react';
import { PreviousResultsSection } from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useDashboardApi } from '@/services/api';
import { SpecDecompositionHistory } from './SpecDecompositionHistory';
import { decompositionReady, emptyDecomposition, SpecDecompositionFields } from './SpecDecompositionFields';

export function SpecDecompositionPanel({ specId, edition, canSubmit, refreshKey, onRecorded }: {
  specId: string; edition: number; canSubmit: boolean; refreshKey: number; onRecorded: () => void;
}) {
  const api = useDashboardApi();
  const [previous, setPrevious] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(emptyDecomposition);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const basis = useRef({ edition, refreshKey });
  useEffect(() => {
    if (editing && (basis.current.edition !== edition || basis.current.refreshKey !== refreshKey)) setChanged(true);
    basis.current = { edition, refreshKey };
  }, [edition, refreshKey, editing]);
  const submit = async () => {
    if (!canSubmit || submitting || changed || !decompositionReady(draft)) return;
    setSubmitting(true); setError(null);
    try {
      await api.submitSpecEvaluation(specId, draft);
      setEditing(false); setDraft(emptyDecomposition()); onRecorded();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Opinion could not be recorded.'); }
    finally { setSubmitting(false); }
  };
  return <div className="space-y-4">
    <p className="text-xs text-surface-600 dark:text-surface-300">
      These opinions assess the planned Tasks, Tests and dependencies. They remain separate from Spec quality.
      Recording an opinion does not promote the Spec; the planning conclusion checks every applicable gate.
    </p>
    <SpecDecompositionHistory specId={specId} edition={edition} view="current" refreshKey={refreshKey} />
    {canSubmit && !editing && <button className="btn btn-secondary text-xs" onClick={() => setEditing(true)}>Contribute a decomposition opinion</button>}
    {canSubmit && editing && <section className="space-y-3 rounded-xl border border-surface-200 p-4 dark:border-surface-700">
      <SpecDecompositionFields value={draft} onChange={setDraft} disabled={submitting} />
      {changed && <div role="alert" className="space-y-2 text-xs text-amber-700 dark:text-amber-300">
        <p>The planning inputs changed. Your draft is preserved. Review the current plan before recording this opinion.</p>
        <button className="btn btn-secondary" onClick={() => setChanged(false)}>I reviewed the current plan</button>
      </div>}
      {error && <p role="alert" className="text-xs text-red-700 dark:text-red-300">{error}</p>}
      <div className="flex gap-2">
        <button className="btn btn-secondary" disabled={submitting} onClick={() => { setEditing(false); setDraft(emptyDecomposition()); setChanged(false); }}>Discard draft</button>
        <button className="btn btn-primary" disabled={submitting || changed || !decompositionReady(draft)} onClick={() => void submit()}>
          {submitting ? 'Recording…' : 'Record decomposition opinion'}
        </button>
      </div>
    </section>}
    <PreviousResultsSection expanded={previous} onToggle={() => setPrevious(value => !value)}
      title="Previous decomposition opinions" description="Full opinions from earlier editions, with their original authors and scores.">
      <SpecDecompositionHistory specId={specId} edition={edition} view="previous" refreshKey={refreshKey} />
    </PreviousResultsSection>
  </div>;
}
