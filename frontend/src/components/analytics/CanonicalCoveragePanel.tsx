import { useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleSlash2,
  Clock3,
  Download,
  FileCode2,
  Link2,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react';
import type {
  CanonicalAnalyticsRecord,
  CanonicalCoverageResponse,
  CanonicalCoverageRow,
} from './analyticsCanonicalTypes';
import {
  canonicalCoverageQueryState,
  type CanonicalCoverageOutcome,
  type CanonicalCoverageQueryState,
} from './canonicalCoverageQueryState';

export interface CanonicalCoveragePanelProps {
  data: CanonicalCoverageResponse | null;
  loading: boolean;
  error: string | null;
  exporting: boolean;
  from: string;
  to: string;
  specTitles: Record<string, string>;
  onRetry: () => void;
  onExport: () => Promise<void>;
  onOpenSpec: (specId: string, title: string) => void;
  onOpenFullView?: (query: CanonicalCoverageQueryState) => void;
  onQueryStateChange?: (query: CanonicalCoverageQueryState) => void;
  queryState?: CanonicalCoverageQueryState;
  viewMode?: 'summary' | 'full';
}

type OutcomeFilter = CanonicalCoverageOutcome;

function words(value: string | null | undefined): string {
  if (!value) return 'Unknown';
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const OBLIGATION_LABELS: Record<string, string> = {
  ac: 'Acceptance Criteria',
  fr: 'Functional Requirement',
  test_scenario: 'Test Scenario',
  business_rule: 'Business Rule',
  api_contract: 'API Contract',
  technical_requirement: 'Technical Requirement',
  decision: 'Decision',
  integration_requirement: 'Integration Requirement',
  observability_requirement: 'Observability Requirement',
};

function obligationLabel(value: string): string {
  return OBLIGATION_LABELS[value] ?? words(value);
}

function countValue(value: number | null | undefined): number | string {
  return value === null || value === undefined ? '—' : value;
}

function coverageRate(
  state: string,
  applicable: number | null,
  value: number | null,
): string {
  if (state === 'not_applicable' || applicable === 0) return 'N/A';
  if (value === null) return words(state);
  return `${Math.round(value)}%`;
}

function shortRef(value: string | null | undefined): string {
  if (!value) return '—';
  return value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value;
}

function ageLabel(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'Age unavailable';
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s old`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m old`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h old`;
  return `${Math.floor(seconds / 86400)}d old`;
}

function badgeTone(value: string | null | undefined): string {
  const normalized = (value ?? '').toLowerCase();
  if (['current', 'active', 'covered', 'complete', 'completed', 'passed', 'available', 'eligible'].includes(normalized)) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/35 dark:text-emerald-300';
  }
  if (['previous', 'stale', 'skipped', 'waived', 'partial', 'incomplete'].includes(normalized)) {
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/35 dark:text-amber-300';
  }
  if (['missing', 'revoked', 'failed', 'blocked', 'uncovered', 'ineligible', 'inconsistent'].includes(normalized)) {
    return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/35 dark:text-red-300';
  }
  return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
}

function StatusBadge({ value, title }: { value: string | null | undefined; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold ${badgeTone(value)}`}
    >
      {words(value)}
    </span>
  );
}

function rowOutcome(row: CanonicalCoverageRow): Exclude<OutcomeFilter, 'all'> {
  if (row.skip?.effective) return 'skipped';
  if (row.covered === true) return 'covered';
  if (row.covered === false) return 'uncovered';
  return 'incomplete';
}

function rowLifecycle(row: CanonicalCoverageRow): string {
  const evidence = row.evidence ?? [];
  return (
    row.lifecycle
    ?? evidence.find((item) => item.lifecycle_status)?.lifecycle_status
    ?? evidence.find((item) => item.delivery_state)?.delivery_state
    ?? row.identity.currentness
    ?? row.currentness
    ?? row.state
    ?? 'unknown'
  );
}

