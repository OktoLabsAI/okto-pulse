import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SourceLineageResponse } from './lineageQueryTypes';

const button = 'border rounded px-2 py-1 dark:border-gray-600';

export function LineagePathsPanel({ boardId, subjectRef, revision, onContinue }: {
  boardId: string; subjectRef: string; revision: number; onContinue: (subjectRef: string) => void;
}) {
  const api = useDashboardApi();
  const [data, setData] = useState<SourceLineageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [depth, setDepth] = useState(3);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const load = useCallback(async (previous: SourceLineageResponse | null = null) => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const id = ++sequence.current;
    setLoading(true); setError(null);
    if (!previous) setData(null);
    try {
      const page = await api.getSourceLineage(boardId,
        { subject_ref: subjectRef, limit: 200, max_depth: depth, cursor: previous?.next_cursor }, controller.signal);
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(previous ? { ...page, items: [...previous.items, ...page.items] } : page);
    } catch (failure) {
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(null);
      const status = failure instanceof AuthenticatedFetchError ? failure.status : null;
      setError(status === 403 ? 'You do not have permission to read all source families in this lineage.'
        : status === 409 ? 'The source changed or exceeded the query limit. Reload or choose another subject.'
        : status === 404 ? 'Lineage subject not found or unavailable.' : 'Lineage could not be loaded. Reload to try again.');
    } finally {
      if (!controller.signal.aborted && sequence.current === id) setLoading(false);
    }
  }, [api, boardId, subjectRef, depth]);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load, revision]);
  return <section aria-label="Source lineage paths" className="space-y-3 text-sm">
    <h3 className="font-semibold">Source lineage paths</h3>
    <p className="break-all">Subject: {subjectRef}</p>
    <p>Declared workflow origins, dependencies and amendments. These paths do not establish execution eligibility or delivery proof.</p>
    <p>An amendment may correct only part of a Spec. Its association does not supersede the whole original Spec.</p>
    <button type="button" className={button} disabled={loading} onClick={() => void load()}>Reload source paths</button>
    {loading && <p role="status">Loading source paths…</p>}
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>Source scope: {data.completeness.complete_for_scope ? 'complete' : 'partial'}. Graph projection: not read; freshness remains unknown.</p>
      <p>Source nodes: {data.counts.source_nodes}. Source relations: {data.counts.source_relations}. Reached targets: {data.counts.reached_targets}.</p>
      <p>Counts cover this authorized source observation, not just this page. Exploration depth: {data.scope.max_depth}.</p>
      {data.completeness.limitations.includes('source_endpoint_unavailable') && <p>A source endpoint is unavailable; absence is not established.</p>}
      {data.completeness.truncated && <p>Partial exploration: the frontier is not exhausted.</p>}
      {data.frontier_refs.length > 0 && depth < 32 && <button type="button" className={button} disabled={loading}
        onClick={() => setDepth(value => Math.min(32, value + 3))}>Explore more hops</button>}
      {data.items.length === 0 && <p>No related workflow sources found in this observation.</p>}
      <ul className="space-y-2">{data.items.map(item => <li className="border rounded p-2" key={item.subject_ref}>
        <p className="font-semibold">{item.title} · {item.entity_type} · {item.status}</p>
        <p className="break-all">{item.subject_ref} · {item.depth} hop{item.depth === 1 ? '' : 's'}</p>
        <details><summary>Show lineage path</summary><ol className="list-decimal list-inside space-y-1 break-all">
          {item.path.map((step, index) => <li key={index}>{step.source_ref} → {step.relation} → {step.target_ref} ({step.direction})
            <p>Declared source: {step.provenance_ref}</p>
          </li>)}
        </ol></details>
      </li>)}</ul>
      {data.next_cursor && <button type="button" className={button} disabled={loading} onClick={() => void load(data)}>Load more source paths</button>}
      {data.frontier_refs.length > 0 && <details><summary>Continue from the frontier</summary>
        <p>Each continuation starts a new scoped observation from the chosen source.</p>
        <ul className="space-y-1">{data.frontier_refs.map(ref => <li key={ref}>
          <button type="button" className={`${button} break-all`} onClick={() => onContinue(ref)}>Continue from {ref}</button>
        </li>)}</ul>
      </details>}
    </>}
  </section>;
}
