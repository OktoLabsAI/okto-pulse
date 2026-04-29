/**
 * KGHealthView — fullscreen overlay rendering the live KG health snapshot
 * for the active board (spec d754d004, MVP visualization-only).
 *
 * Renders 4 cards in a 2x2 grid (Schema&Tick, Queue&DeadLetter, KG Health,
 * Activity) plus the Top-10 most disconnected nodes table. Polls
 * GET /api/v1/kg/health every `pollIntervalMs` (default 30000) while the
 * tab is visible. Pauses on document.visibilityState='hidden', skips
 * overlapping fetches (BR4), aborts in-flight requests on unmount or
 * board change (BR8). Refresh button fires an immediate fetch without
 * touching the polling cadence (BR10).
 *
 * Stale tick badge — amber when (now - last_decay_tick_at) > 24h, red
 * when null (BR1). Schema banner — red full-width when schema_version
 * !== EXPECTED_SCHEMA_VERSION (BR2). Skeleton appears only on the very
 * first fetch (BR11). Errors preserve previous data and let polling
 * keep retrying (BR5/D9).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Database,
  Inbox,
  Loader2,
  Play,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { useDashboardStore } from '@/store/dashboard';
import { EXPECTED_KG_HEALTH_SCHEMA_VERSION } from '@/constants/kg';
import { getKGHealth, type KGHealth, type TopDisconnectedNode } from '@/services/kg-health-api';
import { triggerKGTick } from '@/services/kg-tick-api';

interface KGHealthViewProps {
  pollIntervalMs?: number;
  onClose: () => void;
}

const DEFAULT_POLL_INTERVAL_MS = 30000;
const STALE_TICK_THRESHOLD_MS = 24 * 60 * 60 * 1000;

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

  const tickInfo = useMemo(
    () => computeTickInfo(data?.last_decay_tick_at ?? null),
    [data?.last_decay_tick_at],
  );

  const schemaMismatch = data && data.schema_version !== EXPECTED_KG_HEALTH_SCHEMA_VERSION;

  if (!boardId) {
    return <EmptyState onClose={onClose} />;
  }

  return (
    <div className="flex flex-col h-full bg-surface-50 dark:bg-surface-950">
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
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
              <SchemaTickCard
                schemaVersion={data.schema_version}
                schemaMismatch={Boolean(schemaMismatch)}
                tickInfo={tickInfo}
                nodesRecomputed={data.nodes_recomputed_in_last_tick}
                boardId={boardId}
                onTickStarted={handleRefresh}
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
              />
              <ActivityCard
                disconnectedCount={data.top_disconnected_nodes.length}
              />
            </div>
            <TopDisconnectedTable rows={data.top_disconnected_nodes} />
          </>
        )}
      </div>
    </div>
  );
}

interface TickInfo {
  status: 'never' | 'stale' | 'fresh';
  ageHours: number | null;
  label: string;
  ariaLabel: string;
}

function computeTickInfo(lastDecayTickAt: string | null): TickInfo {
  if (!lastDecayTickAt) {
    return {
      status: 'never',
      ageHours: null,
      label: 'Tick has never run',
      ariaLabel: 'Tick has never run',
    };
  }
  const tickDate = new Date(lastDecayTickAt);
  const ageMs = Date.now() - tickDate.getTime();
  const ageHours = Math.floor(ageMs / (60 * 60 * 1000));
  if (ageMs > STALE_TICK_THRESHOLD_MS) {
    return {
      status: 'stale',
      ageHours,
      label: `Stale tick: ${ageHours}h ago`,
      ariaLabel: `Stale tick: ${ageHours} hours ago`,
    };
  }
  return {
    status: 'fresh',
    ageHours,
    label: `Last tick: ${ageHours}h ago`,
    ariaLabel: `Last tick: ${ageHours} hours ago`,
  };
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
  schemaMismatch: boolean;
  tickInfo: TickInfo;
  nodesRecomputed: number;
  /** Spec 54399628 — board scope para `triggerKGTick`. */
  boardId: string;
  /** Callback chamado após tick disparar com sucesso (para refresh natural). */
  onTickStarted: () => void;
  /** Bug fix — true quando o advisory lock global ``kg_daily_tick`` está
   *  acquired no backend. Vem de KGHealth.tick_in_progress, atualizado a
   *  cada poll (30s). Garante que o botão fica desabilitado mesmo se o
   *  usuário fechar o modal e voltar — ou se outra origem (cron/MCP)
   *  estiver rodando o tick agora. */
  tickInProgress: boolean;
}

