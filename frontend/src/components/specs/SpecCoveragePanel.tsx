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
type CoverageView = 'overview' | 'planning' | 'implementation' | 'verification';
const views: [CoverageView, string][] = [['overview', 'Overview'], ['planning', 'Planning'],
  ['implementation', 'Implementation'], ['verification', 'Verification']];

function ratio(covered: unknown, total: unknown) {
  return typeof covered === 'number' && typeof total === 'number'
    && Number.isFinite(covered) && Number.isFinite(total) && covered >= 0 && total >= covered
    ? { covered, total, percent: total ? Math.round(covered / total * 100) : null } : null;
}

function CoverageBar({ label, covered, total }: { label: string; covered: number; total: number }) {
  return <div role="progressbar" aria-label={label} aria-valuenow={covered} aria-valuemin={0} aria-valuemax={total}
    className="my-3 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
    <div className="h-full rounded-full bg-indigo-500" style={{ width: `${covered / total * 100}%` }} />
  </div>;
}

function Meter({ label, value, description }: { label: string;
  value: ReturnType<typeof ratio>; description: string }) {
  return <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
    <p className="text-sm font-medium text-gray-600 dark:text-gray-300">{label}</p>
    <p className="mt-2 text-3xl font-semibold tracking-tight text-gray-900 dark:text-white">
      {!value ? 'Unavailable' : value.percent === null ? 'No items' : `${value.percent}%`}
    </p>
    {value && value.total > 0 && <>
      <CoverageBar label={label} covered={value.covered} total={value.total} />
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{value.covered} of {value.total}</p>
    </>}
    <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{description}</p>
  </div>;
}

