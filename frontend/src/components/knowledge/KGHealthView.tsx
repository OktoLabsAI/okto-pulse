/**
 * KGHealthView — fullscreen overlay rendering the live KG health snapshot
 * for the active board (spec d754d004, MVP visualization-only).
 *
 * Renders KG health cards for schema/tick, queues, health, debt, and storage. Polls
 * GET /api/v1/kg/health every `pollIntervalMs` (default 30000) while the
 * tab is visible. Pauses on document.visibilityState='hidden', skips
 * overlapping fetches (BR4), aborts in-flight requests on unmount or
 * board change (BR8). Refresh button fires an immediate fetch without
 * touching the polling cadence (BR10).
 *
 * Scheduler badge — driven by backend decay_scheduler_diagnostics when
 * available; legacy last_decay_tick_at fallback remains for older payloads.
 * Schema banner — red full-width when schema_version
 * !== EXPECTED_SCHEMA_VERSION (BR2). Skeleton appears only on the very
 * first fetch (BR11). Errors preserve previous data and let polling
 * keep retrying (BR5/D9).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Brain,
  Database,
  HardDrive,
  Inbox,
  RefreshCw,
  XCircle,
} from 'lucide-react';

import { useDashboardStore } from '@/store/dashboard';
import { EXPECTED_KG_HEALTH_SCHEMA_VERSION } from '@/constants/kg';
import {
  getKGHealth,
  type KGHealth,
  type CanonicalDebtSummary,
  type DecaySchedulerDiagnostics,
  type KGLayerCounts,
  type RebuildDiagnostics,
  type StorageFootprintProxy,
} from '@/services/kg-health-api';
import { KGHealthCognitivePendingPanel } from './KGHealthCognitivePendingPanel';
import { CandidateDecisionPanel } from './CandidateDecisionPanel';
import { usePermissions } from '@/hooks/usePermissions';
import { GrafxBranding } from '@/components/shared/GrafxBranding';
import { ReadinessHelp } from './ReadinessHelp';
import { KGHealthOverview, KGHealthSectionHeading } from './KGHealthOverview';

interface KGHealthViewProps {
  pollIntervalMs?: number;
  onClose: () => void;
}

const DEFAULT_POLL_INTERVAL_MS = 30000;
const LEGACY_STALE_TICK_THRESHOLD_MS = 24 * 60 * 60 * 1000;

export function KGHealthView({
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  onClose,
}: KGHealthViewProps) {
  const currentBoard = useDashboardStore((s) => s.currentBoard);
  const boardId = currentBoard?.id ?? null;
  const permissions = usePermissions(boardId);
  const policyReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canReadHealth = policyReady && permissions.has('kg.operations.health.read');
  const canReadCognitive = policyReady && permissions.has('kg.operations.cognitive.read');

  const [data, setData] = useState<KGHealth | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [lastFetchAt, setLastFetchAt] = useState<Date | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef<boolean>(false);

  const tick = useCallback(async () => {
    if (!boardId) return;
    if (!canReadHealth) return;
    if (inFlightRef.current) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;

    inFlightRef.current = true;
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    try {
      const fresh = await getKGHealth(boardId, abortRef.current.signal);
      setData(fresh);
      setError(null);
      setLastFetchAt(new Date());
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      setError(err as Error);
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, [boardId, canReadHealth]);

  useEffect(() => {
    if (!boardId) {
      setLoading(false);
      return;
    }
    if (permissions.isLoading) return;
    if (!canReadHealth) {
      setLoading(false);
      setData(null);
      setError(new Error('You do not have permission to read KG health'));
      return;
    }
    setLoading(true);
    setData(null);
    setError(null);
    tick();
    intervalRef.current = setInterval(tick, pollIntervalMs);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      abortRef.current?.abort();
      intervalRef.current = null;
      inFlightRef.current = false;
    };
  }, [boardId, pollIntervalMs, tick, canReadHealth, permissions.isLoading]);

  useEffect(() => {
    if (!boardId) return;
    const onVis = () => {
      if (document.visibilityState === 'visible') {
        tick();
      }
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [boardId, tick]);

  const handleRefresh = useCallback(() => {
    void tick();
  }, [tick]);

  const handleRetry = useCallback(() => {
    setError(null);
    void tick();
  }, [tick]);

  const tickInfo = useMemo(
    () => computeTickInfo(
      data?.decay_scheduler_diagnostics ?? null,
      data?.last_decay_tick_at ?? null,
    ),
    [data?.decay_scheduler_diagnostics, data?.last_decay_tick_at],
  );

  const schemaMismatch = data
    && data.health_schema_version !== EXPECTED_KG_HEALTH_SCHEMA_VERSION;

  if (!boardId) {
    return <EmptyState onClose={onClose} />;
  }

  if (!permissions.isLoading && !canReadHealth) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3" data-testid="kg-health-permission-denied">
        <p className="text-sm text-rose-600">You do not have permission to read KG health.</p>
        <button type="button" onClick={onClose} className="btn btn-secondary text-sm">Close</button>
      </div>
    );
  }

  return (
    <div
      className="flex min-h-0 flex-col h-full bg-slate-50 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
      data-testid="kg-health-view"
    >
      <HeaderBar
        boardName={currentBoard?.name ?? ''}
        pollIntervalMs={pollIntervalMs}
        lastFetchAt={lastFetchAt}
        onRefresh={handleRefresh}
        onClose={onClose}
        canReadCognitive={canReadCognitive}
        grafxActive={data?.graph_storage?.board?.backend === 'grafx'}
      />
      <div className="min-h-0 flex-1 overflow-auto" data-testid="kg-health-scroll-content">
        <div className="mx-auto max-w-[1600px] space-y-6 px-4 py-5 sm:px-6 sm:py-6">
        {schemaMismatch && (
          <SchemaBanner
            expected={EXPECTED_KG_HEALTH_SCHEMA_VERSION}
            received={data!.health_schema_version}
          />
        )}

        {error && !data && (
          <ErrorPanel message={error.message} onRetry={handleRetry} />
        )}

        {loading && !data && !error && <SkeletonGrid />}

        {data && (
          <>
            {error && <InlineErrorBanner message={error.message} />}
            <KGHealthOverview health={data} stale={Boolean(error)} />
            <nav aria-label="KG Health sections" className="sticky top-0 z-20 flex flex-wrap gap-1 rounded-xl border border-slate-200 bg-white/95 p-1.5 shadow-sm backdrop-blur dark:border-slate-800 dark:bg-slate-900/95">
              {[
                ['overview', 'Overview'], ['processing', 'Processing & knowledge'],
                ['diagnostics', 'Diagnostics'],
              ].map(([id, label]) => (
                <a key={id} href={`#kg-health-${id}`} className="rounded-lg px-3 py-2 text-sm font-medium text-slate-600 hover:bg-sky-50 hover:text-sky-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-sky-300">{label}</a>
              ))}
            </nav>
            <section id="kg-health-processing" aria-labelledby="kg-health-processing-title" className="scroll-mt-28 space-y-4">
              <KGHealthSectionHeading id="kg-health-processing-title" eyebrow="01 · Knowledge work" title="Processing & pending work"
                description="Track work waiting to reach the graph. These queues describe processing, not database integrity, and their counts may overlap." />
              <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
                <QueueDeadLetterCard
                  queueDepth={data.queue_depth}
                  oldestPendingAgeS={data.oldest_pending_age_s}
                  deadLetterCount={data.dead_letter_count}
                  globalOutboxDeadLetterCount={
                    data.operational_domains?.global_outbox_dead_letter?.count
                    ?? data.global_outbox_dead_letter_count
                    ?? 0
                  }
                />
                <CanonicalDebtCard summary={data.canonical_debt ?? null} layerCounts={data.kg_layer_counts ?? null} diagnostics={data.rebuild_diagnostics ?? null} />
              </div>
              {canReadCognitive && (
                <>
                  <KGHealthCognitivePendingPanel
                    boardId={boardId}
                    selectedKgGenerationId={data.current_kg_generation_id ?? null}
                    pollIntervalMs={pollIntervalMs}
                  />
                  <CandidateDecisionPanel boardId={boardId} />
                </>
              )}
            </section>
            <section id="kg-health-diagnostics" aria-labelledby="kg-health-diagnostics-title" className="scroll-mt-28">
              <KGHealthSectionHeading id="kg-health-diagnostics-title" eyebrow="02 · Observations" title="Diagnostics"
                description="Inspect integrity signals, storage usage and relevance scheduling. Refresh reloads the reported observations." />
              <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
                <div className="min-w-0 space-y-4">
                  <KGHealthCard
                    totalNodes={data.total_nodes}
                    defaultScoreCount={data.default_score_count}
                    defaultScoreRatio={data.default_score_ratio}
                    avgRelevance={data.avg_relevance}
                    contradictWarnCount={data.contradict_warn_count}
                    metricStatus={data.metric_status ?? null}
                    healthIssues={data.health_issues ?? []}
                  />
                  <StorageFootprintCard proxy={data.storage_footprint_proxy ?? null} />
                </div>
              <SchemaTickCard
                schemaVersion={data.schema_version}
                healthSchemaVersion={data.health_schema_version ?? data.schema_version}
                graphSchemaVersion={data.graph_schema_version ?? null}
                schemaMismatch={Boolean(schemaMismatch)}
                tickInfo={tickInfo}
                schedulerDiagnostics={data.decay_scheduler_diagnostics ?? null}
                lastTickStatus={data.last_tick_status ?? null}
                lastTickError={data.last_tick_error ?? null}
                nodesRecomputed={data.nodes_recomputed_in_last_tick}
              />
              </div>
            </section>
          </>
        )}
        </div>
      </div>
    </div>
  );
}

interface TickInfo {
  status: 'never' | 'stale' | 'fresh' | 'failed' | 'running' | 'unknown';
  ageHours: number | null;
  label: string;
  ariaLabel: string;
  reason: string | null;
  nextScheduledAt: string | null;
  staleToleranceSeconds: number | null;
  source: 'backend' | 'legacy';
}

function computeTickInfo(
  diagnostics: DecaySchedulerDiagnostics | null,
  lastDecayTickAt: string | null,
): TickInfo {
  if (diagnostics) {
    const lastSuccessAge = ageHoursFromIso(diagnostics.last_success_at);
    const status = normalizeSchedulerStatus(diagnostics.status);
    const label = schedulerLabel(diagnostics, lastSuccessAge);
    return {
      status,
      ageHours: lastSuccessAge,
      label,
      ariaLabel: label,
      reason: diagnostics.reason,
      nextScheduledAt: diagnostics.next_scheduled_at,
      staleToleranceSeconds: diagnostics.stale_tolerance_seconds,
      source: 'backend',
    };
  }
  if (!lastDecayTickAt) {
    return {
      status: 'never',
      ageHours: null,
      label: 'Tick has never run',
      ariaLabel: 'Tick has never run',
      reason: 'legacy_no_tick',
      nextScheduledAt: null,
      staleToleranceSeconds: null,
      source: 'legacy',
    };
  }
  const tickDate = new Date(lastDecayTickAt);
  const ageMs = Date.now() - tickDate.getTime();
  const ageHours = Math.floor(ageMs / (60 * 60 * 1000));
  if (ageMs > LEGACY_STALE_TICK_THRESHOLD_MS) {
    return {
      status: 'stale',
      ageHours,
      label: `Stale tick: ${ageHours}h ago`,
      ariaLabel: `Stale tick: ${ageHours} hours ago`,
      reason: 'legacy_stale_threshold',
      nextScheduledAt: null,
      staleToleranceSeconds: 24 * 60 * 60,
      source: 'legacy',
    };
  }
  return {
    status: 'fresh',
    ageHours,
    label: `Last tick: ${ageHours}h ago`,
    ariaLabel: `Last tick: ${ageHours} hours ago`,
    reason: 'legacy_recent_tick',
    nextScheduledAt: null,
    staleToleranceSeconds: 24 * 60 * 60,
    source: 'legacy',
  };
}

function normalizeSchedulerStatus(status: string): TickInfo['status'] {
  if (status === 'ok') return 'fresh';
  if (status === 'never_run') return 'never';
  if (
    status === 'stale' ||
    status === 'failed' ||
    status === 'running' ||
    status === 'unknown'
  ) {
    return status;
  }
  return 'unknown';
}

function ageHoursFromIso(value: string | null): number | null {
  if (!value) return null;
  const ts = new Date(value).getTime();
  if (!Number.isFinite(ts)) return null;
  return Math.max(0, Math.floor((Date.now() - ts) / (60 * 60 * 1000)));
}

function schedulerLabel(
  diagnostics: DecaySchedulerDiagnostics,
  lastSuccessAge: number | null,
): string {
  const age = lastSuccessAge === null ? null : `${lastSuccessAge}h ago`;
  if (diagnostics.status === 'ok') {
    return age ? `Last success: ${age}` : 'Scheduler ok';
  }
  if (diagnostics.status === 'never_run') {
    return 'Scheduler has never run';
  }
  if (diagnostics.status === 'stale') {
    return age ? `Scheduler stale: ${age}` : 'Scheduler stale';
  }
  if (diagnostics.status === 'failed') {
    return 'Scheduler failed';
  }
  if (diagnostics.status === 'running') {
    return 'Scheduler running';
  }
  return 'Scheduler status unknown';
}

interface HeaderBarProps {
  boardName: string;
  pollIntervalMs: number;
  lastFetchAt: Date | null;
  onRefresh: () => void;
  onClose: () => void;
  canReadCognitive: boolean;
  grafxActive: boolean;
}

function HeaderBar({ boardName, pollIntervalMs, lastFetchAt, onRefresh, onClose, canReadCognitive, grafxActive }: HeaderBarProps) {
  const lastFetchLabel = lastFetchAt
    ? `last fetch ${Math.max(0, Math.floor((Date.now() - lastFetchAt.getTime()) / 1000))}s ago`
    : 'fetching...';
  const intervalLabel = `Polling ${Math.round(pollIntervalMs / 1000)}s`;
  return (
    <header className="shrink-0 border-b border-slate-200 bg-white px-4 py-4 dark:border-slate-800 dark:bg-slate-900 sm:px-6">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-4">
      <div className="min-w-0 flex-1 basis-80">
          <p className="mb-1 flex items-center gap-2 text-xs font-semibold text-sky-700 dark:text-sky-400"><Activity className="h-4 w-4" aria-hidden /> Knowledge Graph · Operations</p>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-white">KG Health Dashboard</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Understand graph health, follow pending work and recover with confidence.</p>
          <p className="mt-2 flex flex-wrap items-center gap-x-2 text-xs text-slate-500 dark:text-slate-400">
            Board: {boardName} · {intervalLabel} · {lastFetchLabel}
            <ReadinessHelp label="About automatic refresh">This page refreshes observations while visible, without starting a rebuild or consolidation. Refresh does not retry failed jobs. If a refresh fails, previous data remains visible with a warning.</ReadinessHelp>
          </p>
      </div>
      <div className="flex flex-col gap-3 sm:items-end">
        {grafxActive && <GrafxBranding />}
      <div className="flex flex-wrap items-center gap-2">
        {canReadCognitive && (
          <button
            type="button"
            onClick={() =>
              window.dispatchEvent(new CustomEvent('okto:open-cognitive-action-center'))
            }
            className="px-3 py-2 text-sm font-medium bg-violet-600 hover:bg-violet-700 text-white rounded-lg flex items-center gap-1.5"
            aria-label="Open Cognitive Action Center"
            data-testid="kg-open-cognitive-action-center"
          >
            <Brain className="w-4 h-4" aria-hidden /> Cognitive Action Center
          </button>
        )}
        <button
          type="button"
          onClick={onRefresh}
          className="px-3 py-2 text-sm font-medium border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg flex items-center gap-1.5"
          aria-label="Refresh KG data now"
        >
          <RefreshCw className="w-4 h-4" aria-hidden /> Refresh
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-2 text-sm font-medium border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg flex items-center gap-1.5"
        >
          <ArrowLeft className="w-4 h-4" aria-hidden /> Back to Board
        </button>
      </div>
      </div>
      </div>
    </header>
  );
}

interface SchemaBannerProps {
  expected: string;
  received: string;
}

function SchemaBanner({ expected, received }: SchemaBannerProps) {
  return (
    <div
      className="bg-rose-50 dark:bg-rose-900/30 border-2 border-rose-300 dark:border-rose-700 rounded-lg px-4 py-3 mb-4 flex items-center gap-3"
      role="alert"
    >
      <AlertTriangle className="text-rose-600 w-5 h-5 shrink-0" aria-hidden />
      <div>
        <p className="font-semibold text-rose-900 dark:text-rose-200">Schema outdated</p>
        <p className="text-sm text-rose-700 dark:text-rose-300">
          Expected <span className="font-mono">{expected}</span>, received{' '}
          <span className="font-mono">{received}</span>. Backend restart may be required.
        </p>
      </div>
    </div>
  );
}

interface SchemaTickCardProps {
  schemaVersion: string;
  healthSchemaVersion: string;
  graphSchemaVersion: string | null;
  schemaMismatch: boolean;
  tickInfo: TickInfo;
  schedulerDiagnostics: DecaySchedulerDiagnostics | null;
  lastTickStatus: string | null;
  lastTickError: string | null;
  nodesRecomputed: number;

}

function SchemaTickCard({
  schemaVersion,
  healthSchemaVersion,
  graphSchemaVersion,
  schemaMismatch,
  tickInfo,
  schedulerDiagnostics,
  lastTickStatus,
  lastTickError,
  nodesRecomputed,
}: SchemaTickCardProps) {
  const tickClasses =
    tickInfo.status === 'never'
      ? 'bg-rose-100 dark:bg-rose-900/30 text-rose-700 dark:text-rose-300'
      : tickInfo.status === 'stale' || tickInfo.status === 'failed'
      ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300'
      : tickInfo.status === 'running'
      ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
      : tickInfo.status === 'unknown'
      ? 'bg-surface-100 dark:bg-surface-700 text-surface-700 dark:text-surface-300'
      : 'text-surface-700 dark:text-surface-300';
  const lastFailureLabel = schedulerDiagnostics?.last_failure_at
    ? formatIsoDateTime(schedulerDiagnostics.last_failure_at)
    : null;
  const nextRunLabel = tickInfo.nextScheduledAt
    ? formatIsoDateTime(tickInfo.nextScheduledAt)
    : 'unavailable';
  const toleranceLabel = tickInfo.staleToleranceSeconds
    ? formatDurationSeconds(tickInfo.staleToleranceSeconds)
    : 'unavailable';
  return (
    <Card title="Decay Scheduler" testId="kg-health-card" icon={<Database className="w-4 h-4" aria-hidden />}>
      <Row label="Schema version">
        <span
          className={`text-sm font-mono px-2 py-0.5 rounded ${
            schemaMismatch
              ? 'bg-rose-100 dark:bg-rose-900/30 text-rose-700 dark:text-rose-300'
              : 'bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300'
          }`}
        >
          {schemaVersion} {schemaMismatch ? '✕' : '✓'}
        </span>
      </Row>
      <Row label="Health schema">
        <span className="text-sm font-mono text-surface-700 dark:text-surface-300">
          {healthSchemaVersion}
        </span>
      </Row>
      <Row label="Graph schema">
        <span className="text-sm font-mono text-surface-700 dark:text-surface-300">
          {graphSchemaVersion ?? 'unavailable'}
        </span>
      </Row>
      <Row label="Last tick">
        <span
          className={`text-sm font-semibold px-2 py-0.5 rounded ${tickClasses}`}
          aria-label={tickInfo.ariaLabel}
        >
          {tickInfo.label}
        </span>
      </Row>
      <Row label="Signal type">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          operational debt only
        </span>
      </Row>
      <Row label="Next run">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {nextRunLabel}
        </span>
      </Row>
      <Row label="Stale tolerance">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {toleranceLabel}
        </span>
      </Row>
      {lastFailureLabel && (
        <Row label="Last failure">
          <span className="text-sm text-amber-700 dark:text-amber-300">
            {lastFailureLabel}
          </span>
        </Row>
      )}
      {lastTickStatus && (
        <Row label="Tick status">
          <span className={`text-sm font-semibold ${
            lastTickStatus === 'failed'
              ? 'text-rose-600 dark:text-rose-300'
              : 'text-surface-700 dark:text-surface-300'
          }`}>
            {lastTickStatus}
          </span>
        </Row>
      )}
      {lastTickError && (
        <p className="rounded bg-rose-50 dark:bg-rose-950/40 px-2 py-1 text-xs text-rose-700 dark:text-rose-300" title={lastTickError}>
          {lastTickError}
        </p>
      )}
      {tickInfo.reason && (
        <p
          className="rounded bg-surface-50 dark:bg-surface-900 px-2 py-1 text-xs text-surface-600 dark:text-surface-400"
          title={tickInfo.reason}
        >
          {formatReasonLabel(tickInfo.reason)}
        </p>
      )}
      <Row label="Nodes recomputed (last tick)">
        <span className="text-2xl font-bold text-surface-900 dark:text-white">
          {nodesRecomputed.toLocaleString()}
        </span>
      </Row>
    </Card>
  );
}

interface QueueDeadLetterCardProps {
  queueDepth: number;
  oldestPendingAgeS: number | null;
  deadLetterCount: number;
  globalOutboxDeadLetterCount: number;
}

function QueueDeadLetterCard({
  queueDepth,
  oldestPendingAgeS,
  deadLetterCount,
  globalOutboxDeadLetterCount,
}: QueueDeadLetterCardProps) {
  const dlClass =
    deadLetterCount === 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-amber-600 dark:text-amber-400';
  const globalOutboxClass =
    globalOutboxDeadLetterCount === 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-amber-600 dark:text-amber-400';
  return (
    <Card title="Queue & Dead Letter" testId="kg-health-card" icon={<Inbox className="w-4 h-4" aria-hidden />}>
      <Row label="Queue depth">
        <span className="text-2xl font-bold text-surface-900 dark:text-white">
          {queueDepth.toLocaleString()}
        </span>
      </Row>
      <Row label="Oldest pending">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {formatAgeSeconds(oldestPendingAgeS)}
        </span>
      </Row>
      <Row label="Consolidation dead letter">
        <span className={`text-2xl font-bold ${dlClass}`}>{deadLetterCount.toLocaleString()}</span>
      </Row>
      <Row label="Global outbox terminal">
        <span className={`text-2xl font-bold ${globalOutboxClass}`}>
          {globalOutboxDeadLetterCount.toLocaleString()}
        </span>
      </Row>
    </Card>
  );
}

interface KGHealthCardProps {
  totalNodes: number;
  defaultScoreCount: number;
  defaultScoreRatio: number;
  avgRelevance: number;
  contradictWarnCount: number;
  metricStatus: string | null;
  healthIssues: Array<{
    code: string;
    component: string;
    severity: string;
    reason: string;
    description?: string;
    drill_down_tool?: string | null;
    counts?: Record<string, number>;
  }>;
}

function KGHealthCard({
  totalNodes,
  defaultScoreCount,
  defaultScoreRatio,
  avgRelevance,
  contradictWarnCount,
  metricStatus,
  healthIssues,
}: KGHealthCardProps) {
  const partitionIssue = healthIssues.find((issue) => issue.code === 'canonical_partition_integrity');
  const contradictClass =
    contradictWarnCount === 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-amber-600 dark:text-amber-400';
  const ratioPct = (defaultScoreRatio * 100).toFixed(1);
  const telemetryClass =
    metricStatus === 'available'
      ? 'text-emerald-700 dark:text-emerald-400'
      : 'text-amber-700 dark:text-amber-400';
  const issueSummary = healthIssues.length === 0
    ? 'none'
    : healthIssues
      .map((issue) => `${issue.component}:${issue.reason}`)
      .join('; ');
  return (
    <>
    <Card title="KG Health" testId="kg-health-card" icon={<Activity className="w-4 h-4" aria-hidden />}>
      <Row label="Total nodes">
        <span className="text-2xl font-bold text-surface-900 dark:text-white">
          {totalNodes.toLocaleString()}
        </span>
      </Row>
      <Row label="Default score ratio">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {ratioPct}% ({defaultScoreCount.toLocaleString()} nodes)
        </span>
      </Row>
      <Row label="Avg relevance">
        <span className="text-sm font-mono text-surface-700 dark:text-surface-300">
          {avgRelevance.toFixed(3)}
        </span>
      </Row>
      <Row label="Contradict warnings">
        <span className={`text-sm font-bold ${contradictClass}`}>{contradictWarnCount.toLocaleString()}</span>
      </Row>
      <Row label="Metric telemetry">
        <span className={`text-sm font-semibold ${telemetryClass}`}>
          {metricStatus ?? 'unknown'}
        </span>
      </Row>
      <Row label="Health issues">
        <span
          className="text-xs text-right text-surface-600 dark:text-surface-400 max-w-[14rem] truncate"
          title={issueSummary}
        >
          {healthIssues.length === 0 ? 'none' : `${healthIssues.length} signal${healthIssues.length === 1 ? '' : 's'}`}
        </span>
      </Row>
      {partitionIssue && (
        <Row label="Canonical partition integrity">
          <span className="text-sm font-semibold text-amber-700 dark:text-amber-400" title={partitionIssue.description}>
            {partitionIssue.counts
              ? `${Object.values(partitionIssue.counts).reduce((a, b) => a + b, 0)} signals`
              : 'Attention required'}
          </span>
        </Row>
      )}
    </Card>
    </>
  );
}

interface CanonicalDebtCardProps {
  summary: CanonicalDebtSummary | null;
  layerCounts: KGLayerCounts | null;
  diagnostics: RebuildDiagnostics | null;
}

function CanonicalDebtCard({
  summary,
  layerCounts,
  diagnostics,
}: CanonicalDebtCardProps) {
  const debtAvailable = summary != null
    && (summary.status == null || summary.status === 'available' || summary.status === 'ok');
  const openCount = debtAvailable ? summary.open_count : null;
  const layersAvailable = layerCounts?.status === 'ok' || layerCounts?.status === 'available';
  const canonicalCount = layersAvailable ? layerCounts?.by_layer?.canonical : null;
  const workingCount = layersAvailable ? layerCounts?.by_layer?.working : null;
  const retryable = debtAvailable ? summary.retryable_count : null;
  const blocked = debtAvailable ? summary.blocked_count : null;
  const debtClass =
    openCount == null
      ? 'text-surface-600 dark:text-surface-400'
      : openCount === 0
      ? 'text-emerald-700 dark:text-emerald-400'
      : blocked != null && blocked > 0
      ? 'text-rose-700 dark:text-rose-400'
      : 'text-amber-700 dark:text-amber-400';
  const outcome = openCount == null ? 'unavailable' : diagnostics?.last_outcome ?? 'unknown';
  return (
    <Card
      title="Canonical Debt"
      testId="kg-health-card"
      icon={<Database className="w-4 h-4" aria-hidden />}
    >
      <Row label="Open debt">
        <span className={`text-2xl font-bold ${debtClass}`}>
          {openCount == null ? 'Unavailable' : openCount.toLocaleString()}
        </span>
      </Row>
      <Row label="Retryable / blocked">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {retryable == null ? 'Unavailable' : retryable.toLocaleString()} / {blocked == null ? 'Unavailable' : blocked.toLocaleString()}
        </span>
      </Row>
      <Row label="Graph layers">
        <span className="text-xs text-right text-surface-600 dark:text-surface-400">
          canonical {canonicalCount == null ? 'unavailable' : canonicalCount.toLocaleString()} · working {workingCount == null ? 'unavailable' : workingCount.toLocaleString()}
        </span>
      </Row>
      <Row label="Layer status">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {layerCounts?.status ?? 'unavailable'}
        </span>
      </Row>
      <Row label="Rebuild outcome">
        <span
          className="text-xs text-right text-surface-600 dark:text-surface-400 max-w-[14rem] truncate"
          title={outcome}
        >
          {formatActionLabel(outcome)}
        </span>
      </Row>
    </Card>
  );
}

interface StorageFootprintCardProps {
  proxy: StorageFootprintProxy | null;
}

function StorageFootprintCard({ proxy }: StorageFootprintCardProps) {
  const pct = proxy?.percentage ?? proxy?.high_water_mark_pct ?? null;
  const notApplicable = proxy?.status === 'available' && proxy.percentage_status === 'not_applicable';
  const pctLabel = typeof pct === 'number' ? `${pct.toFixed(1)}%` : notApplicable ? 'Not applicable' : 'unavailable';
  const status = proxy?.status ?? 'unavailable';
  const totalBytes = proxy?.total_bytes ?? null;
  const maxBytes = proxy?.configured_max_db_size_bytes ?? null;
  const bytesLabel = totalBytes !== null && maxBytes !== null
    ? `${formatBytes(totalBytes)} / ${formatBytes(maxBytes)}`
    : totalBytes !== null
    ? formatBytes(totalBytes)
    : 'file size unavailable';
  const tone =
    typeof pct === 'number' && pct >= 80
      ? 'bg-amber-500'
      : 'bg-emerald-500';
  return (
    <Card
      title="Storage Footprint Proxy"
      testId="kg-health-card"
      icon={<HardDrive className="w-4 h-4" aria-hidden />}
    >
      <Row label="Source">
        <span className="text-sm font-mono text-surface-700 dark:text-surface-300">
          {proxy?.source ?? 'file_size_proxy'}
        </span>
      </Row>
      <Row label="Status">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {status}
        </span>
      </Row>
      <Row label="Footprint">
        <span className="text-2xl font-bold text-surface-900 dark:text-white">
          {pctLabel}
        </span>
      </Row>
      {typeof pct === 'number' && <div className="h-2 rounded-full bg-surface-200 dark:bg-surface-700 overflow-hidden" aria-hidden>
        <div
          className={`h-full rounded-full ${tone}`}
          style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
        />
      </div>}
      {notApplicable && <p className="text-xs text-surface-600 dark:text-surface-400">No storage limit configured. File size is available; a capacity percentage does not apply.</p>}
      <Row label="Files">
        <span className="text-xs text-surface-600 dark:text-surface-400">
          {bytesLabel}
        </span>
      </Row>
      <p
        className="rounded bg-surface-50 dark:bg-surface-900 px-2 py-1 text-xs text-surface-600 dark:text-surface-400"
        title={proxy?.tooltip ?? 'On-disk file-size proxy used as an early warning signal.'}
      >
        On-disk file-size proxy. It is not runtime memory telemetry.
      </p>
      {proxy?.unavailable_reason && (
        <p className="text-[11px] text-amber-700 dark:text-amber-300">
          {formatReasonLabel(proxy.unavailable_reason)}
        </p>
      )}
    </Card>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
      {[0, 1, 2, 3, 4].map((i) => (
        <div
          key={i}
          data-testid="skeleton-card"
          className="bg-white dark:bg-surface-800 rounded-xl border border-surface-200 dark:border-surface-700 p-5 h-40 animate-pulse"
          aria-hidden
        />
      ))}
    </div>
  );
}

interface ErrorPanelProps {
  message: string;
  onRetry: () => void;
}

function ErrorPanel({ message, onRetry }: ErrorPanelProps) {
  return (
    <div className="max-w-2xl mx-auto bg-white dark:bg-surface-800 rounded-xl border border-rose-200 dark:border-rose-800 p-6 text-center">
      <XCircle className="text-rose-500 w-12 h-12 mx-auto mb-2" aria-hidden />
      <h3 className="text-lg font-semibold text-surface-900 dark:text-white mb-1">
        Failed to load KG health
      </h3>
      <p className="text-sm text-surface-600 dark:text-surface-400 mb-4 font-mono">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm"
      >
        Try again
      </button>
      <p className="text-xs text-surface-500 mt-3">
        Polling will keep retrying in the background.
      </p>
    </div>
  );
}

interface InlineErrorBannerProps {
  message: string;
}

function InlineErrorBanner({ message }: InlineErrorBannerProps) {
  return (
    <div
      className="bg-amber-50 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-700 rounded-lg px-4 py-2 mb-4 text-sm text-amber-800 dark:text-amber-200 flex items-center gap-2"
      role="status"
    >
      <AlertTriangle className="w-4 h-4 shrink-0" aria-hidden />
      Latest update failed: <span className="font-mono">{message}</span>. Showing previous data.
    </div>
  );
}

interface EmptyStateProps {
  onClose: () => void;
}

function EmptyState({ onClose }: EmptyStateProps) {
  return (
    <div
      className="flex flex-col h-full bg-surface-50 dark:bg-surface-950"
      data-testid="kg-health-view"
    >
      <div className="flex items-center justify-between border-b border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 px-6 py-3 shrink-0">
        <div className="flex items-center gap-3">
          <Activity className="text-emerald-500" aria-hidden />
          <h1 className="text-lg font-bold text-surface-900 dark:text-white">KG Health Dashboard</h1>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-sm bg-surface-200 dark:bg-surface-700 hover:bg-surface-300 dark:hover:bg-surface-600 text-surface-900 dark:text-white rounded-lg flex items-center gap-1.5"
        >
          <ArrowLeft className="w-4 h-4" aria-hidden /> Back to Board
        </button>
      </div>
      <div className="flex-1 flex items-center justify-center">
        <p className="text-lg text-surface-600 dark:text-surface-400 text-center max-w-md">
          Select a board to view KG health
        </p>
      </div>
    </div>
  );
}

interface CardProps {
  title: string;
  testId?: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}

const CARD_GUIDANCE: Record<string, { description: string; help: string }> = {
  'Decay Scheduler': {
    description: 'Keep relevance scores current.',
    help: 'The internal scheduler recomputes relevance using the configured decay policy. These observations describe its last reported execution and next scheduled run. Reading health does not start a tick.',
  },
  'Queue & Dead Letter': {
    description: 'Follow queued work and failures that need review.',
    help: 'Consolidation and global outbox dead letters are different queues. Counts describe outstanding failures and may overlap other processing signals. Refreshing this dashboard does not retry any item.',
  },
  'KG Health': {
    description: 'Read integrity signals and graph telemetry.',
    help: 'These are backend observations, not inferred health. An unavailable metric is not zero. Inspect reported issues before deciding whether maintenance or recovery is needed.',
  },
  'Canonical Debt': {
    description: 'Identify updates still waiting for canonical materialization.',
    help: 'Canonical debt tracks materialization obligations. It is separate from cognitive pending work and may refer to the same sources. The counts describe outstanding obligations; reading health does not settle them.',
  },
  'Storage Footprint Proxy': {
    description: 'Monitor disk usage, not process memory.',
    help: 'The footprint measures storage files, not RAM. When no finite quota is configured, a usage percentage is not applicable; that alone does not make the byte measurement unavailable.',
  },
};

function Card({ title, testId, icon, children }: CardProps) {
  const guidance = CARD_GUIDANCE[title];
  return (
    <section
      className="min-w-0 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900"
      data-testid={testId}
    >
      <div className="mb-5 border-b border-slate-100 pb-4 dark:border-slate-800">
        <div className="flex items-center gap-2">
          <span className="rounded-lg bg-sky-50 p-2 text-sky-700 dark:bg-sky-500/10 dark:text-sky-400">{icon}</span>
          <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h3>
          {guidance && <ReadinessHelp label={`About ${title}`}>{guidance.help}</ReadinessHelp>}
        </div>
        {guidance && <p className="mt-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">{guidance.description}</p>}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

interface RowProps {
  label: string;
  children: React.ReactNode;
}

function Row({ label, children }: RowProps) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <span className="text-xs text-slate-500 dark:text-slate-400">{label}</span>
      {children}
    </div>
  );
}

function formatAgeSeconds(seconds: number | null): string {
  if (seconds === null) return '—';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function formatDurationSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function formatIsoDateTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return date.toLocaleString();
}

function formatActionLabel(value: string): string {
  return value
    .replace(/^operator_action:/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatReasonLabel(value: string): string {
  return value
    .replace(/[:_]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}