function recordId(record: CanonicalAnalyticsRecord, fallback: string): string {
  return String(
    record.waiver_id
    ?? record.overlap_id
    ?? record.execution_id
    ?? record.resolution_id
    ?? record.target_id
    ?? record.id
    ?? fallback,
  );
}

function recordReceipt(record: CanonicalAnalyticsRecord): string | null {
  const direct = record.receipt_id ?? record.investigation_receipt_id;
  if (typeof direct === 'string' && direct) return direct;
  return typeof record.authority_ref === 'string' && record.authority_ref.startsWith('receipt:')
    ? record.authority_ref
    : null;
}

function recordAge(record: CanonicalAnalyticsRecord): number | null {
  return typeof record.age_seconds === 'number' ? record.age_seconds : null;
}

function recordBadge(record: CanonicalAnalyticsRecord): string {
  return String(record.currentness ?? record.state ?? record.lifecycle_status ?? record.outcome ?? 'recorded');
}

interface SpecMatrixRow {
  specId: string;
  edition: number;
  rows: CanonicalCoverageRow[];
}

export function CanonicalCoveragePanel({
  data,
  loading,
  error,
  exporting,
  from,
  to,
  specTitles,
  onRetry,
  onExport,
  onOpenSpec,
  onOpenFullView,
  onQueryStateChange,
  queryState,
  viewMode = 'full',
}: CanonicalCoveragePanelProps) {
  const [localQuery, setLocalQuery] = useState<CanonicalCoverageQueryState>(() => (
    canonicalCoverageQueryState({ from, to })
  ));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const activeQuery = queryState
    ? canonicalCoverageQueryState(queryState)
    : canonicalCoverageQueryState({ ...localQuery, from, to });
  const { lifecycle, outcome, search } = activeQuery;

  const updateQuery = (patch: Partial<CanonicalCoverageQueryState>) => {
    const next = canonicalCoverageQueryState({ ...activeQuery, ...patch });
    if (!queryState) setLocalQuery(next);
    onQueryStateChange?.(next);
  };

  const allRows = useMemo(
    () => (data?.coverage ?? []).flatMap((group) =>
      (group.rows ?? []).map((row) => ({
        ...row,
        identity: {
          ...row.identity,
          obligation_type: row.identity.obligation_type ?? group.obligation_type,
        },
      })),
    ),
    [data],
  );

  const lifecycleOptions = useMemo(
    () => Array.from(new Set([
      ...allRows.map(rowLifecycle).filter(Boolean),
      ...(lifecycle === 'all' ? [] : [lifecycle]),
    ])).sort(),
    [allRows, lifecycle],
  );

  const filteredRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return allRows.filter((row) => {
      if (lifecycle !== 'all' && rowLifecycle(row) !== lifecycle) return false;
      if (outcome !== 'all' && rowOutcome(row) !== outcome) return false;
      if (!needle) return true;
      const evidenceText = (row.evidence ?? []).flatMap((item) => [
        item.evidence_id,
        item.source_ref,
        item.parent_card_id,
        item.lifecycle_status,
        item.delivery_state,
        item.currentness,
      ]).filter(Boolean).join(' ');
      const haystack = [
        row.identity.spec_id,
        specTitles[row.identity.spec_id],
        row.identity.obligation_id,
        row.identity.obligation_type,
        row.state,
        row.reason,
        row.skip?.reason_code,
        evidenceText,
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }, [allRows, lifecycle, outcome, search, specTitles]);

  const matrix = useMemo(() => {
    const bySpec = new Map<string, SpecMatrixRow>();
    filteredRows.forEach((row) => {
      const key = `${row.identity.spec_id}:${row.identity.edition}`;
      const existing = bySpec.get(key);
      if (existing) existing.rows.push(row);
      else bySpec.set(key, { specId: row.identity.spec_id, edition: row.identity.edition, rows: [row] });
    });
    return [...bySpec.values()].sort((a, b) => {
      const left = specTitles[a.specId] ?? a.specId;
      const right = specTitles[b.specId] ?? b.specId;
      return left.localeCompare(right);
    });
  }, [filteredRows, specTitles]);

  const codeEvidence = data?.code_evidence;
  const targets = codeEvidence?.targets ?? [];
  const resolutions = codeEvidence?.resolutions ?? [];
  const executions = codeEvidence?.executions ?? [];
  const overlaps = codeEvidence?.overlaps ?? [];
  const waivers = codeEvidence?.waivers ?? [];
  const applicable = data?.totals.applicable ?? 0;
  const coverageValue = applicable === 0 || data?.totals.state === 'not_applicable'
    ? 'N/A'
    : data?.totals.value === null || data?.totals.value === undefined
      ? words(data?.totals.state)
      : `${Math.round(data.totals.value)}%`;
  const incomplete = allRows.filter((row) => rowOutcome(row) === 'incomplete').length;
  const readiness = codeEvidence?.state ?? (incomplete > 0 ? 'incomplete' : data?.totals.state ?? 'unavailable');
  const effectiveSkips = allRows.filter((row) => row.skip?.effective);
  const boardSkips = effectiveSkips.filter((row) => row.skip.authority_ref?.startsWith('board:')).length;
  const specSkips = effectiveSkips.filter((row) => row.skip.authority_ref?.startsWith('spec:')).length;
  const unclassifiedSkips = effectiveSkips.length - boardSkips - specSkips;
  const effectiveSkipCount = data?.totals.skipped;
  const skipRate = data?.totals.state === 'not_applicable' || applicable === 0
    ? 'N/A'
    : data?.totals.state !== 'available'
      ? words(data?.totals.state)
      : effectiveSkipCount === null || effectiveSkipCount === undefined
        ? 'Unavailable'
        : `${Math.round((effectiveSkipCount / applicable) * 100)}%`;
  const authoritySkipSummary = data?.totals.state === 'not_applicable' || applicable === 0
    ? 'No applicable obligations'
    : data?.totals.state !== 'available'
      ? 'Authority split is not available'
      : `Board ${Math.round((boardSkips / applicable) * 100)}% · Spec ${Math.round((specSkips / applicable) * 100)}%${unclassifiedSkips > 0 ? ` · ${unclassifiedSkips} unclassified` : ''}`;
  const headlineCards = [
    { label: 'Native coverage', value: coverageValue, icon: CheckCircle2, note: applicable === 0 ? 'No applicable obligations' : `${applicable} applicable` },
    { label: 'Readiness', value: words(readiness), icon: ShieldCheck, note: codeEvidence?.reason ? words(codeEvidence.reason) : 'Final disposition authority' },
    { label: 'Effective skip rate', value: skipRate, icon: CircleSlash2, note: authoritySkipSummary },
    { label: 'Incomplete projections', value: incomplete, icon: AlertTriangle, note: 'Covered interpretation is unavailable' },
  ];
  const detailCards = [
    { label: 'Covered', value: data?.totals.covered ?? '—', icon: Link2, note: 'Canonical evidence only' },
    { label: 'Final / deferred', value: 'Unavailable', icon: ShieldCheck, note: 'Not supplied by canonical authority' },
    { label: 'Skipped', value: data?.totals.skipped ?? '—', icon: CircleSlash2, note: 'Governed, never covered' },
    { label: 'Board skips', value: boardSkips, icon: CircleSlash2, note: 'Effective, never covered' },
    { label: 'Spec skips', value: specSkips, icon: CircleSlash2, note: unclassifiedSkips > 0 ? `${unclassifiedSkips} unclassified` : 'Effective, never covered' },
    { label: 'Current targets', value: targets.filter((item) => item.currentness === 'current').length, icon: Clock3, note: `${targets.length} total targets` },
  ];

  return (
    <section
      id="analytics-canonical-coverage"
      aria-labelledby="canonical-coverage-heading"
      className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
      data-testid="canonical-coverage-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <FileCode2 className="h-4 w-4 text-cyan-500" aria-hidden="true" />
            <h3 id="canonical-coverage-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">
              Canonical Coverage &amp; Traceability
            </h3>
          </div>
          <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
            Governed obligations and Code Evidence use the same canonical authority. Skips, incomplete authority and historical evidence never count as covered.
          </p>
          <p className="mt-1 text-[10px] text-gray-400">Period {activeQuery.from} through {activeQuery.to}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {onOpenFullView && (
            <button
              type="button"
              onClick={() => onOpenFullView(activeQuery)}
              className="inline-flex items-center gap-1.5 rounded-md bg-cyan-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-cyan-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-500"
            >
              Open full view <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
          <button type="button" disabled={loading} onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Refresh</button>
          <button
            type="button"
            disabled={exporting || loading || data === null}
            onClick={() => void onExport()}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            {exporting ? 'Exporting…' : 'Complete CSV'}
          </button>
        </div>
      </div>

      {loading && <p className="mt-4 text-xs text-gray-500" role="status">Loading canonical coverage…</p>}
      {!loading && error && (
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 dark:bg-red-900/20" role="alert">
          <span className="text-xs text-red-700 dark:text-red-300">{error}</span>
          <button type="button" onClick={onRetry} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 dark:text-red-300">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Retry
          </button>
        </div>
      )}

      {!loading && !error && data && (
        <div className="mt-5 space-y-5">
          <div className={`grid grid-cols-2 gap-3 md:grid-cols-4 ${viewMode === 'full' ? 'xl:grid-cols-5' : ''}`} aria-label="Canonical coverage KPIs">
            {(viewMode === 'summary' ? headlineCards : [...headlineCards, ...detailCards]).map(({ label, value, icon: Icon, note }) => (
              <div key={label} className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
                <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  <Icon className="h-3.5 w-3.5" aria-hidden="true" /> {label}
                </div>
                <p className="mt-1 text-xl font-bold text-gray-900 dark:text-white">{value}</p>
                <p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">{note}</p>
              </div>
            ))}
          </div>

          {viewMode === 'full' && <>
          <div className="grid gap-3 rounded-lg border border-gray-200 bg-gray-50 p-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_190px_180px_180px] dark:border-gray-700 dark:bg-gray-900/30" aria-label="Coverage filters">
            <label className="relative">
              <span className="sr-only">Search coverage</span>
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" aria-hidden="true" />
              <input
                value={search}
                onChange={(event) => updateQuery({ search: event.target.value })}
                placeholder="Search Spec, obligation, evidence or source…"
                className="min-h-9 w-full rounded-md border border-gray-300 bg-white py-2 pl-9 pr-3 text-xs text-gray-800 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
              />
            </label>
            <fieldset className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              <legend>Period</legend>
              <div className="mt-1 grid min-h-9 grid-cols-2 gap-1">
                <label className="sr-only" htmlFor="canonical-coverage-from">From date</label>
                <input
                  id="canonical-coverage-from"
                  type="date"
                  value={activeQuery.from}
                  onChange={(event) => updateQuery({ from: event.target.value })}
                  className="min-w-0 rounded-md border border-gray-300 bg-white px-1.5 text-[10px] font-normal normal-case dark:border-gray-600 dark:bg-gray-800"
                />
                <label className="sr-only" htmlFor="canonical-coverage-to">To date</label>
                <input
                  id="canonical-coverage-to"
                  type="date"
                  value={activeQuery.to}
                  onChange={(event) => updateQuery({ to: event.target.value })}
                  className="min-w-0 rounded-md border border-gray-300 bg-white px-1.5 text-[10px] font-normal normal-case dark:border-gray-600 dark:bg-gray-800"
                />
              </div>
            </fieldset>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              Lifecycle
              <select value={lifecycle} onChange={(event) => updateQuery({ lifecycle: event.target.value })} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800">
                <option value="all">All lifecycle states</option>
                {lifecycleOptions.map((value) => <option key={value} value={value}>{words(value)}</option>)}
              </select>
            </label>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              Outcome
              <select value={outcome} onChange={(event) => updateQuery({ outcome: event.target.value as OutcomeFilter })} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800">
                <option value="all">All outcomes</option>
                <option value="covered">Covered</option>
                <option value="uncovered">Uncovered</option>
                <option value="skipped">Skipped</option>
                <option value="incomplete">Incomplete</option>
              </select>
            </label>
          </div>

          <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="w-full min-w-[680px] text-left text-xs" aria-label="Canonical coverage by obligation type">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-400 dark:border-gray-700 dark:bg-gray-900/30">
                  <th className="px-3 py-2">Obligation</th>
                  <th className="px-3 py-2">Authority state</th>
                  <th className="px-3 py-2 text-right">Applicable</th>
                  <th className="px-3 py-2 text-right">Covered</th>
                  <th className="px-3 py-2 text-right">Uncovered</th>
                  <th className="px-3 py-2 text-right">Skipped</th>
                  <th className="px-3 py-2 text-right">Rate</th>
                </tr>
              </thead>
              <tbody>
                {data.coverage.map((group) => (
                  <tr key={group.obligation_type} className="border-b border-gray-100 last:border-0 dark:border-gray-700/60">
                    <th className="px-3 py-2 font-semibold text-gray-800 dark:text-gray-100">{obligationLabel(group.obligation_type)}</th>
                    <td className="px-3 py-2"><StatusBadge value={group.state} title={group.reason ?? undefined} /></td>
                    <td className="px-3 py-2 text-right tabular-nums">{countValue(group.applicable)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{countValue(group.covered)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{countValue(group.uncovered)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{countValue(group.skipped)}</td>
                    <td className="px-3 py-2 text-right font-semibold">
                      <span>{coverageRate(group.state, group.applicable, group.value)}</span>
                      {(group.state === 'not_applicable' || group.applicable === 0) && (
                        <span className="mt-0.5 block text-[10px] font-normal text-gray-500 dark:text-gray-400">
                          {group.reason ? words(group.reason) : 'No applicable obligations'}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {data.coverage.length === 0 && (
                  <tr><td colSpan={7} className="px-3 py-6 text-center text-xs text-gray-400">No obligation groups were returned for this period.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <div>
                <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Per-Spec Code Evidence Matrix</h4>
                <p className="text-[10px] text-gray-500 dark:text-gray-400">{matrix.length} Specs · {filteredRows.length} obligations after filters</p>
              </div>
              <StatusBadge value={codeEvidence?.state ?? 'unavailable'} title={codeEvidence?.reason ?? undefined} />
            </div>

            {matrix.length === 0 ? (
              <div className="rounded-lg border border-dashed border-gray-300 px-4 py-8 text-center text-xs text-gray-500 dark:border-gray-600">
                {allRows.length === 0 ? 'No canonical obligations are applicable in this period.' : 'No Specs match the current filters.'}
              </div>
            ) : (
              <div className="space-y-2">
                {matrix.map((spec) => {
                  const specKey = `${spec.specId}:${spec.edition}`;
                  const isOpen = expanded.has(specKey);
                  const specTitle = specTitles[spec.specId] ?? spec.specId;
                  const coveredCount = spec.rows.filter((row) => rowOutcome(row) === 'covered').length;
                  const skippedCount = spec.rows.filter((row) => rowOutcome(row) === 'skipped').length;
                  const incompleteCount = spec.rows.filter((row) => rowOutcome(row) === 'incomplete').length;
                  const evidence = spec.rows.flatMap((row) => row.evidence ?? []);
                  const cardIds = new Set(evidence.map((item) => item.parent_card_id).filter((value): value is string => Boolean(value)));
                  const sourceRefs = new Set(evidence.map((item) => item.source_ref).filter((value): value is string => Boolean(value)));
                  const specTargets = targets.filter((target) => (
                    (typeof target.card_id === 'string' && cardIds.has(target.card_id))
                    || (typeof target.source_ref === 'string' && sourceRefs.has(target.source_ref))
                  ));
                  const targetIds = new Set(specTargets.map((target, index) => recordId(target, `target-${index}`)));
                  const specResolutions = resolutions.filter((record) => typeof record.target_id === 'string' && targetIds.has(record.target_id));
                  const resolutionIds = new Set(specResolutions.map((record, index) => recordId(record, `resolution-${index}`)));
                  const specExecutions = executions.filter((record) => typeof record.target_id === 'string' && targetIds.has(record.target_id));
                  const specOverlaps = overlaps.filter((record) => (
                    (typeof record.target_a_id === 'string' && targetIds.has(record.target_a_id))
                    || (typeof record.target_b_id === 'string' && targetIds.has(record.target_b_id))
                    || (typeof record.resolution_a_id === 'string' && resolutionIds.has(record.resolution_a_id))
                    || (typeof record.resolution_b_id === 'string' && resolutionIds.has(record.resolution_b_id))
                  ));
                  const specWaivers = waivers.filter((record) => (
                    record.entity_id === spec.specId
                    || (typeof record.entity_id === 'string' && cardIds.has(record.entity_id))
                  ));

                  return (
                    <article key={specKey} className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700" data-testid="coverage-spec-row">
                      <div className="flex flex-wrap items-center gap-3 bg-gray-50 px-3 py-2 dark:bg-gray-900/30">
                        <button
                          type="button"
                          aria-expanded={isOpen}
                          onClick={() => setExpanded((previous) => {
                            const next = new Set(previous);
                            if (next.has(specKey)) next.delete(specKey); else next.add(specKey);
                            return next;
                          })}
                          className="flex min-w-0 flex-1 items-center gap-2 text-left"
                        >
                          {isOpen ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
                          <span className="truncate text-xs font-semibold text-gray-800 dark:text-gray-100">{specTitle}</span>
                          <span className="shrink-0 text-[10px] text-gray-400">Edition {spec.edition}</span>
                        </button>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <StatusBadge value="covered" title={`${coveredCount} covered`} />
                          <span className="text-[10px] text-gray-500">{coveredCount}/{spec.rows.length}</span>
                          {skippedCount > 0 && <StatusBadge value="skipped" title={`${skippedCount} skipped`} />}
                          {incompleteCount > 0 && <StatusBadge value="incomplete" title={`${incompleteCount} incomplete`} />}
                          {specOverlaps.length > 0 && <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-700 dark:bg-violet-950/40 dark:text-violet-300">{specOverlaps.length} overlaps</span>}
                          {specWaivers.length > 0 && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">{specWaivers.length} waivers</span>}
                          <button type="button" onClick={() => onOpenSpec(spec.specId, specTitle)} className="rounded-md border border-gray-200 px-2 py-1 text-[10px] font-semibold hover:bg-white dark:border-gray-600 dark:hover:bg-gray-800">Open Spec</button>
                        </div>
                      </div>

                      {isOpen && (
                        <div className="space-y-4 p-3">
                          <div className="overflow-x-auto">
                            <table className="w-full min-w-[900px] text-left text-[11px]">
                              <thead><tr className="border-b border-gray-200 text-[10px] uppercase tracking-wide text-gray-400 dark:border-gray-700"><th className="pb-2">Obligation</th><th className="pb-2">Outcome</th><th className="pb-2">Lifecycle</th><th className="pb-2">Currentness</th><th className="pb-2">Evidence / receipt</th><th className="pb-2">Authority</th></tr></thead>
                              <tbody>
                                {spec.rows.map((row) => (
                                  <tr key={`${row.identity.obligation_type}:${row.identity.obligation_id}`} className="border-b border-gray-100 align-top dark:border-gray-700/60">
                                    <td className="py-2 pr-3"><p className="font-semibold text-gray-800 dark:text-gray-100">{obligationLabel(row.identity.obligation_type ?? 'unknown')}</p><p className="font-mono text-[10px] text-gray-400">{shortRef(row.identity.obligation_id)}</p></td>
                                    <td className="py-2 pr-3"><StatusBadge value={rowOutcome(row)} title={row.reason ?? row.skip?.reason_code ?? undefined} /></td>
                                    <td className="py-2 pr-3"><StatusBadge value={rowLifecycle(row)} /></td>
                                    <td className="py-2 pr-3"><StatusBadge value={row.identity.currentness ?? row.currentness ?? 'unknown'} /></td>
                                    <td className="py-2 pr-3">
                                      {(row.evidence ?? []).length === 0 ? <span className="text-gray-400">No eligible evidence</span> : (row.evidence ?? []).map((item) => (
                                        <div key={item.evidence_id} className="mb-1 flex flex-wrap items-center gap-1">
                                          <span className="font-mono text-[10px]" title={item.evidence_id}>{shortRef(item.evidence_id)}</span>
                                          <StatusBadge value={item.currentness ?? item.eligibility?.toString()} title={item.currentness_reason ?? undefined} />
                                          {item.source_ref && <span className="text-[10px] text-gray-400" title={item.source_ref}>{shortRef(item.source_ref)}</span>}
                                        </div>
                                      ))}
                                    </td>
                                    <td className="py-2"><span className="font-mono text-[10px] text-gray-500" title={row.authority_ref ?? undefined}>{shortRef(row.authority_ref)}</span></td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>

                          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5" aria-label="Code Evidence drilldown">
                            {[
                              ['Targets', specTargets],
                              ['Resolutions', specResolutions],
                              ['Executions', specExecutions],
                              ['Overlaps', specOverlaps],
                              ['Waivers', specWaivers],
                            ].map(([label, records]) => (
                              <div key={label as string} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                                <div className="flex items-center justify-between gap-2"><h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{label as string}</h5><span className="text-xs font-bold">{(records as CanonicalAnalyticsRecord[]).length}</span></div>
                                <div className="mt-2 space-y-2">
                                  {(records as CanonicalAnalyticsRecord[]).slice(0, 4).map((record, index) => (
                                    <div key={recordId(record, `${label}-${index}`)} className="rounded-md bg-gray-50 p-2 dark:bg-gray-900/40">
                                      <p className="truncate font-mono text-[10px]" title={recordId(record, `${label}-${index}`)}>{shortRef(recordId(record, `${label}-${index}`))}</p>
                                      <div className="mt-1 flex flex-wrap gap-1">
                                        <StatusBadge value={recordBadge(record)} title={String(record.currentness_reason ?? record.reason ?? '') || undefined} />
                                        {recordAge(record) !== null && <span className="text-[10px] text-gray-400">{ageLabel(recordAge(record))}</span>}
                                        {recordReceipt(record) && <span className="rounded-full border border-cyan-200 bg-cyan-50 px-2 py-0.5 text-[10px] font-semibold text-cyan-700 dark:border-cyan-800 dark:bg-cyan-950/30 dark:text-cyan-300" title={recordReceipt(record) ?? undefined}>Receipt {shortRef(recordReceipt(record))}</span>}
                                      </div>
                                    </div>
                                  ))}
                                  {(records as CanonicalAnalyticsRecord[]).length === 0 && <p className="text-[10px] text-gray-400">None recorded</p>}
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </div>
          </>}

          <p className="text-[10px] text-gray-400">
            as_of {data.as_of} · query {data.query_fingerprint.slice(0, 12)}…{data.contract_version ? ` · contract ${data.contract_version}` : ''}
          </p>
        </div>
      )}
    </section>
  );
}
