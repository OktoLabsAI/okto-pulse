import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { AccessibleTabList, AccessibleTabPanel } from '@/components/shared/AccessibleTabs';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { DeliveryEvidencePanel } from '@/components/code-traceability/DeliveryEvidencePanel';
import type { CoverageSection, SpecCoverageResponse } from './specCoverageTypes';

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
const observations = { observed: 'Observed', observed_expected: 'Expected relation observed',
  graph_only: 'Graph observation only; not confirmed by this source comparison',
  not_found_in_projection: 'Not found in the available projection' };
const button = 'inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700';
type CoverageView = 'overview' | 'planning' | 'implementation';
const views: [CoverageView, string][] = [['overview', 'Overview'], ['planning', 'Planning'],
  ['implementation', 'Implementation']];

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
export function SpecCoveragePanel({ boardId, specId, revision, canCorrect = [], onOpenSection, canReadSummary = true, canReadImplementation = true, skipDeliveryEvidence, onSkipDeliveryEvidenceChange }: {
  boardId: string; specId: string; revision: string; canCorrect?: CoverageSection[];
  onOpenSection: (section: CoverageSection) => void;
  canReadSummary?: boolean; canReadImplementation?: boolean; skipDeliveryEvidence?: boolean;
  onSkipDeliveryEvidenceChange?: (value: boolean) => void;
}) {
  const api = useDashboardApi();
  const [view, setView] = useState<CoverageView>(canReadSummary ? 'overview' : 'implementation');
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
    if (canReadSummary) void load();
    else setData(null);
    return () => request.current?.abort();
  }, [load, revision, canReadSummary]);
  const availableViews = views.filter(([id]) => id === 'implementation' ? canReadImplementation : canReadSummary);
  const activeView = availableViews.some(([id]) => id === view) ? view : availableViews[0]?.[0] ?? 'overview';
  const tabId = `spec-coverage-${specId}`;
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
  const gaps = planning.filter(row => row.value && row.value.covered < row.value.total);
  return <section className="space-y-5 text-sm text-gray-700 dark:text-gray-300" aria-label="Spec coverage">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h3 className="text-lg font-semibold text-gray-900 dark:text-white">Coverage</h3>
        <p className="mt-1 text-gray-500 dark:text-gray-400">Find planning gaps and track proof of delivery.</p>
      </div>
      {activeView !== 'implementation' && <button type="button" className={button} disabled={loading} onClick={() => void load()}>Reload coverage</button>}
    </div>
    <AccessibleTabList idBase={tabId} ariaLabel="Coverage views" variant="secondary"
      items={availableViews.map(([id, label]) => ({ id, label }))} value={activeView} onValueChange={setView} />
    {canReadImplementation && <AccessibleTabPanel idBase={tabId} tabId="implementation" value={activeView}>
      <DeliveryEvidencePanel boardId={boardId} specId={specId} revision={revision}
        skipDeliveryEvidence={skipDeliveryEvidence} onSkipDeliveryEvidenceChange={onSkipDeliveryEvidenceChange} />
    </AccessibleTabPanel>}
    {activeView !== 'implementation' && loading && <PulseLoader label="Loading coverage…" size="sm" className="py-6" />}
    {activeView !== 'implementation' && error && <p role="alert">{error}</p>}
    {canReadSummary && data && activeView !== 'implementation' && <>
      <p className="text-xs text-gray-500 dark:text-gray-400">Edition {data.edition} · Informational view — this does not approve a gate.</p>
      <AccessibleTabPanel idBase={tabId} tabId="overview" value={activeView} className="space-y-4">
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
                <button className={button} disabled={!canReadImplementation} onClick={() => setView('implementation')}>Review {kind}</button>
              </li>)}
          </ul>
        </section>
        <p className="text-xs text-gray-500 dark:text-gray-400">Planning links are not delivery proof. Waivers do not count as proven work.</p>
      </AccessibleTabPanel>
      <AccessibleTabPanel idBase={tabId} tabId="planning" value={activeView}><section aria-label="Authoritative structural coverage" className="space-y-3">
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
      </section></AccessibleTabPanel>
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
