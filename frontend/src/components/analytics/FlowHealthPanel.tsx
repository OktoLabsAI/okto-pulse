import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertOctagon,
  ChevronDown,
  ChevronRight,
  Clock3,
  Download,
  ExternalLink,
  Filter,
  History,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Settings2,
  ShieldAlert,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import type {
  CanonicalAnalyticsRecord,
  FlowHealthBlocker,
  FlowHealthResponse,
  FlowHealthSettingsResponse,
} from './analyticsCanonicalTypes';
import {
  deriveFlowHealthMetrics,
  flowHealthAuthorityState,
  formatMetric,
} from './flowHealthMetrics';
import type { FlowHealthRouteFilters } from './flowHealthQueryState';

export interface FlowHealthPanelProps {
  boardId: string;
  data: FlowHealthResponse | null;
  loading: boolean;
  error: string | null;
  exportError?: string | null;
  exporting: boolean;
  from: string;
  to: string;
  subjectTitles: Record<string, string>;
  onRetry: () => void;
  onExport: () => Promise<void>;
  onReload: () => void;
  onOpenSubject: (type: string, id: string, title: string) => void;
  initialFilters?: FlowHealthRouteFilters;
  onFiltersChange?: (filters: FlowHealthRouteFilters) => void;
  settingsMode?: 'inline' | 'separate' | 'hidden';
  onOpenSettings?: () => void;
}

const DEFAULT_GENERAL_STALE_HOURS = 72;
const DEFAULT_REJECTED_STALE_HOURS = 96;
const OVERRIDE_STATES = ['backlog', 'pending', 'in_progress', 'rejected', 'done'] as const;

