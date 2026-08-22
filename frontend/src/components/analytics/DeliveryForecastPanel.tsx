import { useMemo, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  CalendarRange,
  CheckCircle2,
  Clock3,
  Gauge,
  Download,
  RefreshCw,
  Search,
  ShieldCheck,
  Target,
} from 'lucide-react';
import type {
  DeliveryForecastResponse,
  SprintAnalyticsResponse,
  SprintCommitmentProjection,
} from './analyticsDeliveryTypes';

interface DeliveryForecastPanelProps {
  sprints: SprintAnalyticsResponse | null;
  forecast: DeliveryForecastResponse | null;
  forecastLoading: boolean;
  forecastError: string | null;
  forecastExporting: boolean;
  from: string;
  to: string;
  onRetryForecast: () => void;
  onExportForecast: () => Promise<void>;
  compact?: boolean;
  onOpenFullView?: () => void;
}

function words(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function tone(value: string | null | undefined): string {
  const state = (value ?? '').toLowerCase();
  if (['available', 'ready', 'current', 'healthy', 'complete', 'closed'].includes(state)) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300';
  }
  if (['partial', 'at_risk', 'stale', 'active', 'review', 'unavailable_legacy', 'insufficient_history', 'empty'].includes(state)) {
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-300';
  }
  if (['unavailable', 'restricted', 'error', 'inconsistent', 'not_ready'].includes(state)) {
    return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300';
  }
  return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
}

