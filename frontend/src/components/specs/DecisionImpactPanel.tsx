import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { DecisionImpactResponse } from './decisionImpactTypes';

const button = 'border rounded px-2 py-1 dark:border-gray-600';
const interpretations = {
  potential_shared_card_reach: 'Potential reach through a shared Card; this does not establish what the scenario tests.',
  graph_only_observation: 'Potential reach based on a graph observation without a current source link.',
  declared_link_not_proven_change: 'Declared source link; this does not prove implementation or a change to this target.',
};

/** Mounted on demand by DecisionsTab; never writes or schedules found Cards. */
export function DecisionImpactPanel({ boardId, specId, decisionId, revision, onOpenDecision }: {
  boardId: string; specId: string; decisionId: string; revision: string;
  onOpenDecision: (decisionId: string) => void;
}) {
  const api = useDashboardApi();
  const [data, setData] = useState<DecisionImpactResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [depth, setDepth] = useState(3);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const load = useCallback(async (previous: DecisionImpactResponse | null = null) => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const id = ++sequence.current;
    setLoading(true); setError(null);
    if (!previous) setData(null);
    try {
      const page = await api.getDecisionImpact(boardId, specId, decisionId,
        { limit: 200, max_depth: depth, cursor: previous?.next_cursor }, controller.signal);
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(previous ? { ...page, items: [...previous.items, ...page.items] } : page);
    } catch (failure) {
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(null);
      const status = failure instanceof AuthenticatedFetchError ? failure.status : null;
      setError(status === 403 ? 'You do not have permission to read this impact scope.'
        : status === 409 ? 'The source or graph changed. Reload to start a new observation.'
        : status === 404 ? 'Decision not found or unavailable.' : 'Impact could not be loaded. Reload to try again.');
    } finally {
      if (!controller.signal.aborted && sequence.current === id) setLoading(false);
    }
  }, [api, boardId, specId, decisionId, depth]);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load, revision]);
  return <section aria-label="Decision impact" className="space-y-3 text-xs">
    <h4 className="font-semibold">Impact</h4>
    <p>Informational paths within this Spec. Source links and graph observations do not prove delivery or approve a gate.</p>
    <button type="button" className={button} disabled={loading} onClick={() => void load()}>Reload impact</button>
    {loading && <p role="status">Loading impact…</p>}
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>Decision: {data.decision_status}. Graph freshness: {data.projection_freshness.state}. Overall completeness: not established.</p>
      {!data.current_decision && <p>This is a historical Decision; its former links are not presented as current impact.</p>}
      <p>Targets reached: {data.counts.observed_targets}. Confirmed source links: {data.counts.confirmed_link_targets}.
        {' '}Potential reach: {data.counts.potential_targets}. Counts cover this exploration, not just this page.</p>
      <p>Exploration depth: {data.scope.max_depth}. One representative path per target; a confirmed source path takes precedence.</p>
      {data.completeness.truncated && <p>Partial exploration: a horizon or resource limit was reached.</p>}
      {data.frontier_refs.length > 0 && depth < 8 && <button type="button" className={button} disabled={loading}
        onClick={() => setDepth(value => Math.min(8, value + 2))}>Explore further</button>}
      {data.items.length === 0 && <p>No current impact paths found in this bounded observation.</p>}
      <ul className="space-y-2">{data.items.map(item => <li className="border rounded p-2" key={item.target_ref}>
        <p className="font-semibold">{item.title} · {item.target_type} · {item.status}</p>
        <p>{item.reach === 'direct' ? 'Direct' : 'Indirect'} · {item.certainty === 'potential' ? 'Potential reach' : 'Confirmed source link'}</p>
        <p>{interpretations[item.interpretation]}</p>
        <details><summary>Show impact path</summary>
          <ol className="list-decimal list-inside space-y-1 break-all">{item.path.map((step, index) => <li key={index}>
            {step.source_ref} → {step.relation} → {step.target_ref} ({step.direction})
            <p>Source link: {step.source_confirmed ? 'confirmed' : 'not confirmed'}; graph: {step.graph_observed ? 'observed' : 'not observed'}.</p>
            <p>Provenance: {step.rule_id} · {step.layer} · {step.created_by}</p>
          </li>)}</ol>
        </details>
      </li>)}</ul>
      {data.next_cursor && <button type="button" className={button} disabled={loading} onClick={() => void load(data)}>Load more impact</button>}
      <details><summary>Decision supersedence history</summary><ul className="space-y-1">{data.history.map(item => <li key={item.subject_ref}>
        <button type="button" className={button} disabled={item.status === 'unavailable' || item.subject_ref === data.subject_ref}
          onClick={() => onOpenDecision(item.subject_ref.split(':')[3])}>{item.title || 'Unavailable predecessor'}</button>
        {' '}— {item.status}{item.supersedes_ref && <span className="break-all"> · supersedes {item.supersedes_ref}</span>}
      </li>)}</ul></details>
    </>}
  </section>;
}
