import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { CoverageSection, ProofStatus, SpecCoverageResponse } from './specCoverageTypes';

const dimensions: [string, string, string, CoverageSection][] = [
  ['Acceptance criteria → scenarios', 'ac_covered', 'ac_total', 'tests'],
  ['Functional requirements → rules', 'fr_covered', 'fr_total', 'rules'],
  ['Scenarios → Test Cards', 'scenarios_linked', 'scenarios_total', 'tests'],
  ['Rules → Cards', 'brs_linked', 'brs_total', 'rules'],
  ['API contracts → Cards', 'contracts_linked', 'contracts_total', 'contracts'],
  ['Technical requirements → Cards', 'trs_linked', 'trs_total', 'trs'],
  ['Decisions → Cards', 'decisions_linked', 'decisions_total', 'decisions'],
  ['Integration requirements → Cards', 'irs_linked', 'irs_total', 'irs'],
  ['Observability requirements → Cards', 'ors_linked', 'ors_total', 'ors'],
];
const proofLabels: Record<ProofStatus, string> = { unknown: 'Unknown', proven: 'Proven',
  partial: 'Partial proof', missing: 'Proof missing', satisfied_with_waiver: 'Satisfied by waiver (not proof)' };
const observations = { observed: 'Observed', observed_expected: 'Expected relation observed',
  graph_only: 'Graph observation only; not confirmed by this source comparison',
  not_found_in_projection: 'Not found in the available projection' };
const button = 'border rounded px-2 py-1 dark:border-gray-600';

/** Lazy contextual read. Navigation never repairs the graph or changes a gate. */
export function SpecCoveragePanel({ boardId, specId, revision, canCorrect = [], onOpenSection }: {
  boardId: string; specId: string; revision: string; canCorrect?: CoverageSection[];
  onOpenSection: (section: CoverageSection) => void;
}) {
  const api = useDashboardApi();
  const [data, setData] = useState<SpecCoverageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const load = useCallback(async (previous: SpecCoverageResponse | null = null) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const id = ++sequence.current;
    setLoading(true); setError(null);
    if (!previous) setData(null);
    try {
      const page = await api.getSpecCoverage(boardId, specId,
        { limit: 200, cursor: previous?.next_cursor }, controller.signal);
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(previous ? { ...page, items: [...previous.items, ...page.items] } : page);
    } catch (failure) {
      if (controller.signal.aborted || sequence.current !== id) return;
      setData(null);
      const status = failure instanceof AuthenticatedFetchError ? failure.status : null;
      setError(status === 403 ? 'You do not have permission to read this coverage scope.'
        : status === 409 ? 'The source or graph changed. Reload to start a new observation.'
        : status === 404 ? 'Spec not found or unavailable.' : 'Coverage could not be loaded. Reload to try again.');
    } finally {
      if (!controller.signal.aborted && sequence.current === id) setLoading(false);
    }
  }, [api, boardId, specId]);
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load, revision]);
  const summary = data?.structure.summary;
  return <section className="space-y-4" aria-label="Spec coverage">
    <h3 className="font-semibold">Coverage</h3>
    <p>Planning links, graph observations and admitted delivery proof are separate. This view does not approve a gate.</p>
    <button type="button" className={button} disabled={loading} onClick={() => void load()}>Reload coverage</button>
    {loading && <p role="status">Loading coverage…</p>}
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>Edition {data.edition} · Graph freshness: {data.projection_freshness.state} · Overall completeness: not established.</p>
      <p>Missing graph observations are not proof of missing work. Observation: {data.projection_freshness.checked_at}.</p>
      <section aria-label="Authoritative structural coverage" className="space-y-2">
        <h4 className="font-semibold">Structural coverage from the source</h4>
        <p>Uses the existing coverage resolver. A linked Test Card does not prove a passing run.</p>
        {!summary ? <p>Source coverage is unknown.</p> : <ul className="space-y-2">{dimensions.map(([label, numerator, denominator, section]) => {
          const n = summary[numerator]; const d = summary[denominator];
          const gap = typeof n === 'number' && typeof d === 'number' && n < d;
          return <li key={label}>{label}: {typeof n === 'number' ? n : 'Unknown'}/{typeof d === 'number' ? d : 'Unknown'}
            {d === 0 ? ' (no items in scope)' : gap ? ' — planning gap' : ''}
            {' '}<button type="button" className={button} onClick={() => onOpenSection(section)}>Open {section}</button>
            {gap && canCorrect.includes(section) && <> <button type="button" className={button} onClick={() => onOpenSection(section)}>Correct {section} links</button></>}
          </li>;
        })}</ul>}
      </section>
      <section aria-label="Admitted delivery proof" className="space-y-2">
        <h4 className="font-semibold">Implementation and verification proof</h4>
        <p>Proof access: {data.delivery.state}. Obligations: {data.delivery.counts.obligations ?? 'Unknown'}.
          {' '}Implementation proven: {data.delivery.counts.implementation_proven ?? 'Unknown'}.
          {' '}Verification proven: {data.delivery.counts.verification_proven ?? 'Unknown'}.</p>
        <p>Counts cover the whole authorized scope, not this page. Waivers do not count as proven work.</p>
        {data.items.filter(item => item.kind === 'delivery').map(item => <article className="border rounded p-2" key={item.obligation_ref}>
          <h5>{item.title || item.obligation_ref}</h5>
          <p>Implementation: {proofLabels[item.implementation]}. Verification: {proofLabels[item.verification]}.</p>
          <details><summary>Obligation and proof references</summary>
            <p className="break-all">{item.obligation_ref} · Revision {item.semantic_sha256}</p>
            <p>Implementation: {item.implementation_record_refs.join(', ') || 'None admitted'}</p>
            <p>Verification: {item.verification_record_refs.join(', ') || 'None admitted'}</p>
            <p>Waivers: {[...item.implementation_waiver_refs, ...item.verification_waiver_refs].join(', ') || 'None'}</p>
            <p>Missing Cards: {item.missing_card_refs.join(', ') || 'None reported'}</p>
            <p>Missing criteria: {item.missing_criteria.map(parts => parts.join(' / ')).join(', ') || 'None reported'}</p>
          </details>
        </article>)}
        {data.delivery.blockers.length > 0 && <details><summary>Delivery findings</summary><ul>{data.delivery.blockers.map(code => <li key={code}>{code}</li>)}</ul></details>}
      </section>
      <section aria-label="Structural graph observations" className="space-y-2">
        <h4 className="font-semibold">Structural graph observations</h4>
        <p>Graph access: {data.structure.graph.state}. Source nodes: {data.structure.graph.expected_nodes ?? 'Unknown'}.
          {' '}Observed nodes: {data.structure.graph.observed_nodes ?? 'Unknown'}.
          {' '}Not found: {data.structure.graph.missing_nodes ?? 'Unknown'} nodes, {data.structure.graph.missing_relations ?? 'Unknown'} expected relations.</p>
        <ul className="space-y-2 break-all">{data.items.map((item, index) => item.kind === 'delivery' ? null : <li key={index}>
          {item.kind === 'structure_node' ? `${item.node_type}: ${item.subject_ref}` : `${item.source_ref} → ${item.relation} → ${item.target_ref}`}
          {' '}— {observations[item.observation]}
          {item.kind === 'structure_relation' && <details><summary>Relation provenance</summary>{item.rule_id} · {item.layer} · {item.created_by}</details>}
        </li>)}</ul>
      </section>
      {data.next_cursor && <button type="button" className={button} disabled={loading} onClick={() => void load(data)}>Load more coverage</button>}
    </>}
  </section>;
}
