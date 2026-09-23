import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';

import type { SpecEvaluationList } from '@/types';

export function SpecEvaluationHistory({ specId, edition, version, canRead }: {
  specId: string; edition: number; version: number; canRead: boolean;
}) {
  const api = useDashboardApi();
  const [data, setData] = useState<SpecEvaluationList | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    setData(null);
    setError(false);
    if (!canRead) return;
    const controller = new AbortController();
    async function load() {
      try {
        const result = await api.listSpecEvaluations(specId, controller.signal);
        if (!controller.signal.aborted) setData(result);
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }
    void load();
    return () => controller.abort();
  }, [api, specId, edition, version, canRead]);
  if (!canRead) return null;
  return <section aria-label="Decomposition evaluations" className="mb-4 space-y-3 rounded-xl border border-surface-200 p-4 dark:border-surface-700">
    <h3 className="font-semibold">Decomposition evaluations</h3>
    <p className="text-sm">Current reviews apply to this Spec edition. Reopening requires a new review; previous results remain in history.</p>
    {error ? <p role="alert">Evaluations are unavailable. Current approval is unknown.</p>
      : !data ? <p role="status">Loading evaluations…</p>
      : <>
        <p>Edition {data.current_edition}: {data.active_count} current, {data.previous_count} previous.</p>
        {!data.active_count && <p>No current evaluation. An earlier approval does not satisfy this edition’s review gate.</p>}
        {(['current', 'previous'] as const).map(state => <div key={state}>
          <h4 className="font-medium">{state === 'current' ? 'Current' : 'Previous'}</h4>
          <ul className="space-y-2">{data.evaluations.filter(item => item.lifecycle_state === state).map(item => <li key={item.id} className="rounded border border-surface-200 p-2 dark:border-surface-700">
            <p>{item.evaluator_name || item.evaluator_id}: {item.recommendation} · {item.overall_score}/100</p>
            <p>{item.spec_edition === undefined ? 'Original edition unknown' : `Edition ${item.spec_edition}`} · <time>{item.created_at}</time></p>
            <p className="whitespace-pre-wrap">{item.overall_justification}</p>
          </li>)}</ul>
        </div>)}
      </>}
  </section>;
}