/** Lazy contextual read. Navigation never repairs the graph or changes a gate. */
export function SpecCoveragePanel({ boardId, specId, revision, canCorrect = [], onOpenSection }: {
  boardId: string; specId: string; revision: string; canCorrect?: CoverageSection[];
  onOpenSection: (section: CoverageSection) => void;
}) {
  const api = useDashboardApi();
  const [view, setView] = useState<CoverageView>('overview');
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
  const planning = dimensions.map(([label, numerator, denominator, section]) => ({
    label, section, value: ratio(summary?.[numerator], summary?.[denominator]),
  }));
  const applicable = planning.filter(row => row.value && row.value.total > 0);
  // Summarize complete dimensions, not a weighted average of unrelated links.
  const planningValue = data?.structure.complete_for_scope && planning.every(row => row.value)
    ? ratio(applicable.filter(row => row.value!.covered === row.value!.total).length, applicable.length) : null;
  const proofAvailable = data?.delivery.state === 'available' && data.delivery.complete_for_scope;
  const implementationValue = proofAvailable
    ? ratio(data.delivery.counts.implementation_proven, data.delivery.counts.obligations) : null;
  const verificationValue = proofAvailable
    ? ratio(data.delivery.counts.verification_proven, data.delivery.counts.obligations) : null;
  const proofUnavailable = data?.delivery.state === 'restricted' ? 'You do not have permission to view delivery proof.'
    : 'Complete delivery proof counts are unavailable. Reload to try again.';
  const gaps = planning.filter(row => row.value && row.value.covered < row.value.total);
  const deliveryItems = data?.items.filter(item => item.kind === 'delivery') ?? [];
  return <section className="space-y-5 text-sm text-gray-700 dark:text-gray-300" aria-label="Spec coverage">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h3 className="text-lg font-semibold text-gray-900 dark:text-white">Coverage</h3>
        <p className="mt-1 text-gray-500 dark:text-gray-400">Find planning gaps and track proof of delivery.</p>
      </div>
      <button type="button" className={button} disabled={loading} onClick={() => void load()}>Reload coverage</button>
    </div>
    <nav aria-label="Coverage views" className="flex flex-wrap gap-1 border-b border-gray-200 pb-2 dark:border-gray-700">
      {views.map(([id, label]) => <button key={id} type="button" aria-current={view === id ? 'page' : undefined}
        onClick={() => setView(id)} className={`rounded-lg px-3 py-2 text-sm font-medium ${view === id
          ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300'
          : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'}`}>{label}</button>)}
    </nav>
    {loading && <p role="status">Loading coverage…</p>}
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p className="text-xs text-gray-500 dark:text-gray-400">Edition {data.edition} · Informational view — this does not approve a gate.</p>
      {view === 'overview' && <>
        <div className="grid gap-3 sm:grid-cols-3">
          <Meter label="Planning coverage" value={planningValue} description="Applicable link dimensions fully covered. Empty dimensions are excluded." />
          <Meter label="Implementation proof" value={implementationValue} description="Obligations with admitted implementation proof." />
          <Meter label="Verification proof" value={verificationValue} description="Obligations with admitted verification proof." />
        </div>
        <section aria-label="Next steps" className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
          <h4 className="font-semibold text-gray-900 dark:text-white">Next steps</h4>
          <ul className="mt-3 space-y-3">
            {gaps.length > 0 && <li className="flex flex-wrap items-center justify-between gap-2">
              <span>{gaps.length} planning dimensions have missing links.</span>
              <button className={button} onClick={() => setView('planning')}>Review planning gaps</button></li>}
            {!planningValue && <li>Some planning counts are unavailable. Open Planning to inspect each dimension.</li>}
            {planningValue && gaps.length === 0 && <li>{planningValue.total ? 'All applicable planning dimensions are covered.' : 'No planning items in scope.'}</li>}
            {([['implementation', implementationValue], ['verification', verificationValue]] as const).map(([kind, value]) =>
              <li key={kind} className="flex flex-wrap items-center justify-between gap-2">
                <span>{!value ? `${kind === 'implementation' ? 'Implementation' : 'Verification'} proof unavailable.`
                  : value.total === 0 ? `No ${kind} obligations in scope.`
                  : `${value.total - value.covered} of ${value.total} obligations without ${kind} proof.`}</span>
                <button className={button} onClick={() => setView(kind)}>Review {kind}</button>
              </li>)}
          </ul>
        </section>
        <p className="text-xs text-gray-500 dark:text-gray-400">Planning links are not delivery proof. Waivers do not count as proven work.</p>
      </>}
      {view === 'planning' && <section aria-label="Authoritative structural coverage" className="space-y-3">
        <div><h4 className="font-semibold">Planning coverage</h4>
          <p className="mt-1 text-gray-500 dark:text-gray-400">Connect the Spec to rules, scenarios and Cards. A linked Test Card does not prove a passing run.</p></div>
        <div className="grid gap-3 sm:grid-cols-2">{planning.map(({ label, section, value }) => {
          const gap = value && value.covered < value.total;
          return <article key={label} className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
            <h5 className="font-medium">{label}</h5>
            <p className="my-2 text-sm">{!value ? 'Unavailable' : value.total === 0 ? 'No items in scope'
              : `${value.covered} / ${value.total} linked · ${value.total - value.covered} missing`}</p>
            {value && value.total > 0 && <CoverageBar label={label} covered={value.covered} total={value.total} />}
            <button type="button" className={button} onClick={() => onOpenSection(section)}>
              {gap && canCorrect.includes(section) ? `Correct ${section} links` : `Open ${section}`}</button>
          </article>;
        })}</div>
      </section>}
      {(view === 'implementation' || view === 'verification') && <section aria-label="Admitted delivery proof" className="space-y-3">
        <Meter label={view === 'implementation' ? 'Implementation proof' : 'Verification proof'}
          value={view === 'implementation' ? implementationValue : verificationValue}
          description="Whole authorized scope. Waivers do not count as proven work." />
        {!proofAvailable && <p>{proofUnavailable}</p>}
        {proofAvailable && deliveryItems.length === 0 && <p>{data.delivery.counts.obligations === 0
          ? 'No obligations in scope.' : 'No obligations on this page. Load more to continue.'}</p>}
        {deliveryItems.map(item => <article className="rounded-xl border border-gray-200 p-4 dark:border-gray-700" key={item.obligation_ref}>
          <div className="flex flex-wrap items-center justify-between gap-2"><h5 className="font-medium">{item.title || item.obligation_ref}</h5>
            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${item[view] === 'proven'
              ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
              : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'}`}>{proofLabels[item[view]]}</span></div>
          <details className="mt-3 text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">Proof details</summary>
            <div className="mt-2 space-y-2 break-all">
              <p>Obligation: {item.obligation_ref} · Revision {item.semantic_sha256}</p>
              <p>Records: {item[`${view}_record_refs`].join(', ') || 'None admitted'}</p>
              <p>Waivers: {item[`${view}_waiver_refs`].join(', ') || 'None'}</p>
              <p>Missing Cards: {item.missing_card_refs.join(', ') || 'None reported'}</p>
              <p>Missing criteria: {item.missing_criteria.map(parts => parts.join(' / ')).join(', ') || 'None reported'}</p>
            </div>
          </details>
        </article>)}
        {data.next_cursor && <button type="button" className={button} disabled={loading} onClick={() => void load(data)}>Load more coverage</button>}
      </section>}
      <details className="rounded-lg border border-gray-200 p-3 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
        <summary className="cursor-pointer">Technical diagnostics</summary>
        <section aria-label="Structural graph observations" className="mt-3 space-y-2">
        <h4 className="font-semibold">Structural graph observations</h4>
        <p>Graph freshness: {data.projection_freshness.state}. Observation: {data.projection_freshness.checked_at}.</p>
        <p>Missing graph observations are not proof of missing work.</p>
        <p>Graph access: {data.structure.graph.state}. Source nodes: {data.structure.graph.expected_nodes ?? 'Unknown'}.
          {' '}Observed nodes: {data.structure.graph.observed_nodes ?? 'Unknown'}.
          {' '}Not found: {data.structure.graph.missing_nodes ?? 'Unknown'} nodes, {data.structure.graph.missing_relations ?? 'Unknown'} expected relations.</p>
        <ul className="space-y-2 break-all">{data.items.map((item, index) => item.kind === 'delivery' ? null : <li key={index}>
          {item.kind === 'structure_node' ? `${item.node_type}: ${item.subject_ref}` : `${item.source_ref} → ${item.relation} → ${item.target_ref}`}
          {' '}— {observations[item.observation]}
          {item.kind === 'structure_relation' && <details><summary>Relation provenance</summary>{item.rule_id} · {item.layer} · {item.created_by}</details>}
        </li>)}</ul>
      </section>
      {data.delivery.blockers.length > 0 && <details><summary>Delivery findings</summary><ul>{data.delivery.blockers.map(code => <li key={code}>{code}</li>)}</ul></details>}
      {data.next_cursor && <button type="button" className={button} disabled={loading} onClick={() => void load(data)}>Load more diagnostics</button>}
      </details>
    </>}
  </section>;
}
