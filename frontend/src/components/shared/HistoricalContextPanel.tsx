import { useEffect, useState } from 'react';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useHistoricalContextApi, type ContextTargetKind, type HistoricalContextPage } from '@/services/historical-context-api';
import type { ArchiveSection } from '@/services/historical-archives-api';

interface Props { boardId: string; targetKind: ContextTargetKind; targetId: string }
const labels: Record<ArchiveSection, string> = {
  content: 'Context', qa: 'Question & answer', evaluations: 'Historical evaluation', history: 'Historical activity',
};
const buttonClass = 'rounded border border-surface-300 dark:border-surface-600 px-3 py-2 text-sm hover:bg-surface-100 dark:hover:bg-surface-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent-600';

function errorMessage(error: unknown): string {
  if (error instanceof AuthenticatedFetchError) {
    if ([401, 403, 404].includes(error.status)) return 'This context is unavailable or you no longer have access.';
    if (error.status === 413) return 'This context exceeds the supported reading limit.';
    if (error.status === 503) return 'The historical context could not be verified. Try again later.';
  }
  return 'Unable to load historical context. Please try again.';
}

function ContextView({ boardId, targetKind, targetId }: Props) {
  const api = useHistoricalContextApi();
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<HistoricalContextPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    api.read(boardId, targetKind, targetId, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) setPage(result);
    }).catch(error => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    });
    return () => controller.abort();
  }, [api, boardId, targetKind, targetId, offset, retry]);
  const refresh = () => { setPage(null); setError(null); setOffset(0); setRetry(value => value + 1); };
  const go = (value: number) => { setPage(null); setError(null); setOffset(value); };
  return <section aria-label="Historical context" className="min-w-0 space-y-4">
    <p className="text-sm text-surface-600 dark:text-surface-400">Original context linked to this {targetKind === 'spec' ? 'Spec' : 'Card'}. It is read-only and does not approve current work.</p>
    {error ? <div role="alert"><p>{error}</p><button className={buttonClass} onClick={() => setRetry(value => value + 1)}>Retry context</button></div>
      : !page ? <p role="status">Loading historical context…</p>
        : page.items.length === 0 ? <p>No historical context available.</p>
          : page.items.map(item => <article key={item.binding_id} aria-label={labels[item.section]} className="min-w-0 rounded-lg border border-surface-200 dark:border-surface-700 p-4">
            <h3 className="font-semibold">{labels[item.section]}</h3>
            <p className="mb-3 break-words text-sm text-surface-600 dark:text-surface-400 [overflow-wrap:anywhere]">Historical origin: {item.origin.kind} · {item.origin.id}</p>
            <dl className="space-y-3">
              {Object.entries(item.record).map(([field, value]) => <div key={field}>
                <dt className="text-sm font-semibold capitalize">{field.replace(/_/g, ' ')}</dt>
                <dd className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">
                  {value === null ? '—' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
                </dd>
              </div>)}
            </dl>
          </article>)}
    <div className="flex flex-wrap gap-2">
      {offset > 0 && <button className={buttonClass} onClick={() => go(0)}>First context page</button>}
      {page?.next_offset != null && <button className={buttonClass} onClick={() => go(page.next_offset!)}>Next context page</button>}
      <button className={buttonClass} onClick={refresh}>Refresh context</button>
    </div>
  </section>;
}

export function HistoricalContextPanel(props: Props) {
  // A new Board or destination must never render the previous destination's data.
  return <ContextView key={JSON.stringify([props.boardId, props.targetKind, props.targetId])} {...props} />;
}
