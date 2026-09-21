import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CalendarRange,
  Download,
  RefreshCw,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import type {
  DeliveryIntelligenceFilters,
  DeliveryIntelligenceResponse,
  DeliveryMetric,
} from './analyticsDeliveryTypes';

interface DeliveryIntelligenceFullViewProps {
  boardId: string;
  from: string;
  to: string;
  initialFilters?: DeliveryIntelligenceFilters;
  onFiltersChange?: (filters: DeliveryIntelligenceFilters) => void;
  onPeriodChange?: (days: 30 | 90) => void;

}

function words(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

function stateTone(value: string | null | undefined): string {
  const state = (value ?? '').toLowerCase();
  if (['available', 'ready', 'current', 'success', 'approve'].includes(state)) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300';
  }
  if (['partial', 'insufficient_history', 'empty', 'previous'].includes(state)) {
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-300';
  }
  if (['restricted', 'unavailable', 'error', 'failed'].includes(state)) {
    return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300';
  }
  return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
}

function StateBadge({ value, title }: { value: string | null | undefined; title?: string | null }) {
  return (
    <span
      title={title ?? undefined}
      className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${stateTone(value)}`}
    >
      {words(value)}
    </span>
  );
}

function metricValue(metric: DeliveryMetric, suffix = ''): string {
  if (metric.state !== 'available' && metric.state !== 'partial') return words(metric.state);
  return metric.value === null ? 'Unavailable' : `${metric.value}${suffix}`;
}

function metricContext(metric: DeliveryMetric): string {
  if (metric.numerator !== null && metric.denominator !== null) {
    return `${metric.numerator}/${metric.denominator} · n = ${metric.sample_size}`;
  }
  if (metric.reason) return words(metric.reason);
  return `n = ${metric.sample_size}`;
}

function formatCycle(value: number | null): string {
  if (value === null) return 'Unavailable';
  if (value < 24) return `${value.toFixed(1)} hours`;
  return `${(value / 24).toFixed(1)} days`;
}

function sameFilters(a: DeliveryIntelligenceFilters, b: DeliveryIntelligenceFilters): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function DeliveryIntelligenceFullView({
  boardId,
  from,
  to,
  initialFilters,
  onFiltersChange,
  onPeriodChange,
}: DeliveryIntelligenceFullViewProps) {
  const api = useDashboardApi();
  const [filters, setFilters] = useState<DeliveryIntelligenceFilters>({
    role: 'all',
    contributionView: 'self_and_aggregates',
    limit: 25,
    ...(initialFilters ?? {}),
  });
  const [data, setData] = useState<DeliveryIntelligenceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState<string | null>(null);
  const requestSequence = useRef(0);

  useEffect(() => {
    const next = {
      role: 'all',
      contributionView: 'self_and_aggregates' as const,
      limit: 25,
      ...(initialFilters ?? {}),
    };
    setFilters((current) => (sameFilters(current, next) ? current : next));
  }, [initialFilters]);

  const queryFilters = useMemo(
    () => ({ ...filters, cursor: undefined }),
    [filters],
  );

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setLoadingMore(false);
    setPaginationError(null);
    setError(null);
    try {
      const response = await api.getBoardDeliveryIntelligence(boardId, from, to, queryFilters);
      if (requestSequence.current === sequence) setData(response);
    } catch (caught) {
      if (requestSequence.current === sequence) {
        setData(null);
        setError(caught instanceof Error ? caught.message : 'Delivery Intelligence is unavailable.');
      }
    } finally {
      if (requestSequence.current === sequence) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, queryFilters, retry]);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
    };
  }, [load]);


  const updateFilters = (patch: Partial<DeliveryIntelligenceFilters>) => {
    setFilters((current) => {
      const next = { ...current, ...patch, cursor: undefined };
      onFiltersChange?.(next);
      return next;
    });
  };

  const loadMore = async () => {
    if (!data?.next_cursor || loadingMore) return;
    const sequence = ++requestSequence.current;
    setLoadingMore(true);
    setPaginationError(null);
    try {
      const next = await api.getBoardDeliveryIntelligence(boardId, from, to, {
        ...filters,
        cursor: data.next_cursor,
      });
      if (requestSequence.current === sequence) {
        setData({ ...next, contributions: [...data.contributions, ...next.contributions] });
      }
    } catch (caught) {
      if (requestSequence.current === sequence) {
        setPaginationError(caught instanceof Error ? caught.message : 'Could not load more contributions.');
      }
    } finally {
      if (requestSequence.current === sequence) setLoadingMore(false);
    }
  };

  const exportCsv = async () => {
    if (exporting) return;
    setExporting(true);
    setExportError(null);
    try {
      await api.exportBoardDeliveryIntelligenceCsv(boardId, from, to, filters);
    } catch (caught) {
      setExportError(caught instanceof Error ? caught.message : 'Delivery Intelligence export failed.');
    } finally {
      setExporting(false);
    }
  };

  const roleOptions = useMemo(
    () => Array.from(new Set([
      'Implementation agent',
      'Validation agent',
      'Implementation + validation',
      ...(data?.contributions.map((item) => item.role) ?? []),
    ])).sort(),
    [data?.contributions],
  );

  return (
    <main aria-label="Delivery Intelligence analytics" className="space-y-6" data-testid="delivery-intelligence-full-view">
      <header className="rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold text-blue-600 dark:text-blue-400">Analytics</p>
            <h2 className="mt-1 text-2xl font-semibold text-gray-900 dark:text-white">Delivery Intelligence</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Current contributions from non-archived Cards created in the selected period. Sprint metrics are discontinued.</p>
          </div>
          <div className="flex items-center gap-2">
            {data && <StateBadge value={data.result_state} title={data.provenance.reason} />}
            <button type="button" onClick={() => setRetry((value) => value + 1)} disabled={loading} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-gray-300 px-3 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><RefreshCw className="h-3.5 w-3.5" /> Refresh</button>
            <button type="button" onClick={() => void exportCsv()} disabled={exporting || loading || data === null} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-gray-300 px-3 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><Download className="h-3.5 w-3.5" /> {exporting ? 'Exporting…' : 'Export CSV'}</button>
          </div>
        </div>

        <form className="mt-5 grid gap-3 md:grid-cols-3" aria-label="Delivery filters" onSubmit={(event) => event.preventDefault()}>
          <label className="text-xs font-medium text-gray-500">Period
            <select aria-label="Delivery period" defaultValue="custom" disabled={exporting} onChange={(event) => { if (event.target.value === '30') onPeriodChange?.(30); if (event.target.value === '90') onPeriodChange?.(90); }} className="mt-1 min-h-10 w-full rounded-md border border-gray-300 bg-white px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900">
              <option value="custom">{from} through {to}</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
            </select>
          </label>
          <label className="text-xs font-medium text-gray-500">Role
            <select aria-label="Contribution role" value={filters.role ?? 'all'} disabled={exporting} onChange={(event) => updateFilters({ role: event.target.value })} className="mt-1 min-h-10 w-full rounded-md border border-gray-300 bg-white px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900">
              <option value="all">All roles</option>
              {roleOptions.map((role) => <option key={role} value={role.toLowerCase().replace(/\s+/g, '_')}>{role}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium text-gray-500">Contribution view
            <select aria-label="Contribution visibility" value={filters.contributionView ?? 'self_and_aggregates'} disabled={exporting} onChange={(event) => updateFilters({ contributionView: event.target.value as DeliveryIntelligenceFilters['contributionView'] })} className="mt-1 min-h-10 w-full rounded-md border border-gray-300 bg-white px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900">
              <option value="self_and_aggregates">Self + aggregates</option><option value="self">Self only</option><option value="aggregates">Aggregates only</option><option value="operator">Authorized operator</option>
            </select>
          </label>
        </form>
      </header>

      {loading && <div className="rounded-xl border border-gray-200 bg-white p-8 text-center text-sm text-gray-500 dark:border-gray-700 dark:bg-gray-800" role="status">Loading Delivery Intelligence…</div>}
      {!loading && error && <div className="flex items-center justify-between rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300" role="alert"><span className="flex items-center gap-2"><AlertTriangle className="h-4 w-4" />{error}</span><button type="button" className="font-semibold underline" onClick={() => setRetry((value) => value + 1)}>Retry</button></div>}
      {exportError && <div className="flex items-center justify-between rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300" role="alert"><span className="flex items-center gap-2"><AlertTriangle className="h-4 w-4" />CSV export failed: {exportError}</span><button type="button" disabled={exporting} className="font-semibold underline disabled:opacity-50" onClick={() => void exportCsv()}>Retry export</button></div>}
      {!loading && !error && data?.result_state === 'empty' && <div className="rounded-xl border border-dashed border-gray-300 bg-white p-8 text-center dark:border-gray-700 dark:bg-gray-800"><CalendarRange className="mx-auto h-6 w-6 text-gray-400" /><p className="mt-2 text-sm font-semibold">No delivery evidence in this period</p><p className="mt-1 text-xs text-gray-500">Change the period, role, or contribution visibility.</p></div>}

      {!loading && data && data.result_state !== 'empty' && (
        <>
          <section>
            <article className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700"><div><h3 className="font-semibold">Contribution by role</h3><p className="text-xs text-gray-500">Context, denominators, and samples are shown; rows are not ranked.</p></div><span className="rounded-full border border-violet-200 bg-violet-50 px-3 py-1 text-[10px] text-violet-700 dark:border-violet-800 dark:bg-violet-950/30 dark:text-violet-300">{words(filters.contributionView)} · minimum n = {data.minimum_sample_size}</span></div>
              <div className="border-b border-gray-200 bg-blue-50/60 px-5 py-3 text-xs text-blue-700 dark:border-gray-700 dark:bg-blue-950/20 dark:text-blue-300" role="status">Named metrics are limited to you or an authorized operator. Other contributors remain minimum-sample-protected aggregates.</div>
              <div className="overflow-x-auto"><table className="w-full min-w-[900px] text-left text-xs"><caption className="sr-only">Role-aware contribution metrics</caption><thead className="bg-gray-50 text-[10px] uppercase text-gray-400 dark:bg-gray-900/40"><tr><th className="px-4 py-3">Subject</th><th className="px-4 py-3">Role</th><th className="px-4 py-3">Done n</th><th className="px-4 py-3">First pass</th><th className="px-4 py-3">Validation success</th><th className="px-4 py-3">Rework</th><th className="px-4 py-3">Median cycle</th></tr></thead><tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {data.contributions.map((row, index) => <tr key={row.subject_id ?? `${row.visibility}-${index}`}><th className="px-4 py-3 font-semibold">{row.subject_label}<div className="mt-1"><StateBadge value={row.visibility} /></div></th><td className="px-4 py-3">{row.role}</td><td className="px-4 py-3">{row.done_count ?? 'Restricted'}</td><td className="px-4 py-3">{metricValue(row.first_pass, '%')}<p className="text-[10px] text-gray-400">{metricContext(row.first_pass)}</p></td><td className="px-4 py-3">{metricValue(row.validation_success, '%')}<p className="text-[10px] text-gray-400">{metricContext(row.validation_success)}</p></td><td className="px-4 py-3">{row.rework_introduced === null ? 'Restricted' : `${row.rework_introduced} introduced · ${row.rework_resolved ?? 0} resolved`}</td><td className="px-4 py-3">{row.median_cycle_hours.state === 'available' || row.median_cycle_hours.state === 'partial' ? formatCycle(row.median_cycle_hours.value) : words(row.median_cycle_hours.state)}<p className="text-[10px] text-gray-400">{metricContext(row.median_cycle_hours)}</p></td></tr>)}
                {data.contributions.length === 0 && <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-400">No authorized contribution sample is available.</td></tr>}
              </tbody></table></div>
              {data.next_cursor && <div className="border-t border-gray-200 p-3 text-center dark:border-gray-700">{paginationError && <div className="mb-3 flex items-center justify-center gap-3 text-xs text-red-700 dark:text-red-300" role="alert"><span>{paginationError}</span><button type="button" className="font-semibold underline" onClick={() => void loadMore()}>Retry page</button></div>}<button type="button" disabled={loadingMore} onClick={() => void loadMore()} className="rounded-md border border-gray-300 px-3 py-1.5 text-xs font-semibold disabled:opacity-50 dark:border-gray-600">{loadingMore ? 'Loading…' : 'Load more contributions'}</button></div>}
              <p className="border-t border-gray-200 px-5 py-3 text-[10px] text-gray-500 dark:border-gray-700">No hidden productivity score is computed. Every displayed rate includes its numerator, denominator, period, and role.</p>
            </article>

          </section>
        </>
      )}
    </main>
  );
}