function StateBadge({ value, title }: { value: string | null | undefined; title?: string }) {
  return <span title={title} className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${tone(value)}`}>{words(value)}</span>;
}

function fact(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'Unavailable';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.length === 0 ? 'None recorded' : value.map(fact).join(', ');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => `${words(key)}: ${fact(nested)}`)
      .join(' · ');
  }
  return 'Unavailable';
}

function commitmentFact(commitment: SprintCommitmentProjection, key: keyof SprintCommitmentProjection): string {
  return fact(commitment[key]);
}

export function DeliveryForecastPanel({
  sprints,
  forecast,
  forecastLoading,
  forecastError,
  forecastExporting,
  from,
  to,
  onRetryForecast,
  onExportForecast,
  compact = false,
  onOpenFullView,
}: DeliveryForecastPanelProps) {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('all');
  const items = useMemo(() => sprints?.sprints ?? [], [sprints]);
  const statuses = useMemo(() => Array.from(new Set(items.map((item) => item.status))).sort(), [items]);
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((item) => (
      (status === 'all' || item.status === status)
      && (!needle || [item.sprint_id, item.title, item.spec_id, item.status].join(' ').toLowerCase().includes(needle))
    ));
  }, [items, search, status]);
  const commitments = items.filter((item) => item.commitment.state === 'available');
  const missingCommitments = items.length - commitments.length;
  const scopeChanges = commitments.reduce((total, item) => (
    total + (item.commitment.added_count ?? 0) + (item.commitment.removed_count ?? 0)
  ), 0);

  if (compact) {
    return (
      <section id="analytics-delivery-forecast" aria-labelledby="delivery-forecast-heading" className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800" data-testid="delivery-forecast-panel">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2"><CalendarRange className="h-4 w-4 text-blue-500" aria-hidden="true" /><h3 id="delivery-forecast-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">Delivery Intelligence</h3></div>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Immutable Sprint commitment, scope change, lane-aware throughput, contribution context, and forecast readiness.</p>
            <p className="mt-1 text-[10px] text-gray-400">Period {from} through {to}</p>
          </div>
          <button type="button" onClick={onOpenFullView} disabled={!onOpenFullView} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-3 text-xs font-semibold text-blue-700 disabled:opacity-50 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300">Open full view <ArrowRight className="h-3.5 w-3.5" /></button>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Sprints</p><p className="mt-1 text-xl font-bold">{sprints?.summary.total_sprints ?? 0}</p><p className="text-[10px] text-gray-500">Selected period</p></div>
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Activation baselines</p><p className="mt-1 text-xl font-bold">{commitments.length}</p><p className="text-[10px] text-gray-500">{missingCommitments} unavailable</p></div>
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Scope changes</p><p className="mt-1 text-xl font-bold">{commitments.length === 0 ? 'Unavailable' : scopeChanges}</p><p className="text-[10px] text-gray-500">Added + removed facts</p></div>
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Forecast readiness</p><div className="mt-1"><StateBadge value={forecastLoading ? 'loading' : forecast?.readiness.state ?? 'unavailable'} title={forecastError ?? forecast?.readiness.reason ?? undefined} /></div><p className="mt-1 text-[10px] text-gray-500">Loaded independently from delivery facts</p></div>
        </div>
      </section>
    );
  }

  return (
    <section id="analytics-delivery-forecast" aria-labelledby="delivery-forecast-heading" className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800" data-testid="delivery-forecast-panel">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><div className="flex items-center gap-2">
          <CalendarRange className="h-4 w-4 text-blue-500" aria-hidden="true" />
          <h3 id="delivery-forecast-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">Sprint Delivery &amp; Forecasting</h3>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">Committed scope, observed completion and server-produced delivery forecasts are separate facts. Missing history never becomes an estimated date.</p>
        <p className="mt-1 text-[10px] text-gray-400">Period {from} through {to}{forecast ? ` · updated ${forecast.provenance.observed_at}` : ''}</p></div>
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={forecastLoading} onClick={onRetryForecast} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><RefreshCw className="h-3.5 w-3.5" /> Refresh</button>
          <button type="button" disabled={forecastExporting || forecastLoading || forecast === null} onClick={() => void onExportForecast()} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><Download className="h-3.5 w-3.5" /> {forecastExporting ? 'Exporting…' : 'Complete CSV'}</button>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-label="Sprint delivery KPIs">
        {[
          { label: 'Sprints', value: sprints?.summary.total_sprints ?? 0, note: 'Selected period', icon: CalendarRange },
          { label: 'Active', value: sprints?.summary.status_breakdown.active ?? 0, note: 'Lifecycle fact', icon: Clock3 },
          { label: 'Avg completion', value: sprints?.summary.avg_completion_rate === null || !sprints ? 'Unavailable' : `${sprints.summary.avg_completion_rate}%`, note: 'Server projection', icon: Gauge },
          { label: 'Baselines', value: commitments.length, note: `${missingCommitments} unavailable`, icon: ShieldCheck },
          { label: 'Scope changes', value: commitments.length === 0 ? 'N/A' : scopeChanges, note: 'Added + removed facts', icon: Target },
          { label: 'Forecast', value: forecastLoading ? 'Loading' : words(forecast?.result_state), note: forecast?.readiness.reason ? words(forecast.readiness.reason) : 'Canonical readiness', icon: forecast?.readiness.ready ? CheckCircle2 : AlertTriangle },
        ].map(({ label, value, note, icon: Icon }) => (
          <div key={label} className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
            <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400"><Icon className="h-3.5 w-3.5" aria-hidden="true" /> {label}</div>
            <p className="mt-1 text-xl font-bold text-gray-900 dark:text-white">{value}</p>
            <p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">{note}</p>
          </div>
        ))}
      </div>

      <div className="mt-5 rounded-lg border border-blue-200 bg-blue-50/40 p-4 dark:border-blue-900 dark:bg-blue-950/20" aria-label="Delivery forecast authority">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Phase 2 · Canonical forecast</h4><p className="mt-0.5 text-[10px] text-gray-500">Only values returned by the governed forecast contract are shown.</p></div>
          {forecast && <StateBadge value={forecast.result_state} title={forecast.readiness.reason ?? undefined} />}
        </div>

        {forecastLoading && <p className="mt-3 text-xs text-gray-500" role="status">Loading delivery forecast…</p>}
        {!forecastLoading && forecastError && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-md bg-red-50 px-3 py-2 dark:bg-red-950/30" role="alert">
            <p className="text-xs text-red-700 dark:text-red-300">{forecastError}</p>
            <button type="button" onClick={onRetryForecast} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 dark:text-red-300"><RefreshCw className="h-3.5 w-3.5" /> Retry</button>
          </div>
        )}
        {!forecastLoading && !forecastError && !forecast && (
          <div className="mt-3 rounded-md border border-dashed border-amber-300 bg-amber-50 px-3 py-3 dark:border-amber-800 dark:bg-amber-950/20">
            <div className="flex items-center gap-2"><StateBadge value="unavailable" /><p className="text-xs font-semibold text-amber-800 dark:text-amber-200">Forecast contract not supplied</p></div>
            <p className="mt-1 text-[10px] text-amber-700 dark:text-amber-300">No completion point, confidence bound or backtest result is inferred from sprint completion.</p>
          </div>
        )}
        {!forecastLoading && !forecastError && forecast && (
          <div className="mt-3 space-y-3">
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-md border border-blue-100 bg-white p-3 dark:border-blue-900 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Readiness</p><div className="mt-1"><StateBadge value={forecast.readiness.state} /></div><p className="mt-1 text-xs font-semibold">{forecast.readiness.actual_observations}/{forecast.readiness.required_observations} observations</p><p className="mt-1 text-[10px] text-gray-500">Rule {forecast.readiness.rule_version}</p></div>
              <div className="rounded-md border border-blue-100 bg-white p-3 dark:border-blue-900 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Point</p><p className="mt-1 text-lg font-bold">{fact(forecast.forecast?.point)}</p><p className="mt-1 text-[10px] text-gray-500">Method {fact(forecast.forecast?.method_version)}</p></div>
              <div className="rounded-md border border-blue-100 bg-white p-3 dark:border-blue-900 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Confidence bounds</p><p className="mt-1 text-xs font-semibold">{fact(forecast.forecast?.lower_bound)} → {fact(forecast.forecast?.upper_bound)}</p><p className="mt-1 text-[10px] text-gray-500">Level {fact(forecast.forecast?.confidence_level)}</p></div>
              <div className="rounded-md border border-blue-100 bg-white p-3 dark:border-blue-900 dark:bg-gray-900/40"><p className="text-[10px] font-semibold uppercase text-gray-400">Backtest</p><div className="mt-1"><StateBadge value={forecast.backtest.state} title={forecast.backtest.reason ?? undefined} /></div><p className="mt-1 text-[10px] text-gray-500">Error {fact(forecast.backtest.error)} · calibration {fact(forecast.backtest.calibration)}</p></div>
            </div>
            {!forecast.readiness.ready && (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-200"><span className="font-semibold">Not ready:</span> {forecast.readiness.reason ? words(forecast.readiness.reason) : 'Canonical readiness has not been satisfied.'}{forecast.readiness.remediation && <span> · {forecast.readiness.remediation}</span>}</div>
            )}
            {forecast.forecast && (
              <dl className="grid gap-2 text-[10px] md:grid-cols-2 xl:grid-cols-4">
                <div><dt className="text-gray-400">Horizon</dt><dd className="font-medium">{fact(forecast.forecast.horizon)}</dd></div>
                <div><dt className="text-gray-400">Sample size</dt><dd className="font-medium">{forecast.forecast.sample_size}</dd></div>
                <div><dt className="text-gray-400">Source period</dt><dd className="font-medium">{fact(forecast.forecast.source_period)}</dd></div>
                <div><dt className="text-gray-400">Assumptions</dt><dd className="font-medium">{fact(forecast.forecast.assumptions)}</dd></div>
              </dl>
            )}
            <div className="grid gap-2 text-[10px] text-gray-500 md:grid-cols-2 xl:grid-cols-4">
              <div><span className="text-gray-400">Currentness</span><div className="mt-1"><StateBadge value={forecast.provenance.currentness} title={forecast.provenance.reason ?? undefined} /></div></div>
              <div><span className="text-gray-400">Population</span><p className="mt-1 font-medium">{forecast.population_scope.accessible_count} accessible · {forecast.population_scope.excluded_count} excluded</p></div>
              <div><span className="text-gray-400">Exclusions</span><p className="mt-1 font-medium">{forecast.exclusions.excluded_count} total · {forecast.exclusions.restricted_count} restricted</p></div>
              <div><span className="text-gray-400">Dependencies</span><p className="mt-1 font-medium">foundation {forecast.dependency_versions.analytics_foundation} · delivery {forecast.dependency_versions.delivery_phase_1}</p></div>
            </div>
            <p className="text-[10px] text-gray-400">as_of {forecast.as_of} · observed {forecast.provenance.observed_at} · query {forecast.query_fingerprint.slice(0, 12)}… · sources {forecast.provenance.sources.length} · filters {forecast.filters.length} · contract {forecast.contract_version}</p>
          </div>
        )}
      </div>

      <div className="mt-5">
        <div className="flex items-center justify-between gap-3"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Phase 1 · Sprint delivery facts</h4><p className="text-[10px] text-gray-500">Completion is current membership; commitment is the immutable activation baseline.</p></div></div>
        <div className="mt-3 grid gap-3 rounded-lg border border-gray-200 bg-gray-50 p-3 md:grid-cols-[minmax(0,1fr)_180px] dark:border-gray-700 dark:bg-gray-900/30">
          <label className="relative"><span className="sr-only">Search Sprints</span><Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search Sprint, Spec or status…" className="min-h-9 w-full rounded-md border border-gray-300 bg-white py-2 pl-9 pr-3 text-xs dark:border-gray-600 dark:bg-gray-800" /></label>
          <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Status<select value={status} onChange={(event) => setStatus(event.target.value)} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800"><option value="all">All statuses</option>{statuses.map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></label>
        </div>
        <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
          <table className="w-full min-w-[980px] text-left text-xs">
            <thead><tr className="border-b border-gray-200 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-400 dark:border-gray-700 dark:bg-gray-900/30"><th className="px-3 py-2">Sprint</th><th className="px-3 py-2">Status</th><th className="px-3 py-2">Current completion</th><th className="px-3 py-2">Activation commitment</th><th className="px-3 py-2">Task gate</th><th className="px-3 py-2">Evaluation</th></tr></thead>
            <tbody>
              {filtered.map((sprint) => <tr key={sprint.sprint_id} className="border-b border-gray-100 align-top last:border-0 dark:border-gray-700/60"><th className="px-3 py-3"><p className="font-semibold text-gray-800 dark:text-gray-100">{sprint.title}</p><p className="font-mono text-[10px] text-gray-400">{sprint.sprint_id} · Spec {sprint.spec_id}</p></th><td className="px-3 py-3"><StateBadge value={sprint.status} /></td><td className="px-3 py-3"><p className="font-semibold">{sprint.done_cards}/{sprint.total_cards} · {sprint.completion_rate}%</p><p className="mt-1 text-[10px] text-gray-400">Current membership projection</p></td><td className="px-3 py-3">{sprint.commitment.state === 'available' ? <div><StateBadge value="available" title={sprint.commitment.baseline_ref ?? undefined} /><p className="mt-1 text-[10px]">Original {commitmentFact(sprint.commitment, 'original_member_count')} · current {commitmentFact(sprint.commitment, 'current_member_count')}</p><p className="text-[10px] text-gray-500">Added {commitmentFact(sprint.commitment, 'added_count')} · removed {commitmentFact(sprint.commitment, 'removed_count')}</p><p className="mt-1 font-mono text-[10px] text-gray-400">{commitmentFact(sprint.commitment, 'baseline_ref')}</p></div> : <div><StateBadge value={sprint.commitment.state} title={sprint.commitment.unavailable_reason ?? undefined} /><p className="mt-1 text-[10px] text-amber-700 dark:text-amber-300">{sprint.commitment.unavailable_reason ? words(sprint.commitment.unavailable_reason) : 'Activation baseline unavailable'}</p></div>}</td><td className="px-3 py-3"><p className="font-semibold text-emerald-600 dark:text-emerald-400">{sprint.task_validation_gate.total_success} passed</p><p className="text-[10px] text-red-500">{sprint.task_validation_gate.total_failed} failed · {sprint.task_validation_gate.total_submitted} submitted</p></td><td className="px-3 py-3">{sprint.last_evaluation ? <div><StateBadge value={sprint.last_evaluation.recommendation} /><p className="mt-1 text-[10px]">Score {fact(sprint.last_evaluation.overall_score)}</p></div> : <span className="text-[10px] text-gray-400">Unavailable</span>}</td></tr>)}
              {filtered.length === 0 && <tr><td colSpan={6} className="px-3 py-8 text-center text-xs text-gray-400">No Sprints match the current filters.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
