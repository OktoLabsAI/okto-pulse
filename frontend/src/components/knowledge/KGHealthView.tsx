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
  Loader2,
  Play,
  RefreshCw,
  SlidersHorizontal,
  XCircle,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { useDashboardStore } from '@/store/dashboard';
import { EXPECTED_KG_HEALTH_SCHEMA_VERSION } from '@/constants/kg';
import {
  getKGCognitivePendingItems,
  getKGHealth,
  runRebuildConfirm,
  runRebuildPreflight,
  runRebuildRun,
  type KGHealth,
  type KGCognitivePendingCounts,
  type CanonicalDebtSummary,
  type DecaySchedulerDiagnostics,
  type KGLayerCounts,
  type RebuildPreflightResult,
  type RebuildDiagnostics,
  type RebuildRunResult,
  type StorageFootprintProxy,
} from '@/services/kg-health-api';
import { triggerKGTick } from '@/services/kg-tick-api';
import { KGHealthCognitivePendingPanel } from './KGHealthCognitivePendingPanel';
import { CandidateDecisionPanel } from './CandidateDecisionPanel';

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

  const [data, setData] = useState<KGHealth | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [lastFetchAt, setLastFetchAt] = useState<Date | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef<boolean>(false);

  const tick = useCallback(async () => {
    if (!boardId) return;
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
  }, [boardId]);

  useEffect(() => {
    if (!boardId) {
      setLoading(false);
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
  }, [boardId, pollIntervalMs, tick]);

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

  const handleOpenDecayTickSettings = useCallback(() => {
    window.dispatchEvent(new CustomEvent('okto:open-runtime-settings', {
      detail: { initialTab: 'decaytick' },
    }));
    onClose();
  }, [onClose]);

  const tickInfo = useMemo(
    () => computeTickInfo(
      data?.decay_scheduler_diagnostics ?? null,
      data?.last_decay_tick_at ?? null,
    ),
    [data?.decay_scheduler_diagnostics, data?.last_decay_tick_at],
  );

  const schemaMismatch = data && data.schema_version !== EXPECTED_KG_HEALTH_SCHEMA_VERSION;

  if (!boardId) {
    return <EmptyState onClose={onClose} />;
  }

  return (
    <div
      className="flex flex-col h-full bg-surface-50 dark:bg-surface-950"
      data-testid="kg-health-view"
    >
      <HeaderBar
        boardName={currentBoard?.name ?? ''}
        pollIntervalMs={pollIntervalMs}
        lastFetchAt={lastFetchAt}
        onRefresh={handleRefresh}
        onClose={onClose}
      />
      <div className="flex-1 overflow-auto p-6">
        {schemaMismatch && (
          <SchemaBanner
            expected={EXPECTED_KG_HEALTH_SCHEMA_VERSION}
            received={data!.schema_version}
          />
        )}

        {error && !data && (
          <ErrorPanel message={error.message} onRetry={handleRetry} />
        )}

        {loading && !data && !error && <SkeletonGrid />}

        {data && (
          <>
            {error && <InlineErrorBanner message={error.message} />}
            <RecoveryPanel
              boardId={boardId}
              graphState={data.graph_state ?? null}
              discoveryState={data.discovery_state ?? null}
              overallState={data.overall_state ?? null}
              currentGenerationId={data.current_kg_generation_id ?? null}
              classificationReason={data.classification_reason ?? null}
              totalNodes={data.total_nodes}
              pollIntervalMs={pollIntervalMs}
              onCompleted={handleRefresh}
            />
            <KGHealthCognitivePendingPanel
              boardId={boardId}
              selectedKgGenerationId={data.current_kg_generation_id ?? null}
              pollIntervalMs={pollIntervalMs}
            />
            <CandidateDecisionPanel boardId={boardId} />
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
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
                boardId={boardId}
                onTickStarted={handleRefresh}
                onOpenDecayTickSettings={handleOpenDecayTickSettings}
                tickInProgress={data.tick_in_progress ?? false}
              />
              <QueueDeadLetterCard
                queueDepth={data.queue_depth}
                oldestPendingAgeS={data.oldest_pending_age_s}
                deadLetterCount={data.dead_letter_count}
              />
              <KGHealthCard
                totalNodes={data.total_nodes}
                defaultScoreCount={data.default_score_count}
                defaultScoreRatio={data.default_score_ratio}
                avgRelevance={data.avg_relevance}
                contradictWarnCount={data.contradict_warn_count}
                metricStatus={data.metric_status ?? null}
                healthIssues={data.health_issues ?? []}
              />
              <CanonicalDebtCard
                summary={data.canonical_debt ?? null}
                layerCounts={data.kg_layer_counts ?? null}
                diagnostics={data.rebuild_diagnostics ?? null}
              />
              <StorageFootprintCard
                proxy={data.storage_footprint_proxy ?? null}
              />
            </div>
          </>
        )}
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
  recommendedAction: string | null;
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
      recommendedAction: diagnostics.recommended_action,
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
      recommendedAction: null,
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
      recommendedAction: null,
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
    recommendedAction: null,
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
}

function HeaderBar({ boardName, pollIntervalMs, lastFetchAt, onRefresh, onClose }: HeaderBarProps) {
  const lastFetchLabel = lastFetchAt
    ? `last fetch ${Math.max(0, Math.floor((Date.now() - lastFetchAt.getTime()) / 1000))}s ago`
    : 'fetching...';
  const intervalLabel = `Polling ${Math.round(pollIntervalMs / 1000)}s`;
  return (
    <div className="flex items-center justify-between border-b border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 px-6 py-3 shrink-0">
      <div className="flex items-center gap-3">
        <Activity className="text-emerald-500" aria-hidden />
        <div>
          <h1 className="text-lg font-bold text-surface-900 dark:text-white">KG Health Dashboard</h1>
          <p className="text-xs text-surface-500 dark:text-surface-400">
            Board: {boardName} · {intervalLabel} · {lastFetchLabel}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() =>
            window.dispatchEvent(new CustomEvent('okto:open-cognitive-action-center'))
          }
          className="px-3 py-1.5 text-sm bg-violet-600 hover:bg-violet-700 text-white rounded-lg flex items-center gap-1.5"
          aria-label="Open Cognitive Action Center"
          data-testid="kg-open-cognitive-action-center"
        >
          <Brain className="w-4 h-4" aria-hidden /> Cognitive Action Center
        </button>
        <button
          type="button"
          onClick={onRefresh}
          className="px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded-lg flex items-center gap-1.5"
          aria-label="Refresh KG data now"
        >
          <RefreshCw className="w-4 h-4" aria-hidden /> Refresh
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-sm bg-surface-200 dark:bg-surface-700 hover:bg-surface-300 dark:hover:bg-surface-600 text-surface-900 dark:text-white rounded-lg flex items-center gap-1.5"
        >
          <ArrowLeft className="w-4 h-4" aria-hidden /> Back to Board
        </button>
      </div>
    </div>
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
  /** Spec 54399628 — board scope para `triggerKGTick`. */
  boardId: string;
  /** Callback chamado após tick disparar com sucesso (para refresh natural). */
  onTickStarted: () => void;
  onOpenDecayTickSettings: () => void;
  /** Bug fix — true quando o advisory lock global ``kg_daily_tick`` está
   *  acquired no backend. Vem de KGHealth.tick_in_progress, atualizado a
   *  cada poll (30s). Garante que o botão fica desabilitado mesmo se o
   *  usuário fechar o modal e voltar — ou se outra origem (cron/MCP)
   *  estiver rodando o tick agora. */
  tickInProgress: boolean;
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
  boardId,
  onTickStarted,
  onOpenDecayTickSettings,
  tickInProgress,
}: SchemaTickCardProps) {
  // Spec 54399628 (Wave 2 NC f9732afc) — botão "Run tick now" com 4 estados:
  // idle / running / success (toast + handleRefresh) / error (toast).
  // Usamos `running` como estado local; idle é o default. Success/error são
  // transições efêmeras representadas por toasts; o botão volta para idle
  // após o callback async resolver.
  const [tickRunning, setTickRunning] = useState(false);

  // Bug fix (Playwright E2E reproduzido):
  //
  // 1. `useRef` síncrono garante que cliques no mesmo macro-tick (antes do
  //    React re-render) sejam bloqueados — o guard via state-only falhava
  //    em rajadas de 10ms entre cliques.
  // 2. A unlock do ref é DEFERRED por 3s mesmo após o `triggerKGTick`
  //    resolver: o endpoint retorna 202 quase imediatamente, então sem o
  //    cooldown o ref liberaria antes do próximo click humano (>100ms).
  //    3s cobre o gap até o próximo poll do health (que verá
  //    `tick_in_progress=true` via advisory lock e mantém o botão disabled).
  // 3. `tickInProgress` vindo do health é a defesa cross-mount/cross-tab
  //    (avaliado no botão `disabled` + entry guard).
  const inFlightRef = useRef(false);

  const handleRunTickNow = useCallback(async () => {
    if (inFlightRef.current || tickRunning || tickInProgress) return;
    inFlightRef.current = true;
    setTickRunning(true);
    // Cooldown lock — mantém o guard por 3s além do fetch para cobrir o
    // gap entre o 202 e o próximo poll do health.
    setTimeout(() => { inFlightRef.current = false; }, 3000);
    try {
      await triggerKGTick(boardId);
      toast.success('Tick started — graph will update on next poll');
      onTickStarted();
    } catch (err: any) {
      if (err?.code === 'tick_already_running') {
        toast(
          'Tick already running, retry shortly',
          { icon: '⚠️' },
        );
      } else {
        toast.error(err?.message ?? 'Failed to start tick');
      }
    } finally {
      setTickRunning(false);
    }
  }, [boardId, onTickStarted, tickRunning, tickInProgress]);

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
  const recommendedAction = tickInfo.recommendedAction
    ? formatActionLabel(tickInfo.recommendedAction)
    : tickInfo.source === 'legacy'
    ? 'Legacy tick fields only'
    : 'Inspect scheduler';
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
      <Row label="Recommended action">
        <span className="text-sm font-medium text-blue-700 dark:text-blue-300">
          {recommendedAction}
        </span>
      </Row>
      <div className="rounded bg-blue-50 dark:bg-blue-950/40 px-3 py-2 text-xs text-blue-900 dark:text-blue-200">
        <div className="font-semibold">Cadence is edited in Settings.</div>
        <div className="mt-1 text-blue-800 dark:text-blue-300">
          KG Health reflects the active tolerance and next run from backend diagnostics; use Runtime Settings &gt; Decay Tick to change interval, staleness threshold, or max-age cap.
        </div>
      </div>
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
      <div className="pt-2 mt-2 border-t border-surface-200 dark:border-surface-700">
        <button
          type="button"
          onClick={handleRunTickNow}
          disabled={tickRunning || tickInProgress}
          className={`w-full inline-flex items-center justify-center gap-2 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
            tickRunning || tickInProgress
              ? 'bg-surface-200 dark:bg-surface-700 text-surface-500 dark:text-surface-400 cursor-not-allowed'
              : 'bg-blue-600 hover:bg-blue-700 text-white'
          }`}
          data-testid="kg-tick-run-now"
          aria-label="Run KG decay tick now"
          title={tickInProgress && !tickRunning ? 'Tick is already running globally (cron, MCP or another tab)' : undefined}
        >
          {tickRunning || tickInProgress ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
              {tickInProgress && !tickRunning ? 'Tick in progress…' : 'Running…'}
            </>
          ) : (
            <>
              <Play className="w-3.5 h-3.5" aria-hidden />
              Run tick now
            </>
          )}
        </button>
        <button
          type="button"
          onClick={onOpenDecayTickSettings}
          className="mt-2 w-full inline-flex items-center justify-center gap-2 px-3 py-1.5 text-xs font-medium rounded-lg border border-surface-300 dark:border-surface-600 text-surface-700 dark:text-surface-200 hover:bg-surface-50 dark:hover:bg-surface-700"
          data-testid="kg-open-decay-settings"
          aria-label="Open Runtime Settings Decay Tick tab"
        >
          <SlidersHorizontal className="w-3.5 h-3.5" aria-hidden />
          Open Decay Tick settings
        </button>
      </div>
    </Card>
  );
}

