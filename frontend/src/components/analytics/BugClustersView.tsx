import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { BugClusterGrouping, BugClustersQuery, BugClustersResponse } from './bugClustersTypes';

function initialQuery(): BugClustersQuery {
  const today = new Date();
  const first = new Date(today);
  first.setUTCDate(first.getUTCDate() - 14);
  return { from: first.toISOString().slice(0, 10), to: today.toISOString().slice(0, 10), group_by: 'proxy', limit: 200 };
}

const groupingLabels: Record<BugClusterGrouping, string> = {
  proxy: 'Origin association', spec: 'Origin Spec', learning: 'Recorded Learning', severity: 'Severity',
};
const inputClass = 'border rounded px-2 py-1 bg-white dark:bg-gray-800 dark:border-gray-600';

/** Mounted only on the dedicated Analytics route; never a Board-wide prefetch. */
export function BugClustersView({ boardId, onBack }: { boardId: string; onBack: () => void }) {
  const api = useDashboardApi();
  const [query, setQuery] = useState(initialQuery);
  const [draft, setDraft] = useState(query);
  const [data, setData] = useState<BugClustersResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<{ controller: AbortController; sequence: number } | null>(null);
  const sequence = useRef(0);

  const load = useCallback(async (previous: BugClustersResponse | null = null) => {
    request.current?.controller.abort();
    const controller = new AbortController();
    const id = ++sequence.current;
    request.current = { controller, sequence: id };
    setLoading(true);
    setError(null);
    if (!previous) setData(null);
    try {
      const page = await api.getBugClusters(boardId, previous
        ? { ...query, ...previous.window, cursor: previous.next_cursor }
        : query, controller.signal);
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(previous ? { ...page, items: [...previous.items, ...page.items] } : page);
    } catch (failure) {
      if (controller.signal.aborted || sequence.current !== id) return;
      // A failed page never leaves earlier pages presented as current data.
      setData(null);
      const status = failure instanceof AuthenticatedFetchError ? failure.status : null;
      setError(status === 403 ? 'You do not have permission to read this grouping.'
        : status === 409 ? 'The query scope changed. Reload to start a new observation.'
        : status === 404 ? 'Board not found or unavailable.'
        : 'Clusters could not be loaded. Reload to try again.');
    } finally {
      if (!controller.signal.aborted && sequence.current === id) setLoading(false);
    }
  }, [api, boardId, query]);

  useEffect(() => {
    void load();
    return () => { request.current?.controller.abort(); };
  }, [load]);

  return <section className="space-y-4" aria-label="Bug clusters">
    <button type="button" onClick={onBack} className={inputClass}>Back to Board analytics</button>
    <h2 className="text-lg font-semibold">Bug clusters</h2>
    <p className="text-sm text-gray-600 dark:text-gray-300">
      Origin associations and recorded Learnings do not establish a common cause or a validated risk.
      Counts represent distinct Bugs in the selected creation window. A Bug can belong to several clusters.
    </p>
    <form className="flex flex-wrap gap-3 items-end" onSubmit={event => {
      event.preventDefault();
      setQuery({ ...draft });
    }}>
      <label className="grid gap-1">Created from (UTC)
        <input className={inputClass} type="date" required value={draft.from} max={draft.to}
          onChange={e => setDraft({ ...draft, from: e.target.value })} />
      </label>
      <label className="grid gap-1">Created through (UTC)
        <input className={inputClass} type="date" required value={draft.to} min={draft.from}
          onChange={e => setDraft({ ...draft, to: e.target.value })} />
      </label>
      <label className="grid gap-1">Group by
        <select className={inputClass} value={draft.group_by} onChange={e => setDraft({ ...draft, group_by: e.target.value as BugClusterGrouping })}>
          {Object.entries(groupingLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="grid gap-1">Severity
        <select className={inputClass} value={draft.severity ?? ''} onChange={e => setDraft({ ...draft, severity: e.target.value })}>
          <option value="">All severities</option>
          {['critical', 'major', 'minor'].map(value => <option key={value}>{value}</option>)}
        </select>
      </label>
      <label className="grid gap-1">Status
        <select className={inputClass} value={draft.status ?? ''} onChange={e => setDraft({ ...draft, status: e.target.value })}>
          <option value="">All statuses</option>
          {['not_started', 'started', 'in_progress', 'validation', 'rejected', 'on_hold', 'done', 'cancelled'].map(value => <option key={value}>{value}</option>)}
        </select>
      </label>
      <button className={inputClass} type="submit">Apply filters</button>
      <button className={inputClass} type="button" onClick={() => { const recent = initialQuery(); setDraft(recent); setQuery(recent); }}>Last 15 days</button>
    </form>
    {loading && <p role="status">Loading clusters…</p>}
    {error && <p role="alert">{error}</p>}
    <button className={inputClass} type="button" disabled={loading} onClick={() => void load()}>Reload</button>
    {data && <>
      <p>Distinct Bugs in scope: {data.distinct_bug_count ?? 'Unknown'}. Observed Bugs: {data.observed_bug_count}.
        {' '}Clusters in scope: {data.cluster_count ?? 'Unknown'}. Observed clusters: {data.observed_cluster_count}.</p>
      <p>Source: {data.data_source === 'relational' ? 'authoritative relational records' : 'relational records and graph observations'}.
        {' '}Graph freshness: {data.projection_freshness.state}. Scope completeness: {data.completeness.complete_for_scope ? 'complete' : 'not established'}.</p>
      {!data.completeness.complete_for_scope && <p role="status">Available observations may be incomplete. Missing associations are not proof of absence.</p>}
      {data.items.length === 0 && <p>{data.completeness.complete_for_scope ? 'No clusters in this scope.' : 'No clusters found in the available observations.'}</p>}
      <p className="text-sm">Duration runs from source creation to the latest verified Done transition of currently Done Bugs.
        It includes reopened periods and is not operational MTTR. Missing timestamps remain unknown; the median includes only Bugs with verified timestamps.</p>
      <div className="space-y-3">
        {data.items.map(item => <article key={`${item.target_ref}:${item.validity}`} className="border dark:border-gray-700 rounded p-3 space-y-2">
          <h3 className="font-medium">{item.title}</h3>
          <p>{groupingLabels[data.group_by]} · Validity: {item.validity} · Graph: {item.projection_freshness}</p>
          <p>Distinct Bugs: {item.distinct_bug_count ?? 'Unknown'}; observed: {item.observed_bug_count}; currently Done: {item.observed_done_count}.</p>
          <p>Median hours from creation to latest completion: {item.observed_median_resolution_hours === null ? 'Unknown' : item.observed_median_resolution_hours.toFixed(1)}
            {' '}({item.observed_resolution_timestamp_count} verified timestamps).</p>
          <details><summary>Bug references and provenance</summary>
            <p className="break-all">Target: {item.target_ref ?? 'Unspecified'}</p>
            <ul>{item.bug_refs.map(ref => <li key={ref}><a className="underline" href={`/analytics/boards/${encodeURIComponent(boardId)}/entities/card/${encodeURIComponent(ref.slice(5))}`}>{ref}</a></li>)}</ul>
            <ul>{item.provenance_refs.map(ref => <li className="break-all" key={ref}>{ref}</li>)}</ul>
          </details>
        </article>)}
      </div>
      {data.next_cursor && <button className={inputClass} type="button" disabled={loading} onClick={() => void load(data)}>Load more clusters</button>}
    </>}
  </section>;
}