function SchemaTickCard({
  schemaVersion,
  schemaMismatch,
  tickInfo,
  nodesRecomputed,
  boardId,
  onTickStarted,
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
      : tickInfo.status === 'stale'
      ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300'
      : 'text-surface-700 dark:text-surface-300';
  return (
    <Card title="Schema & Tick" testId="kg-health-card" icon={<Database className="w-4 h-4" aria-hidden />}>
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
      <Row label="Last tick">
        <span
          className={`text-sm font-semibold px-2 py-0.5 rounded ${tickClasses}`}
          aria-label={tickInfo.ariaLabel}
        >
          {tickInfo.label}
        </span>
      </Row>
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
}

function KGHealthCard({
  totalNodes,
  defaultScoreCount,
  defaultScoreRatio,
  avgRelevance,
  contradictWarnCount,
}: KGHealthCardProps) {
  const contradictClass =
    contradictWarnCount === 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-amber-600 dark:text-amber-400';
  const ratioPct = (defaultScoreRatio * 100).toFixed(1);
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
    </Card>
  );
}

interface ActivityCardProps {
  disconnectedCount: number;
}

function ActivityCard({ disconnectedCount }: ActivityCardProps) {
  const cls =
    disconnectedCount === 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-amber-600 dark:text-amber-400';
  return (
    <Card title="Activity" testId="kg-health-card" icon={<Activity className="w-4 h-4" aria-hidden />}>
      <Row label="Disconnected nodes (top 10)">
        <span className={`text-sm font-bold ${cls}`}>
          {disconnectedCount === 0 ? 'none detected' : `${disconnectedCount} detected`}
        </span>
      </Row>
      <p className="text-xs text-surface-500 dark:text-surface-400 italic mt-2">
        See the table below for details (id/type/degree). Nodes with degree=0 or 1 may indicate
        incomplete imports or disconnections during consolidation.
      </p>
    </Card>
  );
}

interface TopDisconnectedTableProps {
  rows: TopDisconnectedNode[];
}

function TopDisconnectedTable({ rows }: TopDisconnectedTableProps) {
  return (
    <div className="bg-white dark:bg-surface-800 rounded-xl border border-surface-200 dark:border-surface-700 shadow-sm overflow-hidden">
      <div className="px-5 py-3 border-b border-surface-200 dark:border-surface-700">
        <h2 className="text-sm font-semibold text-surface-700 dark:text-surface-300">
          Top 10 most disconnected nodes
        </h2>
      </div>
      {rows.length === 0 ? (
        <div className="px-5 py-8 text-center text-sm text-surface-500 dark:text-surface-400 italic">
          No disconnected nodes
        </div>
      ) : (
        <table className="w-full text-sm">
          <caption className="sr-only">List of lowest-degree nodes in the current board's KG</caption>
          <thead className="bg-surface-50 dark:bg-surface-900/50 text-xs uppercase text-surface-500 dark:text-surface-400">
            <tr>
              <th scope="col" className="text-left px-5 py-2.5">
                Node ID
              </th>
              <th scope="col" className="text-left px-5 py-2.5">
                Type
              </th>
              <th scope="col" className="text-right px-5 py-2.5">
                Degree
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-200 dark:divide-surface-700">
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-surface-50 dark:hover:bg-surface-900/30">
                <td className="px-5 py-2 font-mono text-xs text-surface-700 dark:text-surface-300 max-w-xs truncate" title={row.id}>
                  {row.id}
                </td>
                <td className="px-5 py-2">
                  <span className={`px-1.5 py-0.5 text-xs rounded ${typeBadgeClasses(row.type)}`}>
                    {row.type}
                  </span>
                </td>
                <td className="px-5 py-2 text-right font-mono">{row.degree}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function typeBadgeClasses(type: string): string {
  const lower = type.toLowerCase();
  if (lower.includes('decision'))
    return 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300';
  if (lower.includes('criterion'))
    return 'bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300';
  if (lower.includes('constraint'))
    return 'bg-rose-100 dark:bg-rose-900/30 text-rose-700 dark:text-rose-300';
  return 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300';
}

function SkeletonGrid() {
  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            data-testid="skeleton-card"
            className="bg-white dark:bg-surface-800 rounded-xl border border-surface-200 dark:border-surface-700 p-5 h-40 animate-pulse"
            aria-hidden
          />
        ))}
      </div>
      <div
        className="bg-white dark:bg-surface-800 rounded-xl border border-surface-200 dark:border-surface-700 p-6 animate-pulse"
        data-testid="skeleton-table"
        aria-hidden
      >
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-6 bg-surface-200 dark:bg-surface-700 rounded" />
          ))}
        </div>
      </div>
    </>
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
    <div className="flex flex-col h-full bg-surface-50 dark:bg-surface-950">
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