interface QueueDeadLetterCardProps {
  queueDepth: number;
  oldestPendingAgeS: number;
  deadLetterCount: number;
}

function QueueDeadLetterCard({ queueDepth, oldestPendingAgeS, deadLetterCount }: QueueDeadLetterCardProps) {
  const dlClass =
    deadLetterCount === 0
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
      <Row label="Dead letter">
        <span className={`text-2xl font-bold ${dlClass}`}>{deadLetterCount.toLocaleString()}</span>
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
    </Card>
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
  const openCount = summary?.open_count ?? 0;
  const canonicalCount = layerCounts?.by_layer?.canonical ?? 0;
  const workingCount = layerCounts?.by_layer?.working ?? 0;
  const retryable = summary?.retryable_count ?? 0;
  const blocked = summary?.blocked_count ?? 0;
  const debtClass =
    openCount === 0
      ? 'text-emerald-700 dark:text-emerald-400'
      : blocked > 0
      ? 'text-rose-700 dark:text-rose-400'
      : 'text-amber-700 dark:text-amber-400';
  const outcome = diagnostics?.last_outcome ?? 'unknown';
  return (
    <Card
      title="Canonical Debt"
      testId="kg-health-card"
      icon={<Database className="w-4 h-4" aria-hidden />}
    >
      <Row label="Open debt">
        <span className={`text-2xl font-bold ${debtClass}`}>
          {openCount.toLocaleString()}
        </span>
      </Row>
      <Row label="Retryable / blocked">
        <span className="text-sm text-surface-700 dark:text-surface-300">
          {retryable.toLocaleString()} / {blocked.toLocaleString()}
        </span>
      </Row>
      <Row label="Graph layers">
        <span className="text-xs text-right text-surface-600 dark:text-surface-400">
          canonical {canonicalCount.toLocaleString()} · working {workingCount.toLocaleString()}
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
  const pctLabel = typeof pct === 'number' ? `${pct.toFixed(1)}%` : 'unavailable';
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
      <div className="h-2 rounded-full bg-surface-200 dark:bg-surface-700 overflow-hidden" aria-hidden>
        <div
          className={`h-full rounded-full ${tone}`}
          style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }}
        />
      </div>
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

