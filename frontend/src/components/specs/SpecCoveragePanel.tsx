import { useCallback, useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { AccessibleTabList, AccessibleTabPanel } from '@/components/shared/AccessibleTabs';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { ContextualHelpLink } from '@/components/help/ContextualHelpLink';
import { DeliveryEvidenceDetails } from '@/components/code-traceability/DeliveryEvidencePanel';
import { taskDeliveryReasons } from '@/components/code-traceability/taskDeliveryDiagnosisCopy';
import { isRetriableReadinessError, ReadinessContractError } from '@/components/code-traceability/readinessTransport';
import type { ReadinessSpecLocation } from '@/components/code-traceability/readinessNavigation';
import { isPersistedReadinessProjection, type ReadinessMetric, type PersistedReadinessProjection } from '@/types/delivery-readiness';
import { isDeliverySummary } from '@/types/delivery-summary';
import type { CoverageGraphMode, CoverageSection, SpecCoverageResponse } from './specCoverageTypes';

const dimensions: [string, string, string, CoverageSection][] = [
  ['Acceptance criteria → scenarios', 'ac_covered', 'ac_total', 'tests'],
  ['Functional requirements → rules', 'fr_covered', 'fr_total', 'rules'],
  ['Scenarios → Test Cards', 'scenarios_linked', 'scenarios_total', 'tests'],
  ['Rules → Cards', 'brs_linked', 'brs_total', 'rules'],
  ['API contracts → Cards', 'contracts_linked', 'contracts_total', 'contracts'],
  ['Technical requirements → Cards', 'trs_linked', 'trs_total', 'trs'],
  ['Decision verification plans', 'decisions_planned', 'decisions_total', 'decisions'],
  ['Integration requirements → Cards', 'irs_linked', 'irs_total', 'irs'],
  ['Observability requirements → Cards', 'ors_linked', 'ors_total', 'ors'],
];
const observations = { observed: 'Observed', observed_expected: 'Expected relation observed',
  graph_only: 'Graph observation only; not confirmed by this source comparison',
  not_found_in_projection: 'Not found in the available projection' };
const button = 'inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700';
const groupLabels = { delivered: 'Delivered', in_progress: 'In progress', pending: 'Pending', optional_or_waived: 'Optional / Waived' };
type CoverageView = 'planning' | 'implementation';
type Snapshot<T> = { value?: T; loading: boolean; error?: string; previous?: boolean; denied?: boolean };

/** One read per explicit revision; no timer, automatic retry or focus polling. */
function useSnapshot<T>(api: unknown, revision: string, enabled: boolean,
  read: (signal: AbortSignal) => Promise<T>, label: string) {
  const [stored, setStored] = useState<{ api: unknown; state: Snapshot<T> }>();
  useEffect(() => {
    if (!enabled) { setStored(undefined); return; }
    const controller = new AbortController();
    setStored(old => ({ api, state: { value: old && old.api === api ? old.state.value : undefined,
      loading: true, previous: Boolean(old && old.api === api && old.state.value) } }));
    void read(controller.signal).then(value => {
      if (!controller.signal.aborted) setStored({ api, state: { value, loading: false } });
    }).catch(failure => {
      if (controller.signal.aborted) return;
      setStored(old => {
        const value = isRetriableReadinessError(failure) && old && old.api === api ? old.state.value : undefined;
        return { api, state: { value, loading: false, previous: Boolean(value),
          denied: failure instanceof AuthenticatedFetchError && [401, 403, 404].includes(failure.status),
          error: failure instanceof AuthenticatedFetchError && [401, 403, 404].includes(failure.status)
            ? `${label} is unavailable with your current access.`
            : `${label} could not be confirmed. Use Refresh in the Spec modal to try again.` } };
      });
    });
    return () => controller.abort();
  }, [api, revision, enabled, read, label]);
  return enabled && stored && stored.api === api ? stored.state : { loading: enabled } as Snapshot<T>;
}
function ratio(covered: unknown, total: unknown) {
  return typeof covered === 'number' && typeof total === 'number'
    && Number.isFinite(covered) && Number.isFinite(total) && covered >= 0 && total >= covered
    ? { covered, total, percent: total ? Math.round(covered / total * 100) : null } : null;
}
function persistedRatio(metric?: ReadinessMetric) {
  if (!metric) return null;
  if (metric.status === 'not_applicable') return { covered: 0, total: 0, percent: null };
  return metric.status === 'known' && metric.percent !== null && metric.applicable !== null
    ? { covered: metric.satisfied, total: metric.applicable, percent: metric.percent } : null;
}
function CoverageBar({ label, covered, total }: { label: string; covered: number; total: number }) {
  return <div role="progressbar" aria-label={label} aria-valuenow={covered} aria-valuemin={0} aria-valuemax={total}
    className="my-3 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
    <div className="h-full rounded-full bg-indigo-500" style={{ width: `${covered / total * 100}%` }} />
  </div>;
}
function Meter({ label, value, description, emptyLabel = 'Unavailable', zeroLabel = 'No items' }: {
  label: string; value: ReturnType<typeof ratio>; description: string; emptyLabel?: string; zeroLabel?: string }) {
  return <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
    <p className="text-sm font-medium text-gray-600 dark:text-gray-300">{label}</p>
    <p className="mt-2 text-3xl font-semibold tracking-tight text-gray-900 dark:text-white">
      {!value ? emptyLabel : value.percent === null ? zeroLabel : `${value.percent}%`}</p>
    {value && value.total > 0 && <><CoverageBar label={label} covered={value.covered} total={value.total} />
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{value.covered} of {value.total}</p></>}
    <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{description}</p>
  </div>;
}
function phaseLabel(state: string, verification = false) {
  return ({ satisfied: verification ? 'Verified' : 'Implemented', missing: 'Pending', partial: 'Partial',
    failed: 'Failed', stale: 'Needs recheck', conflict: 'Needs clarification', waived: 'Waived · no proof credit',
    not_applicable: 'Not applicable', unknown: 'Unknown' } as Record<string, string>)[state] ?? 'Unknown';
}
function impact(unit: PersistedReadinessProjection['units'][number]) {
  const codes = [...new Set([...unit.implementation_reason_codes, ...unit.verification_reason_codes])];
  const known = codes.map(code => taskDeliveryReasons[code]).filter(Boolean);
  if (known.length) return { reason: known.map(row => row.reason).join(' '), next: [...new Set(known.map(row => row.next))].join(' ') };
  if (codes.length) return { reason: 'The assessment needs further review.', next: 'Open Implementation details to inspect the recorded findings.' };
  if (unit.implementation_state === 'satisfied' && unit.verification_state === 'satisfied')
    return { reason: 'Implementation and verification evidence are recorded.', next: 'Review task completion separately.' };
  return { reason: 'The recorded phase status is shown for this requirement.', next: 'Open Implementation details for the assessment and next action.' };
}
interface CoverageProps {
  boardId: string; specId: string; revision: string; edition?: number; canCorrect?: CoverageSection[];
  onOpenSection: (section: CoverageSection) => void;
  canReadSummary?: boolean; canReadImplementation?: boolean; skipDeliveryEvidence?: boolean;
  onSkipDeliveryEvidenceChange?: (value: boolean) => void;
  onOpenReadinessSpec?: (location: ReadinessSpecLocation) => void;
  focusExceptionId?: string | null; canManageTaskRequirements?: boolean;
  accessPending?: boolean; initialReadPending?: boolean; requirementTitles?: Record<string, string>;
}
/** Identity/permission changes cannot render a previous scope for even one frame. */
export function SpecCoveragePanel(props: CoverageProps) {
  if (props.accessPending) return <PulseLoader label="Checking Coverage access…" size="sm" />;
  return <CoverageContent key={JSON.stringify([props.boardId, props.specId, props.edition,
    props.canReadSummary ?? true, props.canReadImplementation ?? true])} {...props} />;
}
function CoverageContent({ boardId, specId, revision, edition, canCorrect = [], onOpenSection,
  canReadSummary = true, canReadImplementation = true, skipDeliveryEvidence, onSkipDeliveryEvidenceChange,
  onOpenReadinessSpec, focusExceptionId, canManageTaskRequirements = false, initialReadPending = false, requirementTitles = {} }: CoverageProps) {
  const api = useDashboardApi();
  // Wait for the modal's first coherent snapshot, then keep this view mounted
  // during later refreshes so its selected mode and previous data survive.
  const [started, setStarted] = useState(!initialReadPending);
  useEffect(() => { if (!initialReadPending) setStarted(true); }, [initialReadPending]);
  const [localRevision, setLocalRevision] = useState(0);
  const readRevision = `${revision}:${localRevision}`;
  const [detailsOpen, setDetailsOpen] = useState(Boolean(focusExceptionId));
  const [view, setView] = useState<CoverageView>(focusExceptionId || !canReadSummary ? 'implementation' : 'planning');
  const mode = useRef<CoverageGraphMode>(focusExceptionId ? 'detail' : 'summary');
  const [coverage, setCoverage] = useState<{ api: typeof api; state: Snapshot<SpecCoverageResponse>; mode: CoverageGraphMode }>();
  const request = useRef<AbortController>();
  const sequence = useRef(0);
  const state = coverage?.api === api ? coverage.state : { loading: canReadSummary };
  const data = canReadSummary ? state.value : undefined;
  const load = useCallback(async (graphMode: CoverageGraphMode, previous?: SpecCoverageResponse) => {
    request.current?.abort();
    if (!canReadSummary || !started) return;
    const controller = new AbortController(); request.current = controller;
    const id = ++sequence.current;
    setCoverage(old => ({ api, mode: graphMode, state: { value: old?.api === api ? old.state.value : undefined,
      loading: true, previous: Boolean(old?.api === api && old.state.value) } }));
    try {
      const page = await api.getSpecCoverage(boardId, specId,
        { graph_mode: graphMode, limit: 200, cursor: previous?.next_cursor }, controller.signal);
      if (controller.signal.aborted || sequence.current !== id) return;
      if (page.subject_ref !== `spec:${specId}` || edition !== undefined && page.edition !== edition
        || previous && (page.edition !== previous.edition
          || page.projection_freshness.source_checkpoint !== previous.projection_freshness.source_checkpoint
          || page.projection_freshness.graph_generation !== previous.projection_freshness.graph_generation))
        throw new ReadinessContractError('Coverage observation changed.');
      setCoverage({ api, mode: graphMode, state: { loading: false,
        value: previous ? { ...page, items: [...previous.items, ...page.items] } : page } });
    } catch (failure) {
      if (controller.signal.aborted || sequence.current !== id) return;
      const status = failure instanceof AuthenticatedFetchError ? failure.status : null;
      setCoverage(old => {
        const retained = isRetriableReadinessError(failure) && old?.api === api ? old.state.value : undefined;
        return { api, mode: graphMode, state: { value: retained, loading: false, previous: Boolean(retained),
          error: status === 403 || status === 401 ? 'Planning and graph details are unavailable with your current access.'
            : status === 404 ? 'Spec not found or unavailable.'
              : status === 409 || failure instanceof ReadinessContractError ? 'The source or graph changed. Use Refresh in the Spec modal to start a new observation.'
                : status === 504 ? 'Coverage took too long to load. Use Refresh in the Spec modal to try again.'
                  : 'Coverage could not be loaded. Use Refresh in the Spec modal to try again.' } };
      });
    }
  }, [api, boardId, specId, edition, canReadSummary, started]);
  useEffect(() => { void load(mode.current); return () => request.current?.abort(); }, [load, readRevision]);
  const readProjection = useCallback(async (signal: AbortSignal) => {
    const value = await api.getPersistedReadinessProjection(boardId, specId, { unitLimit: 250 }, signal);
    if (!isPersistedReadinessProjection(value) || value.scope.board_id !== boardId || value.scope.spec_id !== specId
      || value.scope.card_id !== null || edition !== undefined && value.scope.spec_edition !== edition)
      throw new ReadinessContractError('Delivery assessment scope changed.');
    return value;
  }, [api, boardId, specId, edition]);
  const readiness = useSnapshot(api, readRevision, canReadImplementation && started, readProjection, 'Delivery assessment');
  const readStatus = useCallback(async (signal: AbortSignal) => {
    const value = await api.getDeliverySummary(boardId, specId, { limit: 1 }, signal);
    if (!isDeliverySummary(value) || value.scope.board_id !== boardId || value.scope.spec_id !== specId
      || edition !== undefined && value.scope.spec_edition !== edition)
      throw new ReadinessContractError('Delivery status scope changed.');
    return value;
  }, [api, boardId, specId, edition]);
  const delivery = useSnapshot(api, readRevision, canReadSummary && canReadImplementation && started, readStatus, 'Delivery status');
  const openDetails = useCallback((nextView?: CoverageView) => {
    if (nextView) setView(nextView);
    if (!detailsOpen) { setDetailsOpen(true); mode.current = 'detail'; void load('detail'); }
  }, [detailsOpen, load]);
  const handledFocus = useRef(focusExceptionId);
  useEffect(() => {
    if (focusExceptionId && focusExceptionId !== handledFocus.current && canReadImplementation) openDetails('implementation');
    handledFocus.current = focusExceptionId;
  }, [focusExceptionId, canReadImplementation, openDetails]);
  const [extra, setExtra] = useState<{ api: typeof api; revision: string; base: PersistedReadinessProjection; value: PersistedReadinessProjection }>();
  const [pageFailure, setPageFailure] = useState<{ revision: string; base: PersistedReadinessProjection; message: string; denied: boolean }>();
  const [paging, setPaging] = useState(false);
  const pageRequest = useRef<AbortController>();
  useEffect(() => { pageRequest.current?.abort(); setPaging(false); return () => pageRequest.current?.abort(); }, [api, readRevision, readiness.value]);
  const pageError = pageFailure?.revision === readRevision && pageFailure.base === readiness.value ? pageFailure : undefined;
  const denied = readiness.denied || delivery.denied || pageError?.denied;
  const projection = denied ? undefined : extra?.api === api && extra.revision === readRevision && extra.base === readiness.value ? extra.value : readiness.value;
  const deliveryStatus = denied ? undefined : delivery.value;
  const proof = projection?.summaries.find(row => row.card_id === null);
  const freshness = pageError ? 'unavailable' : readiness.previous ? (readiness.loading ? 'updating' : 'unavailable') : projection?.projection.freshness;
  async function moreRequirements() {
    const base = readiness.value;
    if (!base || !projection?.page.next_cursor || paging || pageError || readiness.loading) return;
    const controller = new AbortController(); pageRequest.current?.abort(); pageRequest.current = controller;
    setPaging(true);
    try {
      const next = await api.getPersistedReadinessProjection(boardId, specId,
        { unitLimit: 250, cursor: projection.page.next_cursor }, controller.signal);
      if (controller.signal.aborted) return;
      if (!isPersistedReadinessProjection(next) || JSON.stringify(next.scope) !== JSON.stringify(projection.scope)
        || next.projection.generation_id !== projection.projection.generation_id
        || next.projection.source_revision !== projection.projection.source_revision
        || next.projection.target_revision !== projection.projection.target_revision
        || next.page.offset <= projection.page.offset || next.page.next_cursor && next.page.next_cursor === projection.page.next_cursor)
        throw new ReadinessContractError('Assessment page changed.');
      const identity = (row: PersistedReadinessProjection['units'][number]) => JSON.stringify([row.card_id, row.kind, row.ref, row.semantic_sha256]);
      const existing = new Set(projection.units.map(identity));
      if (next.units.some(row => existing.has(identity(row)))) throw new ReadinessContractError('Repeated assessment row.');
      setExtra({ api, revision: readRevision, base, value: { ...next, units: [...projection.units, ...next.units] } });
    } catch (failure) {
      if (!controller.signal.aborted) setPageFailure({ revision: readRevision, base,
        denied: failure instanceof AuthenticatedFetchError && [401, 403, 404].includes(failure.status),
        message: 'Requirement details changed or could not be read. Use Refresh in the Spec modal.' });
    } finally { if (!controller.signal.aborted) setPaging(false); }
  }
  const planning = dimensions.map(([label, numerator, denominator, section]) => ({ label, section,
    value: ratio(data?.structure.summary?.[numerator], data?.structure.summary?.[denominator]) }));
  const applicable = planning.filter(row => row.value && row.value.total > 0);
  const planningValue = data?.structure.complete_for_scope && planning.every(row => row.value)
    ? ratio(applicable.filter(row => row.value!.covered === row.value!.total).length, applicable.length) : null;
  const proofEmpty = readiness.loading || freshness === 'pending_initial' ? 'Waiting for calculation' : 'Unavailable';
  const rows = (projection?.units.filter(row => row.card_id === null) ?? []).sort((left, right) =>
    Number(left.implementation_state === 'satisfied' && left.verification_state === 'satisfied')
    - Number(right.implementation_state === 'satisfied' && right.verification_state === 'satisfied'));
  const tabId = `spec-coverage-${specId}`;
  const availableViews = ([['planning', 'Planning'], ['implementation', 'Implementation']] as const)
    .filter(([id]) => id === 'planning' ? canReadSummary : canReadImplementation);
  const activeView = availableViews.some(([id]) => id === view) ? view : availableViews[0]?.[0] ?? 'planning';
  useEffect(() => {
    if (data || proof) performance.mark?.('pulse:coverage:useful', { detail: { specId, edition, revision: readRevision } });
  }, [data, proof, specId, edition, readRevision]);
  if (!started) return <PulseLoader label="Loading the current Spec snapshot…" size="sm" />;
  return <section className="space-y-5 text-sm text-gray-700 dark:text-gray-300" aria-label="Spec coverage">
    <header><h3 className="text-lg font-semibold text-gray-900 dark:text-white">Coverage</h3>
      <p className="mt-1 text-gray-500 dark:text-gray-400">What is planned, implemented and verified.</p>
      <ContextualHelpLink sectionId="spec-coverage" className="mt-2 text-xs">How Coverage works</ContextualHelpLink></header>
    {canReadSummary && canReadImplementation && <section aria-label="Delivery status" className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
      <h4 className="font-semibold">Delivery status</h4>
      {delivery.loading && !deliveryStatus && <PulseLoader label="Loading delivery status…" size="sm" />}
      {delivery.previous && <p role="status">Previous delivery status · {delivery.loading ? 'Updating' : 'Current status unavailable'}</p>}
      {delivery.error && <p role="alert">{delivery.error}</p>}
      {deliveryStatus && <><div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">{deliveryStatus.groups.map(group =>
        <p key={group.id}>{groupLabels[group.id]}: <strong>{group.count ?? 'Unknown'}</strong></p>)}</div>
        {!deliveryStatus.complete && <p role="status" className="mt-2 text-xs">Delivery status {deliveryStatus.projection.freshness === 'updating' ? 'is updating' : 'is not yet confirmed'}.</p>}</>}
    </section>}
    <div className="grid gap-3 sm:grid-cols-3">
      {canReadSummary && <Meter label="Planning coverage" value={planningValue}
        emptyLabel={state.loading ? 'Loading' : 'Unavailable'} description="Requirement, scenario and task links. This is not delivery proof." />}
      {canReadImplementation && <><Meter label="Implementation proof" value={persistedRatio(proof?.implementation)}
        emptyLabel={proofEmpty} zeroLabel="Not applicable" description="Recorded implementation for applicable requirements." />
      <Meter label="Verification proof" value={persistedRatio(proof?.verification)} emptyLabel={proofEmpty}
        zeroLabel="Not applicable" description="Recorded verification for the delivered version." /></>}
    </div>
    {canReadSummary && <div aria-label="Planning freshness" className="text-xs">
      {state.loading && !data && <PulseLoader label="Loading coverage…" size="sm" />}
      {data && <p role="status">{state.previous ? 'Previous planning data' : 'Planning data is current'}{state.loading ? ' · Updating' : ''}</p>}
      {state.error && <p role="alert" className="text-amber-700 dark:text-amber-300">{state.error}</p>}
      {data?.structure.graph.state === 'unavailable' && <p role="status" className="mt-2 text-amber-700 dark:text-amber-300">
        Graph details are temporarily unavailable. Planning and delivery information remain independent.</p>}
    </div>}
    {canReadImplementation && <div aria-label="Delivery freshness" className="text-xs">
      <p role="status">{freshness === 'current' ? 'Delivery data is current' : freshness === 'updating'
        ? `${proof ? 'Previous delivery data · ' : ''}Delivery data is updating`
        : freshness === 'pending_initial' || readiness.loading ? 'Delivery data is waiting for calculation'
          : `${proof ? 'Previous delivery data · ' : ''}Delivery calculation is unavailable`}</p>
      {readiness.error && <p role="alert">{readiness.error}</p>}
      {pageError && <p role="alert">{pageError.message}</p>}
    </div>}
    {canReadImplementation && rows.length > 0 && <section aria-label="Requirement delivery matrix">
      <h4 className="mb-2 font-semibold">Implementation and verification by requirement</h4>
      {freshness !== 'current' && <p className="mb-2 text-xs">Previous assessment · these rows do not confirm current delivery.</p>}
      <table className="w-full table-fixed text-left text-xs max-sm:block">
        <thead className="max-sm:sr-only"><tr>{['Requirement', 'Implementation', 'Verification', 'Impact', 'Next action'].map(label =>
          <th key={label} className="border-b p-2 align-top">{label}</th>)}</tr></thead>
        <tbody className="max-sm:block">{rows.map(row => { const diagnosis = impact(row); const title = requirementTitles[row.ref.slice(row.ref.indexOf(':') + 1)];
          return <tr key={`${row.ref}:${row.semantic_sha256}`} className="border-b max-sm:mb-3 max-sm:block max-sm:rounded-lg max-sm:border max-sm:p-2">
            {[title && title.length <= 140 ? <span className="block font-medium">{title}</span> : <details>
              <summary className="cursor-pointer font-medium">{title ? `${title.slice(0, 140).trimEnd()}…` : row.ref.startsWith('card:') ? 'Task details' : 'Requirement details'}</summary>
              {title && <p className="mt-2">{title}</p>}<p className="mt-2 break-all text-gray-500">Reference: {row.ref}</p>
            </details>,
              phaseLabel(row.implementation_state), phaseLabel(row.verification_state, true), diagnosis.reason, diagnosis.next].map((value, index) =>
              <td key={index} className="break-words p-2 align-top max-sm:block"><span className="mb-1 block font-semibold sm:hidden">{['Requirement', 'Implementation', 'Verification', 'Impact', 'Next action'][index]}</span>{value}</td>)}
          </tr>;
        })}</tbody>
      </table>
      {projection?.page.has_more && <button className={`${button} mt-3`} disabled={paging || readiness.loading || Boolean(pageError)}
        onClick={() => void moreRequirements()}>{paging ? 'Loading requirements…' : 'Load more requirements'}</button>}
    </section>}
    {projection && !rows.length && <p className="text-xs">{projection.page.total === 0 ? 'No assessed requirements in this scope.' : 'Requirement details are available in Implementation.'}</p>}
    <p className="text-xs text-gray-500">{edition !== undefined ? `Edition ${edition} · ` : data ? `Edition ${data.edition} · ` : ''}Informational view — this does not approve a gate. Waivers do not count as proven work.</p>
    <button type="button" className={button} aria-expanded={detailsOpen} aria-controls={`${tabId}-details`} onClick={() => {
      if (detailsOpen) { setDetailsOpen(false); mode.current = 'summary'; request.current?.abort();
        setCoverage(old => old && ({ ...old, state: { ...old.state, loading: false } })); }
      else openDetails();
    }}>{detailsOpen ? 'Hide details' : 'Show details'}</button>
    {detailsOpen && <div id={`${tabId}-details`} className="space-y-4">
      <AccessibleTabList idBase={tabId} ariaLabel="Coverage details" variant="secondary"
        items={availableViews.map(([id, label]) => ({ id, label }))} value={activeView} onValueChange={setView} />
      {canReadImplementation && activeView === 'implementation' && <AccessibleTabPanel idBase={tabId} tabId="implementation" value={activeView}>
        <DeliveryEvidenceDetails boardId={boardId} specId={specId} revision={readRevision} canManageTaskRequirements={canManageTaskRequirements}
          externalRead={{ projection: projection && freshness ? { ...projection, projection: { ...projection.projection, freshness } } : undefined,
            loading: readiness.loading, error: readiness.error || pageError?.message, onChanged: () => setLocalRevision(value => value + 1) }}
          focusExceptionId={focusExceptionId} onOpenReadinessSpec={onOpenReadinessSpec} onOpenDecisions={() => onOpenSection('decisions')}
          skipDeliveryEvidence={skipDeliveryEvidence} onSkipDeliveryEvidenceChange={onSkipDeliveryEvidenceChange} />
      </AccessibleTabPanel>}
      {canReadSummary && activeView === 'planning' && <AccessibleTabPanel idBase={tabId} tabId="planning" value={activeView}>
        <section aria-label="Authoritative structural coverage" className="grid gap-3 sm:grid-cols-2">{planning.map(({ label, section, value }) =>
          <article key={label} className="rounded-xl border border-gray-200 p-4 dark:border-gray-700"><h5 className="font-medium">{label}</h5>
            <p className="my-2">{!value ? 'Unavailable' : value.total === 0 ? 'No items in scope' : `${value.covered} / ${value.total} linked · ${value.total - value.covered} missing`}</p>
            {value && value.total > 0 && <CoverageBar label={label} covered={value.covered} total={value.total} />}
            <button className={button} onClick={() => onOpenSection(section)}>{value && value.covered < value.total && canCorrect.includes(section) ? `Correct ${section} links` : `Open ${section}`}</button>
          </article>)}</section>
      </AccessibleTabPanel>}
      {canReadSummary && <details className="rounded-lg border border-gray-200 p-3 text-xs dark:border-gray-700">
        <summary className="cursor-pointer">Technical diagnostics</summary>
        {projection?.projection.generation_id && <p className="mt-3 break-all">Delivery generation: {projection.projection.generation_id}</p>}
        {state.loading && <PulseLoader label="Loading graph details…" size="sm" />}
        {data && coverage?.mode === 'detail' && <section aria-label="Structural graph observations" className="mt-3 space-y-2">
          <p>Graph access: {data.structure.graph.state}. Observation: {data.projection_freshness.checked_at}.</p>
          <p>Missing graph observations are not proof of missing work.</p>
          <p>Source nodes: {data.structure.graph.expected_nodes ?? 'Unknown'}. Observed nodes: {data.structure.graph.observed_nodes ?? 'Unknown'}.</p>
          <ul className="space-y-2 break-all">{data.items.map((item, index) => item.kind === 'delivery' ? null : <li key={index}>
            {item.kind === 'structure_node' ? `${item.node_type}: ${item.subject_ref}` : `${item.source_ref} → ${item.relation} → ${item.target_ref}`} — {observations[item.observation]}
            {item.kind === 'structure_relation' && <details><summary>Relation provenance</summary>{item.rule_id} · {item.layer} · {item.created_by}</details>}
          </li>)}</ul>
          {data.next_cursor && <button className={button} disabled={state.loading || Boolean(state.error)} onClick={() => void load('detail', data)}>Load more diagnostics</button>}
        </section>}
      </details>}
    </div>}
  </section>;
}
