import { useEffect, useRef, useState } from 'react';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { useDashboardApi } from '@/services/api';
import type { SpecEvaluationList } from '@/types';
import { decompositionFields } from './SpecDecompositionFields';

export function SpecDecompositionHistory({ specId, edition, view, refreshKey = 0 }: {
  specId: string; edition: number; view: 'current' | 'previous'; refreshKey?: number;
}) {
  const api = useDashboardApi();
  const apiRef = useRef(api); apiRef.current = api;
  const [page, setPage] = useState(0);
  const [data, setData] = useState<SpecEvaluationList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => { setPage(0); setData(null); }, [specId, edition, view, refreshKey]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setData(null);
    apiRef.current.listSpecEvaluations(specId, { view, offset: page * 10, limit: 10, signal: controller.signal })
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.spec_id !== specId || result.current_edition !== edition || result.view !== view) {
          throw new Error('The opinion history no longer matches this edition. Refresh the Spec.');
        }
        setData(result);
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'History unavailable.');
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [specId, edition, view, page, retry, refreshKey]);
  if (loading) return <PulseLoader size="sm" label="Loading decomposition opinions…" className="py-4" />;
  if (error) return <div role="alert" className="space-y-2 text-xs text-red-700 dark:text-red-300">
    <p>{error}</p><button className="btn btn-secondary" onClick={() => setRetry(value => value + 1)}>Retry history</button>
  </div>;
  if (!data?.evaluations.length) return <p className="text-xs text-surface-500">No {view} decomposition opinions.</p>;
  return <div className="space-y-3">
    {data.evaluations.map(opinion => <details key={opinion.id}
      className="rounded-xl border border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/40">
      <summary className="flex cursor-pointer items-center justify-between gap-3 p-3 text-xs">
        <span><span className="block font-semibold text-surface-900 dark:text-white">{opinion.evaluator_name || opinion.evaluator_id}</span>
          <span className="text-surface-500">Edition {opinion.spec_edition} · {new Date(opinion.created_at).toLocaleString()}</span></span>
        <span className="rounded-full bg-surface-100 px-2 py-1 capitalize dark:bg-surface-800">{opinion.recommendation.replace(/_/g, ' ')}</span>
        <span aria-label={`Overall decomposition score ${opinion.overall_score} of 100`}
          className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full border-4 border-violet-400 text-sm font-semibold tabular-nums text-surface-900 dark:text-white">
          {opinion.overall_score}
        </span>
      </summary>
      <div className="grid gap-3 border-t border-surface-200 p-3 dark:border-surface-700 sm:grid-cols-2">
        {decompositionFields.map(({ score, label }) => {
          const metric = score === 'overall_score'
            ? { score: opinion.overall_score, justification: opinion.overall_justification }
            : opinion.dimensions[score];
          return <section key={score} className="flex items-start gap-3 rounded-lg bg-surface-50 p-3 dark:bg-surface-800/50">
            <span aria-label={`${label} score ${metric.score} of 100`}
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border-4 border-violet-300 text-xs font-semibold tabular-nums dark:border-violet-700">{metric.score}</span>
            <div><h4 className="text-xs font-semibold">{label}</h4><p className="mt-1 whitespace-pre-wrap text-xs text-surface-600 dark:text-surface-300">{metric.justification}</p></div>
          </section>;
        })}
      </div>
    </details>)}
    <div className="flex items-center justify-between gap-2 text-xs">
      <button className="btn btn-secondary" disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous page</button>
      <span>{data.offset + 1}–{Math.min(data.offset + data.evaluations.length, data.total)} of {data.total}</span>
      <button className="btn btn-secondary" disabled={data.offset + data.evaluations.length >= data.total}
        onClick={() => setPage(value => value + 1)}>Next page</button>
    </div>
  </div>;
}