function Card({ title, testId, icon, children }: CardProps) {
  return (
    <section
      className="bg-white dark:bg-surface-800 rounded-xl border border-surface-200 dark:border-surface-700 p-5 shadow-sm"
      data-testid={testId}
    >
      <h2 className="text-sm font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mb-3 flex items-center gap-2">
        {icon}
        {title}
      </h2>
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
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-surface-500 dark:text-surface-400">{label}</span>
      {children}
    </div>
  );
}

function formatAgeSeconds(seconds: number): string {
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

// ---------------------------------------------------------------------------
// Recovery panel — KG-02 sm_a30278ad mockup
// ---------------------------------------------------------------------------
//
// Single-page flow per the mockup: preflight summary + rebuild report aside,
// inline reason input and one explicit "Confirm rebuild" button. No second
// modal — the operator already sees all the destructive-op context on the
// page (KG-02 FR3 explicit UI confirmation is satisfied by the destructive
// red button + the reason input + the preflight context above it).
//
//   POST /kg/rebuild/preflight  ──▶  preflight_hash + manifest_ref
//   POST /kg/rebuild/confirm    ──▶  confirmation_id (single-use TTL bound)
//   POST /kg/rebuild/run        ──▶  RebuildRunResult (audit_ref + report_ref
//                                    + promoted generation, KG-02.4 + .7)

interface RecoveryPanelProps {
  boardId: string;
  graphState: string | null;
  discoveryState: string | null;
  overallState: string | null;
  currentGenerationId: string | null;
  classificationReason: string | null;
  totalNodes: number;
  pollIntervalMs: number;
  onCompleted: () => void;
}

interface RecoveryStatusView {
  label: string;
  className: string;
}

function recoveryStatusView(overallState: string | null): RecoveryStatusView {
  if (overallState === 'healthy' || overallState === 'fresh') {
    return {
      label: 'Healthy',
      className:
        'rounded-full bg-emerald-100 dark:bg-emerald-900/40 px-3 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300',
    };
  }
  if (overallState === 'at_risk' || overallState === 'backpressure') {
    return {
      label: 'At risk',
      className:
        'rounded-full bg-amber-100 dark:bg-amber-900/40 px-3 py-1 text-xs font-medium text-amber-700 dark:text-amber-300',
    };
  }
  if (
    overallState === 'recovery_needed' ||
    overallState === 'quarantined' ||
    overallState === 'corrupted' ||
    overallState === 'failed'
  ) {
    return {
      label: 'Recovery needed',
      className:
        'rounded-full bg-rose-100 dark:bg-rose-900/40 px-3 py-1 text-xs font-medium text-rose-700 dark:text-rose-300',
    };
  }
  return {
    label: 'Unknown',
    className:
      'rounded-full bg-surface-100 dark:bg-surface-800 px-3 py-1 text-xs font-medium text-surface-700 dark:text-surface-300',
  };
}

interface CognitiveStateView {
  value: string;
  state: string | null;
  subtitle: string;
  reportValue: string;
  reportTone: 'success' | 'warning' | 'default';
}

function cognitiveStateView(
  currentGenerationId: string | null,
  counts: KGCognitivePendingCounts | null,
  error: string | null,
): CognitiveStateView {
  if (!currentGenerationId) {
    return {
      value: 'no generation',
      state: null,
      subtitle: 'waiting for rebuild',
      reportValue: 'not available',
      reportTone: 'default',
    };
  }
  if (error) {
    return {
      value: 'unavailable',
      state: 'at_risk',
      subtitle: 'could not load markers',
      reportValue: 'unavailable',
      reportTone: 'warning',
    };
  }
  if (!counts) {
    return {
      value: 'checking',
      state: null,
      subtitle: 'loading markers',
      reportValue: 'checking',
      reportTone: 'default',
    };
  }

  const active = counts.pending + counts.in_progress;
  if (counts.failed > 0) {
    return {
      value: `${counts.failed} failed`,
      state: 'failed',
      subtitle: `${active} pending`,
      reportValue: 'failed',
      reportTone: 'warning',
    };
  }
  if (active > 0) {
    return {
      value: `${active} pending`,
      state: 'at_risk',
      subtitle: `${counts.consolidated} consolidated`,
      reportValue: 'pending',
      reportTone: 'warning',
    };
  }
  if (counts.total === 0) {
    return {
      value: 'no pending items',
      state: 'fresh',
      subtitle: 'no markers for generation',
      reportValue: 'none',
      reportTone: 'success',
    };
  }
  return {
    value: 'consolidated after rebuild',
    state: 'fresh',
    subtitle: `${counts.consolidated + counts.skipped}/${counts.total} terminal`,
    reportValue: 'consolidated',
    reportTone: 'success',
  };
}

function stateBadgeClass(state: string | null): string {
  if (state === 'healthy' || state === 'fresh') {
    return 'text-emerald-700 dark:text-emerald-400';
  }
  if (state === 'at_risk' || state === 'recovery_needed' || state === 'empty') {
    return 'text-amber-700 dark:text-amber-400';
  }
  if (state === 'quarantined' || state === 'corrupted' || state === 'failed') {
    return 'text-rose-700 dark:text-rose-400';
  }
  return 'text-surface-600 dark:text-surface-400';
}

function shortGenerationId(value: string | null): string {
  if (!value) return '—';
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}…`;
}

function explainRecoveryState(state: string | null, reason: string | null): string {
  const reasonText = reason ? ` Reason: ${reason}.` : '';
  if (state === 'healthy' || state === 'fresh') {
    return `State is healthy because the latest health check found no blocking risk signals.${reasonText}`;
  }
  if (state === 'at_risk') {
    return `State is at_risk because the KG has a preventive warning, such as unavailable telemetry, scheduler debt, dead-letter backlog, or recent storage warnings. It is not the same as recovery_needed.${reasonText}`;
  }
  if (state === 'backpressure') {
    return `State is backpressure because writes are being throttled or an administrative lane holds the KG lock.${reasonText}`;
  }
  if (state === 'recovery_needed') {
    return `State is recovery_needed because the health classifier saw a storage degradation signal such as WAL or commit errors.${reasonText}`;
  }
  if (state === 'quarantined') {
    return `State is quarantined because a graph file has been isolated after a corruption signal.${reasonText}`;
  }
  if (state === 'corrupted' || state === 'failed') {
    return `State is ${state} because the graph could not be safely used by the current health check.${reasonText}`;
  }
  return `State is unknown because the health payload did not include a known KG state.${reasonText}`;
}

function RecoveryPanel({
  boardId,
  graphState,
  discoveryState,
  overallState,
  currentGenerationId,
  classificationReason,
  totalNodes,
  pollIntervalMs,
  onCompleted,
}: RecoveryPanelProps) {
  const [preflight, setPreflight] = useState<RebuildPreflightResult | null>(null);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [reason, setReason] = useState('');
  const [running, setRunning] = useState(false);
  const [lastResult, setLastResult] = useState<RebuildRunResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [cognitiveCounts, setCognitiveCounts] =
    useState<KGCognitivePendingCounts | null>(null);
  const [cognitiveError, setCognitiveError] = useState<string | null>(null);

  const refreshPreflight = useCallback(async () => {
    setPreflightLoading(true);
    setPreflightError(null);
    try {
      const result = await runRebuildPreflight(boardId);
      setPreflight(result);
    } catch (err) {
      setPreflightError((err as Error).message);
    } finally {
      setPreflightLoading(false);
    }
  }, [boardId]);

  useEffect(() => {
    refreshPreflight();
  }, [refreshPreflight]);

  useEffect(() => {
    if (!currentGenerationId) {
      setCognitiveCounts(null);
      setCognitiveError(null);
      return;
    }

    let cancelled = false;
    let controller: AbortController | null = null;

    const load = async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const result = await getKGCognitivePendingItems(
          boardId,
          { kgGenerationId: currentGenerationId, limit: 1 },
          controller.signal,
        );
        if (cancelled) return;
        setCognitiveCounts(result.counts);
        setCognitiveError(null);
      } catch (err) {
        if ((err as Error).name === 'AbortError') return;
        if (cancelled) return;
        setCognitiveCounts(null);
        setCognitiveError((err as Error).message);
      }
    };

    void load();
    const intervalId = setInterval(load, pollIntervalMs);
    return () => {
      cancelled = true;
      controller?.abort();
      clearInterval(intervalId);
    };
  }, [boardId, currentGenerationId, pollIntervalMs]);

  const confirmRebuild = useCallback(async () => {
    if (!preflight) return;
    if (reason.trim().length === 0) {
      setRunError('Reason is required for the audit trail.');
      return;
    }
    setRunning(true);
    setRunError(null);
    setLastResult(null);
    try {
      // Refresh preflight just before consuming so the manifest_ref is
      // current (KG-02.2 lifecycle: single-use TTL-bound confirmation).
      const fresh = await runRebuildPreflight(boardId);
      setPreflight(fresh);
      const confirmResult = await runRebuildConfirm({
        board_id: boardId,
        operation: 'rebuild',
        preflight_hash: fresh.preflight_hash,
        manifest_ref: fresh.manifest_ref,
      });
      const runResult = await runRebuildRun({
        confirmation_id: confirmResult.confirmation_id,
        board_id: boardId,
        operation: 'rebuild',
        preflight_hash: fresh.preflight_hash,
        manifest_ref: fresh.manifest_ref,
        reason: reason.trim(),
      });
      setLastResult(runResult);
      if (runResult.outcome === 'completed') {
        toast.success('Rebuild completed — new generation promoted.');
        setReason('');
      } else {
        toast.error(`Rebuild ${runResult.outcome}: ${runResult.reason}`);
      }
      onCompleted();
    } catch (err) {
      setRunError((err as Error).message);
    } finally {
      setRunning(false);
    }
  }, [preflight, reason, boardId, onCompleted]);

  const recoveryStatus = recoveryStatusView(overallState);
  const cognitiveStatus = cognitiveStateView(
    currentGenerationId,
    cognitiveCounts,
    cognitiveError,
  );
  const graphDisplayState = totalNodes === 0 ? 'empty' : graphState;
  const graphTooltip =
    totalNodes === 0
      ? `graph.lbug is the board-local LadybugDB graph for this board. The graph is empty because KG Health counted total_nodes=0 and the graph endpoint will return no nodes until the board is indexed again. ${explainRecoveryState(graphState, classificationReason)}`
      : `graph.lbug is the board-local LadybugDB graph for this board. ${explainRecoveryState(graphState, classificationReason)}`;
  const discoveryTooltip = `discovery.lbug is the global discovery LadybugDB index used for cross-board KG discovery. ${explainRecoveryState(discoveryState, classificationReason)}`;
  const generationTooltip = currentGenerationId
    ? `Current KG generation is ${currentGenerationId}. It is fresh because a UUID v4 generation is selected as the active rebuild output.`
    : 'No current KG generation is selected yet, so rebuild-derived status cannot be tied to a generation.';
  const cognitiveTooltip = `Cognitive consolidation tracks items marked during rebuild for semantic agent review. Current status: ${cognitiveStatus.reportValue}. ${cognitiveStatus.subtitle}.`;
  const legacyFallback = preflight?.has_non_deterministic_inputs ?? false;
  const eligibleCount = preflight?.eligible_source_count ?? 0;
  const canonicalCount = preflight?.canonical_source_count ?? eligibleCount;
  const workingCount = preflight?.working_source_count ?? 0;
  const skippedByMaturity = preflight?.skipped_by_maturity_count ?? 0;
  const expiredWorking = preflight?.skipped_expired_working_count ?? 0;
  const legacyUnknown = preflight?.legacy_unknown_count ?? 0;
  const skipped = preflight?.skipped_cancelled_count ?? 0;
  const reasonInvalid = reason.trim().length === 0;
  const isCompleted = lastResult?.outcome === 'completed';

  return (
    <div className="mb-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-surface-900 dark:text-white">
            KG Recovery
          </h2>
          <p className="text-xs text-surface-500 dark:text-surface-400">
            Preflight, rebuild and report with cognitive pendings (KG-02).
          </p>
        </div>
        <span className={recoveryStatus.className}>
          {recoveryStatus.label}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <RecoveryMetricCard
          label="Board graph"
          value="graph.lbug"
          state={graphDisplayState}
          subtitle={totalNodes === 0 ? '0 nodes indexed' : undefined}
          tooltip={graphTooltip}
        />
        <RecoveryMetricCard
          label="Global discovery"
          value="discovery.lbug"
          state={discoveryState}
          tooltip={discoveryTooltip}
        />
        <RecoveryMetricCard
          label="Generation"
          value={shortGenerationId(currentGenerationId)}
          state={currentGenerationId ? 'fresh' : null}
          subtitle={currentGenerationId ? 'current UUID v4' : 'no generation yet'}
          tooltip={generationTooltip}
        />
        <RecoveryMetricCard
          label="Cognitive"
          value={cognitiveStatus.value}
          state={cognitiveStatus.state}
          subtitle={cognitiveStatus.subtitle}
          tooltip={cognitiveTooltip}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1.2fr_0.8fr] gap-4">
        <section className="rounded-lg border border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800">
          <div className="border-b border-surface-200 dark:border-surface-700 px-4 py-3">
            <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
              Preflight
            </h3>
            <p className="text-[11px] text-surface-500 dark:text-surface-400">
              Read-only — manifest persisted on every run.
            </p>
          </div>
          <div className="space-y-2 px-4 py-3 text-sm">
            {preflightLoading && !preflight && (
              <div className="text-surface-500 dark:text-surface-400">
                Loading preflight…
              </div>
            )}
            {preflightError && (
              <div className="text-rose-600 dark:text-rose-400 text-xs">
                {preflightError}
              </div>
            )}
            {preflight && (
              <>
                <PreflightRow label="Canonical sources">
                  <strong>
                    {canonicalCount} eligible · {skipped} cancelled
                  </strong>
                </PreflightRow>
                <PreflightRow label="Working/debt">
                  <span className="text-xs text-right text-surface-600 dark:text-surface-400">
                    {workingCount} working · {skippedByMaturity} immature · {expiredWorking} expired
                  </span>
                </PreflightRow>
                <PreflightRow label="Legacy unknown">
                  <strong className={legacyUnknown > 0 ? 'text-amber-700' : ''}>
                    {legacyUnknown}
                  </strong>
                </PreflightRow>
                <PreflightRow label="Legacy fallback">
                  <strong className={legacyFallback ? 'text-amber-700' : ''}>
                    {legacyFallback ? 'confirmation required' : 'none'}
                  </strong>
                </PreflightRow>
                <PreflightRow label="Outcome">
                  <strong className={preflight.outcome === 'ready' ? 'text-emerald-700' : 'text-amber-700'}>
                    {preflight.outcome}
                  </strong>
                </PreflightRow>
                <PreflightRow label="Preflight hash">
                  <span className="font-mono text-[11px] text-surface-600 dark:text-surface-400">
                    {preflight.preflight_hash.slice(0, 16)}…
                  </span>
                </PreflightRow>
                <PreflightRow label="Manifest">
                  <span className="font-mono text-[11px] text-surface-600 dark:text-surface-400">
                    {preflight.manifest_ref}
                  </span>
                </PreflightRow>
              </>
            )}
          </div>
        </section>

        <aside className="rounded-lg border border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 p-4 flex flex-col">
          <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
            Rebuild report
          </h3>
          <div className="mt-3 space-y-2 text-sm flex-1">
            {!lastResult && (
              <>
                <ReportRow label="Status" value={preflight?.outcome ?? '—'} />
                <ReportRow label="Expected result" value="new UUID v4" />
                <ReportRow
                  label="Cognitive state"
                  value={cognitiveStatus.reportValue}
                  tone={cognitiveStatus.reportTone}
                />
              </>
            )}
            {lastResult && (
              <>
                <ReportRow
                  label="Outcome"
                  value={lastResult.outcome}
                  tone={isCompleted ? 'success' : 'warning'}
                />
                <ReportRow
                  label="Run id"
                  value={lastResult.run_id}
                  mono
                />
                {lastResult.current_kg_generation_id && (
                  <ReportRow
                    label="New generation"
                    value={lastResult.current_kg_generation_id}
                    mono
                  />
                )}
                {lastResult.previous_kg_generation_id && (
                  <ReportRow
                    label="Previous generation"
                    value={lastResult.previous_kg_generation_id}
                    mono
                  />
                )}
                {lastResult.report_id && (
                  <ReportRow
                    label="Report"
                    value={lastResult.report_id}
                    mono
                  />
                )}
                {lastResult.publishable_status && (
                  <ReportRow
                    label="Publishable status"
                    value={lastResult.publishable_status}
                  />
                )}
                {lastResult.promotion_outcome && (
                  <ReportRow
                    label="Promotion"
                    value={lastResult.promotion_outcome}
                    tone={
                      lastResult.promotion_outcome === 'promoted'
                        ? 'success'
                        : 'warning'
                    }
                  />
                )}
                {lastResult.operator_action && (
                  <ReportRow
                    label="Operator action"
                    value={lastResult.operator_action}
                    tone="warning"
                  />
                )}
                <ReportRow
                  label="kg.rebuilt emitted"
                  value={lastResult.event_emitted ? 'yes' : 'no'}
                  tone={lastResult.event_emitted ? 'success' : 'warning'}
                />
              </>
            )}
          </div>

          <div className="mt-4 space-y-2">
            <label
              htmlFor="rebuild-reason"
              className="text-xs text-surface-600 dark:text-surface-400 block"
            >
              Reason (audit) *
            </label>
            <textarea
              id="rebuild-reason"
              value={reason}
              onChange={(e) => {
                setReason(e.target.value);
                setRunError(null);
              }}
              rows={2}
              className="w-full rounded-md border border-surface-300 dark:border-surface-600 bg-white dark:bg-surface-900 px-3 py-2 text-sm text-surface-900 dark:text-white"
              placeholder="e.g. WAL corruption after restart"
              disabled={running}
            />
            {runError && (
              <div className="rounded-md bg-rose-50 dark:bg-rose-900/40 border border-rose-200 dark:border-rose-700 px-3 py-2 text-rose-700 dark:text-rose-300 text-xs">
                {runError}
              </div>
            )}
            <button
              type="button"
              onClick={confirmRebuild}
              disabled={preflightLoading || running || reasonInvalid || !preflight}
              className="w-full rounded-md bg-rose-600 hover:bg-rose-700 disabled:opacity-60 disabled:cursor-not-allowed px-3 py-2 text-sm font-medium text-white flex items-center justify-center gap-2"
              title={
                reasonInvalid
                  ? 'Type a reason first'
                  : 'Run destructive rebuild now'
              }
            >
              {running && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />}
              {running ? 'Running…' : 'Confirm rebuild'}
            </button>
            <p className="text-[11px] text-surface-500 dark:text-surface-400 text-center">
              Destructive — promotes a new UUID v4 generation.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

function PreflightRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-surface-600 dark:text-surface-400">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

interface ReportRowProps {
  label: string;
  value: string;
  mono?: boolean;
  tone?: 'success' | 'warning' | 'default';
}

function ReportRow({ label, value, mono, tone = 'default' }: ReportRowProps) {
  const toneClass =
    tone === 'success'
      ? 'bg-emerald-50 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-200'
      : tone === 'warning'
      ? 'bg-amber-50 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200'
      : 'bg-surface-50 dark:bg-surface-900';
  return (
    <div className={`flex justify-between rounded-md px-3 py-2 gap-2 ${toneClass}`}>
      <span>{label}</span>
      <span
        className={`text-right truncate ${mono ? 'font-mono text-[11px]' : ''}`}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

interface RecoveryMetricCardProps {
  label: string;
  value: string;
  state: string | null;
  subtitle?: string;
  tooltip: string;
}

function RecoveryMetricCard({
  label,
  value,
  state,
  subtitle,
  tooltip,
}: RecoveryMetricCardProps) {
  return (
    <div
      className="rounded-lg border border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 p-4"
      title={tooltip}
      aria-label={`${label}: ${tooltip}`}
      data-testid={`kg-recovery-metric-${label.toLowerCase().replace(/\s+/g, '-')}`}
    >
      <div className="text-[11px] uppercase tracking-wide text-surface-500 dark:text-surface-400">
        {label}
      </div>
      <div className="mt-2 text-base font-semibold text-surface-900 dark:text-white">
        {value}
      </div>
      <div className={`text-xs ${stateBadgeClass(state)}`}>
        {state ?? subtitle ?? 'unknown'}
      </div>
      {subtitle && state && (
        <div className="text-[11px] text-surface-500 dark:text-surface-400 mt-1">
          {subtitle}
        </div>
      )}
    </div>
  );
}