function words(value: string | null | undefined): string {
  if (!value) return 'Unknown';
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function shortRef(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  const text = String(value);
  return text.length > 28 ? `${text.slice(0, 13)}…${text.slice(-10)}` : text;
}

function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'Unavailable';
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function hoursBetween(start: unknown, end: unknown): number | null {
  if (typeof start !== 'string' || typeof end !== 'string') return null;
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
  return (endMs - startMs) / 3_600_000;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function tone(value: string | null | undefined): string {
  const state = (value ?? '').toLowerCase();
  if (['healthy', 'current', 'complete', 'completed', 'available', 'resumed', 'passed'].includes(state)) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/35 dark:text-emerald-300';
  }
  if (['at_risk', 'stale', 'rejected', 'partial', 'skipped', 'previous'].includes(state)) {
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/35 dark:text-amber-300';
  }
  if (['blocked', 'failed', 'inconsistent', 'unavailable', 'recovery_needed'].includes(state)) {
    return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/35 dark:text-red-300';
  }
  return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
}

function StateBadge({ value, title }: { value: string | null | undefined; title?: string }) {
  return <span title={title} className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${tone(value)}`}>{words(value)}</span>;
}

function titleKey(type: string, id: string): string {
  const normalized = type === 'task' ? 'card' : type;
  return `${normalized}:${id}`;
}

function subjectOwner(item: FlowHealthResponse['items'][number]): string | null {
  if (typeof item.owner === 'string' && item.owner.trim()) return item.owner.trim();
  if (typeof item.assignee === 'string' && item.assignee.trim()) return item.assignee.trim();
  if (item.owner && typeof item.owner === 'object') {
    const owner = item.owner as Record<string, unknown>;
    for (const key of ['name', 'title', 'id']) {
      if (typeof owner[key] === 'string' && owner[key].trim()) return owner[key].trim();
    }
  }
  return null;
}

function normalizeOverrides(
  value: FlowHealthResponse['effective_policy']['overrides'],
): Record<string, number> {
  if (Array.isArray(value)) {
    return Object.fromEntries(value.map((item) => [item.state, item.stale_hours]));
  }
  if (!value) return {};
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, number] => (
      typeof entry[1] === 'number' && Number.isFinite(entry[1])
    )),
  );
}

function blockerRemediation(blocker: FlowHealthBlocker): string {
  if (blocker.remediation) return blocker.remediation;
  const code = blocker.code.toLowerCase();
  if (code.includes('dependency')) return 'Resolve the blocking dependency authority, then refresh Flow Health.';
  if (code.includes('defect') || code.includes('bug')) return 'Open the defect lineage, record a governed recovery outcome and re-run validation.';
  if (code.includes('execution')) return 'Complete or supersede the current execution report with canonical evidence.';
  if (code.includes('rejected')) return 'Resume the rejected episode with a governed remediation and a fresh validation outcome.';
  if (blocker.effective_skip) return 'Review the effective skip authority and clear it when the obligation becomes applicable.';
  return 'Open the governed subject, resolve this authority state and refresh Flow Health.';
}

function reportState(record: CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null | undefined): string {
  if (Array.isArray(record)) return record.length > 0 ? 'available' : 'empty';
  if (!record) return 'unavailable';
  return String(record.state ?? record.outcome ?? record.currentness ?? 'available');
}

function FactPreview({ record }: { record: CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null | undefined }) {
  if (!record || (Array.isArray(record) && record.length === 0)) {
    return <p className="mt-1 text-[10px] text-gray-400">Not supplied by the canonical Flow Health authority.</p>;
  }
  const facts = (Array.isArray(record) ? record[0] : record) as CanonicalAnalyticsRecord;
  const entries = Object.entries(facts)
    .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
    .slice(0, 4);
  return (
    <dl className="mt-2 space-y-1 text-[10px]">
      {entries.map(([key, value]) => <div key={key} className="flex justify-between gap-2"><dt className="text-gray-400">{words(key)}</dt><dd className="max-w-[60%] truncate font-medium" title={String(value)}>{shortRef(value)}</dd></div>)}
    </dl>
  );
}

export function FlowHealthPanel({
  boardId,
  data,
  loading,
  error,
  exportError = null,
  exporting,
  from,
  to,
  subjectTitles,
  onRetry,
  onExport,
  onReload,
  onOpenSubject,
  initialFilters,
  onFiltersChange,
  settingsMode = 'inline',
  onOpenSettings,
}: FlowHealthPanelProps) {
  const api = useDashboardApi();
  const [search, setSearch] = useState(initialFilters?.search ?? '');
  const [subjectType, setSubjectType] = useState(initialFilters?.workType ?? 'all');
  const [healthState, setHealthState] = useState(initialFilters?.health ?? 'all');
  const [owner, setOwner] = useState(initialFilters?.owner ?? 'all');
  const [blockersOnly, setBlockersOnly] = useState(initialFilters?.blockersOnly ?? false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [generalHours, setGeneralHours] = useState(DEFAULT_GENERAL_STALE_HOURS);
  const [rejectedHours, setRejectedHours] = useState(DEFAULT_REJECTED_STALE_HOURS);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const [settingsVersion, setSettingsVersion] = useState(1);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!initialFilters) return;
    setSearch(initialFilters.search);
    setSubjectType(initialFilters.workType);
    setHealthState(initialFilters.health);
    setOwner(initialFilters.owner);
    setBlockersOnly(initialFilters.blockersOnly);
  }, [initialFilters]);

  useEffect(() => {
    if (!data) return;
    setGeneralHours(data.effective_policy.general_stale_hours ?? DEFAULT_GENERAL_STALE_HOURS);
    setRejectedHours(data.effective_policy.rejected_stale_hours ?? DEFAULT_REJECTED_STALE_HOURS);
    setOverrides(normalizeOverrides(data.effective_policy.overrides));
    setSettingsVersion(data.effective_policy.version);
  }, [data]);

  useEffect(() => {
    if (!settingsOpen) return;
    let cancelled = false;
    setSettingsLoading(true);
    setSettingsMessage(null);
    api.getBoardFlowHealthSettings(boardId)
      .then((response) => {
        if (cancelled) return;
        const saved = response.settings;
        setGeneralHours(saved.general_stale_hours);
        setRejectedHours(saved.rejected_stale_hours);
        setOverrides(saved.overrides ?? {});
        setSettingsVersion(saved.version);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setSettingsMessage(reason instanceof Error ? reason.message : 'Could not load saved Flow Health policy.');
      })
      .finally(() => {
        if (!cancelled) setSettingsLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsOpen, boardId]);

  const typeOptions = useMemo(() => Array.from(new Set((data?.items ?? []).map((item) => item.subject.type))).sort(), [data]);
  const stateOptions = useMemo(() => Array.from(new Set((data?.items ?? []).map((item) => item.state))).sort(), [data]);
  const ownerOptions = useMemo(() => Array.from(new Set((data?.items ?? []).map(subjectOwner).filter((value): value is string => Boolean(value)))).sort(), [data]);
  const filteredItems = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (data?.items ?? []).filter((item) => {
      if (subjectType !== 'all' && item.subject.type !== subjectType) return false;
      if (healthState !== 'all' && item.state !== healthState) return false;
      if (owner !== 'all' && subjectOwner(item) !== owner) return false;
      if (blockersOnly && (item.blockers ?? []).length === 0) return false;
      if (!needle) return true;
      const title = item.subject.title ?? subjectTitles[titleKey(item.subject.type, item.subject.id)] ?? '';
      const haystack = [
        item.subject.type,
        item.subject.id,
        title,
        subjectOwner(item),
        item.state,
        item.current_episode?.state,
        ...(item.reason_codes ?? []),
        ...(item.blockers ?? []).flatMap((blocker) => [blocker.code, blocker.message, blocker.authority_ref]),
        ...(item.rework ?? []).flatMap((attempt) => [attempt.rejection_code, attempt.rejection_kind, attempt.rejection_summary]),
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }, [blockersOnly, data, healthState, owner, search, subjectTitles, subjectType]);

  const totalSubjects = data?.items.length ?? 0;
  const metrics = data ? deriveFlowHealthMetrics(data) : null;
  const authorityState = data ? flowHealthAuthorityState(data) : null;
  const blockerRows = useMemo(() => filteredItems.flatMap((item) => {
    const key = titleKey(item.subject.type, item.subject.id);
    const itemTitle = item.subject.title ?? subjectTitles[key] ?? `${words(item.subject.type)} ${item.subject.id}`;
    return (item.blockers ?? []).map((blocker) => ({ item, blocker, itemTitle, owner: subjectOwner(item) }));
  }), [filteredItems, subjectTitles]);
  const reworkAttempts = useMemo(() => filteredItems.flatMap((item) => item.rework ?? []), [filteredItems]);
  const recoveredAttempts = reworkAttempts.filter((attempt) => Boolean(attempt.completed_at ?? attempt.recovered_at ?? attempt.resolved_at));
  const repeatRejections = filteredItems.reduce((total, item) => total + Math.max(0, (item.rework ?? []).length - 1), 0);
  const recoveryLeadP50 = median(recoveredAttempts
    .map((attempt) => hoursBetween(attempt.rejected_at, attempt.completed_at ?? attempt.recovered_at ?? attempt.resolved_at))
    .filter((value): value is number => value !== null));
  const rejectionCauses = reworkAttempts.reduce((counts, attempt) => {
    const kind = words(String(attempt.rejection_kind ?? attempt.rejection_code ?? 'Unspecified'));
    counts[kind] = (counts[kind] ?? 0) + 1;
    return counts;
  }, {} as Record<string, number>);
  const dependencyReportCount = filteredItems.filter((item) => Boolean(item.dependency_report ?? item.reports?.dependency)).length;
  const defectReportCount = filteredItems.filter((item) => Boolean(item.defect_report ?? item.reports?.defect)).length;
  const executionReportCount = filteredItems.filter((item) => Boolean(item.execution_report ?? item.reports?.execution)).length;

  const applySettingsResponse = (response: FlowHealthSettingsResponse, message: string) => {
    setGeneralHours(response.settings.general_stale_hours);
    setRejectedHours(response.settings.rejected_stale_hours);
    setOverrides(response.settings.overrides ?? {});
    setSettingsVersion(response.settings.version);
    setSettingsMessage(message);
    onReload();
  };

  const emitFilters = (patch: Partial<FlowHealthRouteFilters>) => {
    onFiltersChange?.({
      search,
      workType: subjectType,
      owner,
      health: healthState,
      blockersOnly,
      ...patch,
    });
  };

  const persistPolicy = async (general: number, rejected: number, nextOverrides: Record<string, number>) => {
    if (!Number.isInteger(general) || general < 1 || general > 8760 || !Number.isInteger(rejected) || rejected < 1 || rejected > 8760) {
      setSettingsMessage('Thresholds must be whole hours between 1 and 8760.');
      return;
    }
    if (Object.values(nextOverrides).some((value) => !Number.isInteger(value) || value < 1 || value > 8760)) {
      setSettingsMessage('Override thresholds must be whole hours between 1 and 8760.');
      return;
    }
    setSettingsSaving(true);
    setSettingsMessage(null);
    try {
      const response = await api.saveBoardFlowHealthSettings(boardId, {
        expected_version: settingsVersion,
        general_stale_hours: general,
        rejected_stale_hours: rejected,
        overrides: nextOverrides,
      });
      applySettingsResponse(response, 'Flow Health policy saved.');
    } catch (reason) {
      setSettingsMessage(reason instanceof Error ? reason.message : 'Could not save Flow Health policy.');
    } finally {
      setSettingsSaving(false);
    }
  };

  const restorePolicy = async () => {
    setSettingsSaving(true);
    setSettingsMessage(null);
    try {
      const response = await api.restoreBoardFlowHealthSettings(boardId, settingsVersion);
      applySettingsResponse(response, 'Default Flow Health policy restored.');
    } catch (reason) {
      setSettingsMessage(reason instanceof Error ? reason.message : 'Could not restore the default Flow Health policy.');
    } finally {
      setSettingsSaving(false);
    }
  };

  return (
    <section
      id="analytics-flow-health"
      aria-labelledby="flow-health-heading"
      className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
      data-testid="flow-health-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-violet-500" aria-hidden="true" />
            <h3 id="flow-health-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">Flow Health</h3>
          </div>
          <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
            Governed episode age, rework, blockers and source authority. Threshold provenance is shown separately from lifecycle facts.
          </p>
          <p className="mt-1 text-[10px] text-gray-400">Period {from} through {to}{data ? ` · updated ${data.as_of} · policy ${data.effective_policy.authority_ref ?? `v${data.effective_policy.version}`}` : ''}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={loading} onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><RefreshCw className="h-3.5 w-3.5" /> Refresh</button>
          {settingsMode === 'inline' && <button type="button" onClick={() => setSettingsOpen((value) => !value)} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium dark:border-gray-600" aria-expanded={settingsOpen}>
            <Settings2 className="h-3.5 w-3.5" aria-hidden="true" /> Thresholds
          </button>}
          {settingsMode === 'separate' && <button type="button" onClick={onOpenSettings} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium dark:border-gray-600">
            <Settings2 className="h-3.5 w-3.5" aria-hidden="true" /> Board settings
          </button>}
          <button type="button" disabled={exporting || loading || data === null} onClick={() => void onExport()} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600">
            <Download className="h-3.5 w-3.5" aria-hidden="true" /> {exporting ? 'Exporting…' : 'Complete CSV'}
          </button>
        </div>
      </div>

      {settingsMode === 'inline' && settingsOpen && (
        <div className="mt-4 rounded-lg border border-violet-200 bg-violet-50/60 p-4 dark:border-violet-800 dark:bg-violet-950/20" data-testid="flow-health-settings">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div><h4 className="text-xs font-semibold text-violet-900 dark:text-violet-100">Effective Flow Health policy</h4><p className="mt-0.5 text-[10px] text-violet-700/80 dark:text-violet-300/80">Board-scoped thresholds persist through the governed board settings endpoint.</p></div>
            {data && <div className="flex flex-wrap gap-1.5"><StateBadge value={data.effective_policy.source ?? 'effective'} /><span className="rounded-full border border-violet-200 px-2 py-0.5 text-[10px] font-semibold dark:border-violet-700">Policy v{data.effective_policy.version}</span></div>}
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">General stale after (hours)<input type="number" min={1} max={8760} step={1} disabled={settingsLoading || settingsSaving} value={generalHours} onChange={(event) => setGeneralHours(Number(event.target.value))} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-3 text-xs normal-case dark:border-gray-600 dark:bg-gray-800" /></label>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Rejected stale after (hours)<input type="number" min={1} max={8760} step={1} disabled={settingsLoading || settingsSaving} value={rejectedHours} onChange={(event) => setRejectedHours(Number(event.target.value))} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-3 text-xs normal-case dark:border-gray-600 dark:bg-gray-800" /></label>
          </div>
          <fieldset className="mt-3">
            <legend className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Per-state overrides (hours)</legend>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
              {OVERRIDE_STATES.map((state) => <label key={state} className="text-[10px] font-medium text-gray-500">{words(state)}<input aria-label={`${words(state)} override (hours)`} type="number" min={1} max={8760} step={1} disabled={settingsLoading || settingsSaving} value={overrides[state] ?? ''} placeholder="Default" onChange={(event) => setOverrides((previous) => { const next = { ...previous }; if (event.target.value === '') delete next[state]; else next[state] = Number(event.target.value); return next; })} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-3 text-xs dark:border-gray-600 dark:bg-gray-800" /></label>)}
            </div>
          </fieldset>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[10px] text-gray-500">{Object.keys(overrides).length} governed override{Object.keys(overrides).length === 1 ? '' : 's'} preserved.</p>
            <div className="flex gap-2">
              <button type="button" disabled={settingsLoading || settingsSaving} onClick={() => void restorePolicy()} className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-3 py-1.5 text-xs font-semibold disabled:opacity-50 dark:border-gray-600"><RotateCcw className="h-3.5 w-3.5" /> Restore defaults</button>
              <button type="button" disabled={settingsLoading || settingsSaving} onClick={() => void persistPolicy(generalHours, rejectedHours, overrides)} className="inline-flex items-center gap-1 rounded-md bg-violet-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"><Save className="h-3.5 w-3.5" /> {settingsSaving ? 'Saving…' : 'Save policy'}</button>
            </div>
          </div>
          {settingsMessage && <p className="mt-2 text-xs text-violet-800 dark:text-violet-200" role="status">{settingsMessage}</p>}
        </div>
      )}

      {loading && <p className="mt-4 text-xs text-gray-500" role="status">Loading Flow Health…</p>}
      {!loading && error && (
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 dark:bg-red-900/20" role="alert">
          <span className="text-xs text-red-700 dark:text-red-300">{error}</span>
          <button type="button" onClick={onRetry} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 dark:text-red-300"><RefreshCw className="h-3.5 w-3.5" /> Retry</button>
        </div>
      )}

      {exportError && (
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 dark:bg-red-900/20" role="alert">
          <span className="text-xs text-red-700 dark:text-red-300">CSV export failed: {exportError}</span>
          <button type="button" disabled={exporting} onClick={() => void onExport()} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 disabled:opacity-50 dark:text-red-300">
            <Download className="h-3.5 w-3.5" aria-hidden="true" /> Retry export
          </button>
        </div>
      )}

      {!loading && !error && data && (
        <div className="mt-5 space-y-5">
          {authorityState !== 'available' && (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-200" role="status">
              Flow Health is {authorityState}. Missing or inaccessible authority is not classified as healthy.
            </p>
          )}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-label="Flow Health KPIs">
            {[
              { label: 'Blocker occurrences', value: formatMetric(metrics?.blockerOccurrences ?? null), icon: AlertOctagon, note: metrics?.blockerSubjects === null || metrics?.blockerSubjects === undefined ? 'Unique entities N/A' : `${metrics.blockerSubjects} unique entities` },
              { label: 'Rejected WIP', value: formatMetric(metrics?.rejectedWip ?? null), icon: ShieldAlert, note: metrics?.rejectedP95Hours === null || metrics?.rejectedP95Hours === undefined ? 'p95 age N/A' : `p95 age ${formatMetric(metrics.rejectedP95Hours, 'h')}` },
              { label: 'Recovery rate', value: metrics?.recoveryRate === null || metrics?.recoveryRate === undefined ? 'N/A' : `${Math.round(metrics.recoveryRate * 100)}%`, icon: History, note: metrics?.recoverySample === null || metrics?.recoverySample === undefined ? 'Sample N/A' : `n ${metrics.recoverySample}` },
              { label: 'Dependency wait', value: formatMetric(metrics?.dependencyWaitP50Hours ?? null, 'h'), icon: Clock3, note: metrics?.dependencyDepth === null || metrics?.dependencyDepth === undefined ? 'Depth N/A' : `longest chain ${metrics.dependencyDepth}` },
              { label: 'Open bugs', value: formatMetric(metrics?.openBugs ?? null), icon: AlertOctagon, note: metrics?.highSeverityBugs === null || metrics?.highSeverityBugs === undefined ? 'Severity N/A' : `${metrics.highSeverityBugs} high severity` },
              { label: 'Policy', value: `v${data.effective_policy.version}`, icon: Settings2, note: `${data.effective_policy.general_stale_hours}h / ${data.effective_policy.rejected_stale_hours}h` },
            ].map(({ label, value, icon: Icon, note }) => (
              <div key={label} className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
                <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400"><Icon className="h-3.5 w-3.5" /> {label}</div>
                <p className="mt-1 text-xl font-bold text-gray-900 dark:text-white">{value}</p>
                <p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">{note}</p>
              </div>
            ))}
          </div>

          <div className="grid gap-3 rounded-lg border border-gray-200 bg-gray-50 p-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_190px_150px_150px_150px_auto] dark:border-gray-700 dark:bg-gray-900/30" aria-label="Flow Health filters">
            <label className="relative"><span className="sr-only">Search Flow Health</span><Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" /><input value={search} onChange={(event) => { setSearch(event.target.value); emitFilters({ search: event.target.value }); }} placeholder="Search subject, reason, blocker or rejection…" className="min-h-9 w-full rounded-md border border-gray-300 bg-white py-2 pl-9 pr-3 text-xs dark:border-gray-600 dark:bg-gray-800" /></label>
            <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Time range<div className="mt-1 flex min-h-9 items-center rounded-md border border-gray-300 bg-white px-2 text-xs font-normal normal-case dark:border-gray-600 dark:bg-gray-800">{from} → {to}</div></div>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Work type<select value={subjectType} onChange={(event) => { setSubjectType(event.target.value); emitFilters({ workType: event.target.value }); }} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800"><option value="all">All work types</option>{typeOptions.map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></label>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Owner<select value={owner} onChange={(event) => { setOwner(event.target.value); emitFilters({ owner: event.target.value }); }} disabled={ownerOptions.length === 0} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800"><option value="all">{ownerOptions.length === 0 ? 'Unavailable' : 'All owners'}</option>{ownerOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Health<select value={healthState} onChange={(event) => { setHealthState(event.target.value); emitFilters({ health: event.target.value }); }} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800"><option value="all">All states</option>{stateOptions.map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></label>
            <label className="flex min-h-9 items-center gap-2 self-end rounded-md border border-gray-300 bg-white px-3 text-xs dark:border-gray-600 dark:bg-gray-800"><input type="checkbox" checked={blockersOnly} onChange={(event) => { setBlockersOnly(event.target.checked); emitFilters({ blockersOnly: event.target.checked }); }} /><Filter className="h-3.5 w-3.5" /> Blockers only</label>
          </div>

          <div>
            <div className="mb-2 flex flex-wrap items-end justify-between gap-2"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Blockers &amp; remediation</h4><p className="text-[10px] text-gray-500">Cause, age against policy, ownership, evidence and authorized next step.</p></div><span className="text-[10px] text-gray-400">{blockerRows.length} occurrences</span></div>
            <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
              <table className="w-full min-w-[1050px] text-left text-xs" aria-label="Flow Health blockers">
                <thead><tr className="border-b border-gray-200 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-400 dark:border-gray-700 dark:bg-gray-900/30"><th className="px-3 py-2">Subject / cause</th><th className="px-3 py-2">Age / threshold</th><th className="px-3 py-2">Owner</th><th className="px-3 py-2">Evidence</th><th className="px-3 py-2">Remediation</th><th className="px-3 py-2">Open</th></tr></thead>
                <tbody>{blockerRows.map(({ item, blocker, itemTitle, owner: itemOwner }, index) => <tr key={`${item.subject.type}:${item.subject.id}:${blocker.code}:${index}`} className="border-b border-gray-100 align-top last:border-0 dark:border-gray-700/60"><th className="px-3 py-3"><p className="font-semibold text-gray-800 dark:text-gray-100">{itemTitle}</p><p className="mt-0.5 font-mono text-[10px] text-gray-400">{words(item.subject.type)} · {item.subject.id}</p><div className="mt-1 flex flex-wrap items-center gap-1"><StateBadge value={item.state} /><span className="font-mono text-[10px] text-red-600 dark:text-red-300">{blocker.code}</span></div></th><td className="px-3 py-3"><p>{duration(item.current_episode?.age_seconds)}</p><p className="text-[10px] text-gray-400">{item.threshold?.stale_hours ?? item.threshold?.threshold_hours ?? '—'}h threshold</p></td><td className="px-3 py-3">{itemOwner ?? 'Unavailable'}</td><td className="px-3 py-3"><StateBadge value={blocker.authority_state} /><p className="mt-1 max-w-[190px] truncate font-mono text-[10px] text-gray-400" title={blocker.authority_ref ?? undefined}>{shortRef(blocker.authority_ref)}</p></td><td className="max-w-[300px] px-3 py-3 text-[10px] text-gray-600 dark:text-gray-300">{blockerRemediation(blocker)}</td><td className="px-3 py-3"><button type="button" onClick={() => onOpenSubject(item.subject.type, item.subject.id, itemTitle)} className="inline-flex items-center gap-1 text-[10px] font-semibold text-red-700 dark:text-red-300"><ExternalLink className="h-3 w-3" /> Remediate</button></td></tr>)}{blockerRows.length === 0 && <tr><td colSpan={6} className="px-3 py-7 text-center text-xs text-gray-400">No canonical blocker occurrences match the current filters.</td></tr>}</tbody>
              </table>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" aria-label="Governed flow reports">
            <article className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
              <div className="flex items-start justify-between gap-2"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Rejected episode recovery</h4><p className="mt-0.5 text-[10px] text-gray-400">Rejected → In progress → Done remains edition bound.</p></div><StateBadge value={reworkAttempts.length > 0 ? 'available' : 'empty'} /></div>
              <dl className="mt-3 grid grid-cols-2 gap-2 text-[10px]"><div><dt className="text-gray-400">Episodes</dt><dd className="mt-0.5 text-lg font-bold">{reworkAttempts.length}</dd></div><div><dt className="text-gray-400">Recovered</dt><dd className="mt-0.5 text-lg font-bold">{recoveredAttempts.length}</dd></div><div><dt className="text-gray-400">Repeat rejection</dt><dd className="mt-0.5 font-semibold">{repeatRejections}</dd></div><div><dt className="text-gray-400">p50 lead time</dt><dd className="mt-0.5 font-semibold">{formatMetric(recoveryLeadP50, 'h')}</dd></div></dl>
              <div className="mt-3"><p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Cause split</p>{Object.keys(rejectionCauses).length === 0 ? <p className="mt-1 text-[10px] text-gray-400">No governed rejection causes in this period.</p> : <ul className="mt-1 space-y-1 text-[10px]">{Object.entries(rejectionCauses).map(([cause, count]) => <li key={cause} className="flex justify-between gap-2"><span>{cause}</span><span className="font-semibold">{count}</span></li>)}</ul>}</div>
            </article>
            <article className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
              <div className="flex items-start justify-between gap-2"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Dependency flow</h4><p className="mt-0.5 text-[10px] text-gray-400">Wait evidence and governed chain depth.</p></div><StateBadge value={dependencyReportCount > 0 ? 'available' : 'unavailable'} /></div>
              <dl className="mt-3 space-y-2 text-[10px]"><div className="flex justify-between"><dt className="text-gray-400">Reports</dt><dd className="font-semibold">{dependencyReportCount || 'N/A'}</dd></div><div className="flex justify-between"><dt className="text-gray-400">Wait p50</dt><dd className="font-semibold">{formatMetric(metrics?.dependencyWaitP50Hours ?? null, 'h')}</dd></div><div className="flex justify-between"><dt className="text-gray-400">Longest chain</dt><dd className="font-semibold">{formatMetric(metrics?.dependencyDepth ?? null)}</dd></div></dl>
            </article>
            <article className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
              <div className="flex items-start justify-between gap-2"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Execution Reports</h4><p className="mt-0.5 text-[10px] text-gray-400">Submission, adoption, currentness and completion outcomes.</p></div><StateBadge value={executionReportCount > 0 ? 'available' : 'unavailable'} /></div>
              <dl className="mt-3 space-y-2 text-[10px]"><div className="flex justify-between"><dt className="text-gray-400">Linked subjects</dt><dd className="font-semibold">{executionReportCount || 'N/A'}</dd></div><div className="flex justify-between"><dt className="text-gray-400">Currentness</dt><dd className="font-semibold">{executionReportCount > 0 ? 'Per subject' : 'N/A'}</dd></div><div className="flex justify-between"><dt className="text-gray-400">Details</dt><dd className="font-semibold">Expand a subject</dd></div></dl>
            </article>
            <article className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
              <div className="flex items-start justify-between gap-2"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Defect flow</h4><p className="mt-0.5 text-[10px] text-gray-400">Severity, open age, triage and regression evidence.</p></div><StateBadge value={defectReportCount > 0 ? 'available' : 'unavailable'} /></div>
              <dl className="mt-3 space-y-2 text-[10px]"><div className="flex justify-between"><dt className="text-gray-400">Reports</dt><dd className="font-semibold">{defectReportCount || 'N/A'}</dd></div><div className="flex justify-between"><dt className="text-gray-400">Open bugs</dt><dd className="font-semibold">{formatMetric(metrics?.openBugs ?? null)}</dd></div><div className="flex justify-between"><dt className="text-gray-400">High severity</dt><dd className="font-semibold">{formatMetric(metrics?.highSeverityBugs ?? null)}</dd></div></dl>
            </article>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Governed subjects</h4><p className="text-[10px] text-gray-500">{filteredItems.length} of {totalSubjects} shown</p></div><div className="flex gap-1.5"><StateBadge value={data.effective_policy.source ?? 'effective'} /><span className="rounded-full border border-gray-200 px-2 py-0.5 text-[10px] font-semibold dark:border-gray-700">{data.effective_policy.general_stale_hours}h general</span><span className="rounded-full border border-gray-200 px-2 py-0.5 text-[10px] font-semibold dark:border-gray-700">{data.effective_policy.rejected_stale_hours}h rejected</span></div></div>
            {filteredItems.length === 0 ? <div className="rounded-lg border border-dashed border-gray-300 px-4 py-8 text-center text-xs text-gray-500 dark:border-gray-600">No subjects match the current filters.</div> : filteredItems.map((item) => {
              const key = titleKey(item.subject.type, item.subject.id);
              const itemTitle = item.subject.title ?? subjectTitles[key] ?? `${words(item.subject.type)} ${item.subject.id}`;
              const open = expanded.has(key);
              const blockers = item.blockers ?? [];
              const reports = item.reports ?? null;
              const rejectedReport = item.rejected_recovery ?? reports?.rejected_recovery ?? (item.rework.length > 0 ? item.rework : null);
              const dependencyReport = item.dependency_report ?? reports?.dependency ?? null;
              const defectReport = item.defect_report ?? reports?.defect ?? null;
              const executionReport = item.execution_report ?? reports?.execution ?? null;
              return (
                <article key={key} className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700" data-testid="flow-health-row">
                  <div className="flex flex-wrap items-center gap-3 bg-gray-50 px-3 py-2 dark:bg-gray-900/30">
                    <button type="button" title={`${item.subject.type}:${item.subject.id}`} aria-label={itemTitle} aria-expanded={open} onClick={() => setExpanded((previous) => { const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next; })} className="flex min-w-0 flex-1 items-center gap-2 text-left">{open ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}<span className="min-w-0"><span className="block truncate text-xs font-semibold">{itemTitle}</span><span className="block truncate font-mono text-[10px] text-gray-400">{words(item.subject.type)} · {item.subject.id}</span></span></button>
                    <div className="flex flex-wrap items-center gap-1.5"><StateBadge value={item.state} title={item.reason_codes.map(words).join(', ')} /><StateBadge value={item.current_episode?.state ?? 'unavailable'} /><span className="text-[10px] text-gray-500">{duration(item.current_episode?.age_seconds)}</span>{blockers.length > 0 && <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300">{blockers.length} blockers</span>}{item.rework.length > 0 && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">{item.rework.length} rework</span>}<button type="button" onClick={() => onOpenSubject(item.subject.type, item.subject.id, itemTitle)} className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-[10px] font-semibold dark:border-gray-600"><ExternalLink className="h-3 w-3" /> Open</button></div>
                  </div>
                  {open && (
                    <div className="space-y-4 p-4">
                      <div className="grid gap-3 lg:grid-cols-3">
                        <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><h5 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500"><Clock3 className="h-3.5 w-3.5" /> Threshold</h5>{item.threshold ? <dl className="mt-2 space-y-1 text-[10px]"><div className="flex justify-between"><dt>State</dt><dd><StateBadge value={item.threshold.state} /></dd></div><div className="flex justify-between"><dt>Stale after</dt><dd className="font-semibold">{item.threshold.stale_hours ?? item.threshold.threshold_hours ?? (typeof item.threshold.threshold_seconds === 'number' ? Math.round(item.threshold.threshold_seconds / 3600) : '—')}h</dd></div><div className="flex justify-between gap-2"><dt>Provenance</dt><dd className="truncate font-mono" title={String(item.threshold.provenance ?? '')}>{shortRef(item.threshold.provenance)}</dd></div><div className="flex justify-between"><dt>Policy</dt><dd>v{item.threshold.policy_version ?? data.effective_policy.version}</dd></div><div className="flex justify-between gap-2"><dt>Authority</dt><dd className="truncate font-mono" title={String(item.threshold.authority_ref ?? '')}>{shortRef(item.threshold.authority_ref)}</dd></div></dl> : <p className="mt-2 text-[10px] text-gray-400">No threshold applies to this episode.</p>}</div>
                        <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><h5 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500"><History className="h-3.5 w-3.5" /> Episode provenance</h5>{item.current_episode ? <dl className="mt-2 space-y-1 text-[10px]"><div className="flex justify-between"><dt>Entered</dt><dd>{item.current_episode.entered_at ?? '—'}</dd></div><div className="flex justify-between"><dt>Age</dt><dd className="font-semibold">{duration(item.current_episode.age_seconds)}</dd></div><div className="flex justify-between gap-2"><dt>Entry event</dt><dd className="truncate font-mono" title={item.current_episode.entry_event_id ?? undefined}>{shortRef(item.current_episode.entry_event_id)}</dd></div><div className="flex justify-between gap-2"><dt>Authority</dt><dd className="truncate font-mono" title={item.current_episode.authority_ref ?? undefined}>{shortRef(item.current_episode.authority_ref)}</dd></div></dl> : <p className="mt-2 text-[10px] text-gray-400">No governed episode is available.</p>}</div>
                        <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><h5 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500"><ShieldAlert className="h-3.5 w-3.5" /> Source authority</h5>{item.source_authority ? <dl className="mt-2 space-y-1 text-[10px]">{Object.entries(item.source_authority).filter(([, value]) => ['string', 'number'].includes(typeof value)).map(([name, value]) => <div key={name} className="flex justify-between gap-2"><dt>{words(name)}</dt><dd className="truncate font-mono" title={String(value)}>{shortRef(value)}</dd></div>)}</dl> : <p className="mt-2 text-[10px] text-gray-400">Source authority unavailable.</p>}</div>
                      </div>

                      <div>
                        <h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Blockers &amp; remediation</h5>
                        {blockers.length === 0 ? <div className="mt-2 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:bg-emerald-950/25 dark:text-emerald-300">No canonical blockers.</div> : <div className="mt-2 grid gap-2 md:grid-cols-2">{blockers.map((blocker, index) => <div key={`${blocker.code}:${index}`} className="rounded-lg border border-red-200 bg-red-50/50 p-3 dark:border-red-800 dark:bg-red-950/20"><div className="flex flex-wrap items-center gap-1.5"><StateBadge value={blocker.authority_state ?? 'blocked'} /><span className="font-mono text-[10px] font-semibold text-red-800 dark:text-red-200">{blocker.code}</span>{blocker.effective_skip && <StateBadge value="skipped" />}</div>{blocker.message && <p className="mt-1 text-xs text-red-800 dark:text-red-200">{blocker.message}</p>}<p className="mt-2 text-[10px] text-gray-600 dark:text-gray-300"><span className="font-semibold">Suggested next step:</span> {blockerRemediation(blocker)}</p><div className="mt-2 flex items-center justify-between gap-2"><span className="truncate font-mono text-[10px] text-gray-400" title={String(blocker.authority_ref ?? '')}>{shortRef(blocker.authority_ref)}</span><button type="button" onClick={() => onOpenSubject(item.subject.type, item.subject.id, itemTitle)} className="inline-flex items-center gap-1 text-[10px] font-semibold text-red-700 dark:text-red-300"><ExternalLink className="h-3 w-3" /> Open remediation</button></div></div>)}</div>}
                      </div>

                      <div>
                        <h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Rejected recovery, defect flow &amp; Execution Reports</h5>
                        <div className="mt-2 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                          {[
                            ['Rejected recovery', rejectedReport],
                            ['Dependency report', dependencyReport],
                            ['Defect report', defectReport],
                            ['Execution report', executionReport],
                          ].map(([label, record]) => <div key={label as string} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><div className="flex items-center justify-between gap-2"><h6 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{label as string}</h6><StateBadge value={reportState(record as CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null)} /></div><FactPreview record={record as CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null} /></div>)}
                        </div>
                      </div>

                      <div>
                        <h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Rework timeline</h5>
                        {item.rework.length === 0 ? <p className="mt-2 text-[10px] text-gray-400">No governed rejection/rework attempts.</p> : <ol className="mt-2 space-y-2">{item.rework.map((attempt, index) => <li key={String(attempt.rejection_event_id ?? attempt.id ?? index)} className="grid gap-2 rounded-lg bg-gray-50 p-3 text-[10px] sm:grid-cols-[50px_minmax(0,1fr)_auto] dark:bg-gray-900/40"><span className="font-bold">#{String(attempt.attempt ?? index + 1)}</span><div><p className="font-semibold">{words(String(attempt.rejection_kind ?? 'rejected'))} · {words(String(attempt.rejection_code ?? 'unspecified'))}</p><p className="mt-0.5 text-gray-500">{String(attempt.rejection_summary ?? 'No rejection summary supplied.')}</p></div><div className="text-right text-gray-400"><p>{String(attempt.rejected_at ?? 'time unavailable')}</p><p>{attempt.completed_at ? 'Completed' : attempt.resumed_at ? 'Resumed' : 'Awaiting recovery'}</p></div></li>)}</ol>}
                      </div>
                    </div>
                  )}
                </article>
              );
            })}
          </div>

          <p className="text-[10px] text-gray-400">as_of {data.as_of} · query {data.query_fingerprint.slice(0, 12)}…{data.contract_version ? ` · contract ${data.contract_version}` : ''}</p>
        </div>
      )}
    </section>
  );
}
