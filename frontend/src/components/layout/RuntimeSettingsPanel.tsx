/**
 * Runtime Settings panel — Graph DB | Event Queue (spec bdcda842).
 *
 * Two-tab layout:
 *   * **Graph DB** (default tab): Kùzu memory tuning. Changing any field
 *     here flips ``restart_required`` because Kùzu Database() is
 *     constructor-time. Banner amber sinaliza isso.
 *   * **Event Queue** (new in v0.1.5): consolidation queue throughput
 *     knobs (max workers, throttle, claim timeout, max attempts, alert
 *     threshold) + Live Queue Health panel polling /api/v1/kg/queue/health
 *     every 2000ms. Banner azul reforça que hot-reload é a semântica.
 *
 * Both tabs share the same draft buffer so a single Save persists
 * partial PUTs across both tab states. Switching tabs preserves the
 * draft (no fetch, no reset).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Clock, Database, Play, Settings, X, Zap } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  getRuntimeSettings,
  putRuntimeSettings,
  type RuntimeSettings,
} from '@/services/runtime-settings-api';
import {
  getQueueHealth,
  type QueueHealth,
} from '@/services/queue-health-api';
import { triggerKGTick } from '@/services/kg-tick-api';
import { getKGHealth } from '@/services/kg-health-api';
import { DeadLetterInspectorModal } from '@/components/knowledge/DeadLetterInspectorModal';
import { useDashboardStore } from '@/store/dashboard';

interface RuntimeSettingsPanelProps {
  onClose: () => void;
  initialTab?: ActiveTab;
}

// Spec 818748f2 (Board panel migration): the per-board NC-9 evidence gate
// toggle moved out of this modal into the Header's "Board" panel alongside
// the other skip_*_coverage_global toggles. Keep this surface focused on
// runtime knobs (Kùzu, queue, decay tick).
type ActiveTab = 'graphdb' | 'eventqueue' | 'decaytick';

const RANGES: Record<keyof Omit<RuntimeSettings, 'restart_required'>, { min: number; max: number }> = {
  // Graph DB tab
  kg_kuzu_buffer_pool_mb: { min: 16, max: 512 },
  kg_kuzu_max_db_size_gb: { min: 1, max: 64 },
  kg_connection_pool_size: { min: 1, max: 32 },
  // Event Queue tab
  kg_queue_max_concurrent_workers: { min: 1, max: 16 },
  kg_queue_min_interval_ms: { min: 0, max: 1000 },
  kg_queue_claim_timeout_s: { min: 60, max: 3600 },
  kg_queue_max_attempts: { min: 1, max: 10 },
  kg_queue_alert_threshold: { min: 100, max: 100000 },
  // Decay Tick tab (spec 54399628)
  kg_decay_tick_interval_minutes: { min: 5, max: 10080 },
  kg_decay_tick_staleness_days: { min: 1, max: 365 },
  kg_decay_tick_max_age_days: { min: 0, max: 365 },
};

// Non-Kùzu baseline: embedding singleton (~120 MB) + query caches (~100 MB) +
// Python/FastAPI runtime (~300 MB) + session/transaction state (~100 MB).
const BUDGET_BASELINE_MB = 620;
const HEALTH_POLL_INTERVAL_MS = 2000;

type DraftState = Omit<RuntimeSettings, 'restart_required'>;

const ZERO_DRAFT: DraftState = {
  kg_kuzu_buffer_pool_mb: 0,
  kg_kuzu_max_db_size_gb: 0,
  kg_connection_pool_size: 0,
  kg_queue_max_concurrent_workers: 0,
  kg_queue_min_interval_ms: 0,
  kg_queue_claim_timeout_s: 0,
  kg_queue_max_attempts: 0,
  kg_queue_alert_threshold: 0,
  // Decay Tick (spec 54399628)
  kg_decay_tick_interval_minutes: 0,
  kg_decay_tick_staleness_days: 0,
  kg_decay_tick_max_age_days: 0,
};

function snapshotDraft(data: RuntimeSettings): DraftState {
  const out: DraftState = { ...ZERO_DRAFT };
  for (const key of Object.keys(ZERO_DRAFT) as Array<keyof DraftState>) {
    out[key] = data[key];
  }
  return out;
}

export function RuntimeSettingsPanel({
  onClose,
  initialTab = 'graphdb',
}: RuntimeSettingsPanelProps) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [values, setValues] = useState<RuntimeSettings | null>(null);
  // Draft state lets the user type freely without triggering saves;
  // shared across both tabs so Save persists partial PUTs in one shot.
  const [draft, setDraft] = useState<DraftState>(ZERO_DRAFT);
  // True once a successful PUT happens AND the changes touched a Graph DB
  // key (Kùzu constructor-time). Event Queue mutations never set this.
  const [restartRequired, setRestartRequired] = useState(false);
  const [activeTab, setActiveTab] = useState<ActiveTab>(initialTab);
  // Spec ed17b1fe (Wave 2 NC 1ede3471) — DLQ Inspector modal state.
  const [showDeadLetter, setShowDeadLetter] = useState(false);
  const currentBoard = useDashboardStore((s) => s.currentBoard);
  // Bug fix — true quando o advisory lock global ``kg_daily_tick`` está
  // acquired no backend. Polled enquanto o usuário está no Decay Tick tab
  // para que "Save & run now" fique disabled mesmo se o usuário tiver
  // acabado de chegar (cron, MCP ou outro tab podem ter disparado o tick).
  const [tickInProgress, setTickInProgress] = useState(false);

  useEffect(() => {
    let active = true;
    getRuntimeSettings()
      .then((data) => {
        if (!active) return;
        setValues(data);
        setDraft(snapshotDraft(data));
        setRestartRequired(data.restart_required);
      })
      .catch((err) => {
        if (!active) return;
        setError(err?.message ?? 'Failed to load runtime settings');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  // Bug fix — poll do tick_in_progress só enquanto o Decay Tick tab está
  // ativo (a única superfície onde o "Save & run now" aparece). Intervalo
  // 15 s é um trade-off entre responsividade e pressão no DB pool — o
  // ``getKGHealth`` faz queries SQL pesadas (queue_depth aggregation) e
  // queremos evitar saturar o pool junto com SSE streams + workers. O
  // ``inFlightRef`` cooldown de 3 s + advisory lock no backend cobre o
  // gap de detecção em cliques rápidos.
  useEffect(() => {
    if (activeTab !== 'decaytick' || !currentBoard) {
      setTickInProgress(false);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    const fetchOnce = async () => {
      try {
        const h = await getKGHealth(currentBoard.id, controller.signal);
        if (!cancelled) setTickInProgress(Boolean(h.tick_in_progress));
      } catch {
        // Health degrades gracefully — botão fica habilitado se polling falha.
      }
    };
    fetchOnce();
    const id = window.setInterval(fetchOnce, 15000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(id);
    };
  }, [activeTab, currentBoard]);

  const budgetMb =
    draft.kg_connection_pool_size * draft.kg_kuzu_buffer_pool_mb +
    BUDGET_BASELINE_MB;

  const outOfRange = useMemo(() => {
    return (Object.keys(RANGES) as Array<keyof typeof RANGES>).some((key) => {
      const v = draft[key];
      const { min, max } = RANGES[key];
      return !Number.isFinite(v) || v < min || v > max;
    });
  }, [draft]);

  const onInputChange = (key: keyof DraftState, raw: string) => {
    const parsed = Number(raw);
    setDraft((d) => ({
      ...d,
      [key]: Number.isFinite(parsed) ? parsed : 0,
    }));
  };

  const onReset = () => {
    if (!values) return;
    setDraft(snapshotDraft(values));
  };

  // Bug fix (Playwright E2E reproduzido):
  //
  // 1. `useRef` síncrono bloqueia rajada de cliques antes do re-render React.
  // 2. Unlock do ref é DEFERRED por 3s no caminho que dispara o tick — o
  //    endpoint retorna 202 quase imediatamente, sem cooldown o ref
  //    liberaria antes do próximo click humano (>100ms). Para o `onSave`
  //    puro (PUT), o unlock no finally já basta porque a request leva mais.
  // 3. `tickInProgress` vindo do polling do `/kg/health` cobre cross-mount /
  //    cross-tab (avaliado no `disabled` do botão e no entry guard).
  const inFlightRef = useRef(false);

  const onSave = async () => {
    if (outOfRange || inFlightRef.current) return;
    inFlightRef.current = true;
    setSaving(true);
    try {
      const resp = await putRuntimeSettings(draft);
      setValues(resp);
      setRestartRequired(resp.restart_required);
      if (resp.restart_required) {
        toast.success('Settings saved — restart required for Graph DB changes');
      } else {
        toast.success('Settings saved (hot-reload, no restart needed)');
      }
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to save runtime settings');
    } finally {
      inFlightRef.current = false;
      setSaving(false);
    }
  };

  // Spec 54399628 (Wave 2 NC f9732afc) — "Save & run now" button:
  // persists settings AND immediately triggers a tick. Available on the
  // Decay Tick tab; surfaces 409 (tick_already_running) as an amber toast
  // so the operator knows settings were still saved.
  const onSaveAndRunNow = async () => {
    // tickInProgress vem do polling KG health (5s) e cobre cross-mount/
    // cross-tab. inFlightRef cobre clique-rápido na mesma sessão.
    if (outOfRange || inFlightRef.current || tickInProgress) return;
    inFlightRef.current = true;
    setSaving(true);
    // Cooldown lock — mantém o guard por 3s além do fetch para cobrir o
    // gap entre o 202 do tick e o próximo poll do health (5s).
    setTimeout(() => { inFlightRef.current = false; }, 3000);
    try {
      const resp = await putRuntimeSettings(draft);
      setValues(resp);
      setRestartRequired(resp.restart_required);
      try {
        await triggerKGTick();
        toast.success('Settings saved. Tick started.');
      } catch (tickErr: any) {
        if (tickErr?.code === 'tick_already_running') {
          toast(
            'Tick already running — settings still saved.',
            { icon: '⚠️' },
          );
        } else {
          toast.error(
            `Settings saved, but tick failed: ${tickErr?.message ?? 'unknown error'}`,
          );
        }
      }
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to save runtime settings');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative w-[640px] max-w-[92vw] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        data-testid="runtime-settings-panel"
      >
        <button
          onClick={onClose}
          className="absolute top-3 right-3 p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg transition-colors z-10"
          aria-label="Close settings"
        >
          <X size={16} />
        </button>

        <div className="px-6 pt-5 pb-3 border-b border-gray-200 dark:border-gray-800">
          <h2 className="text-base font-semibold text-gray-900 dark:text-white">
            Settings
          </h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Knowledge Graph runtime tuning
          </p>
        </div>

        <TabsNav activeTab={activeTab} onChange={setActiveTab} />

        {restartRequired && activeTab === 'graphdb' && (
          <div
            className="px-6 py-2.5 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800/50 text-xs text-amber-900 dark:text-amber-200"
            data-testid="restart-required-banner"
          >
            <strong>Restart required.</strong> New values persist but only
            take effect after restarting the Okto Pulse process
            (Kùzu constructor-time).
          </div>
        )}

        {activeTab === 'eventqueue' && (
          <div
            className="px-6 py-2.5 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-200 dark:border-blue-800/50 text-xs text-blue-900 dark:text-blue-200 space-y-1"
            data-testid="hot-reload-banner"
          >
            <div>
              <strong>Hot-reload.</strong> Worker pool re-reads config on
              every claim cycle. No restart required.
            </div>
            <div className="text-[11px] opacity-90">
              <strong>Per-board lock:</strong> Kùzu serializes commits per
              board; worker parallelism only scales across distinct boards.
            </div>
          </div>
        )}

        {loading ? (
          <div className="px-6 py-10 text-sm text-gray-500 dark:text-gray-400 text-center">
            Loading runtime settings…
          </div>
        ) : error ? (
          <div className="px-6 py-10 text-sm text-red-500 text-center">
            {error}
          </div>
        ) : activeTab === 'graphdb' ? (
          <GraphDBTab
            draft={draft}
            onChange={onInputChange}
            budgetMb={budgetMb}
          />
        ) : activeTab === 'eventqueue' ? (
          <EventQueueTab
            draft={draft}
            onChange={onInputChange}
            isActive={activeTab === 'eventqueue'}
            onOpenDeadLetterInspector={() => setShowDeadLetter(true)}
          />
        ) : (
          <DecayTickTab draft={draft} onChange={onInputChange} />
        )}

        <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-800 flex items-center justify-end gap-2">
          <button
            onClick={onReset}
            disabled={!values || saving}
            className="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50"
          >
            Reset
          </button>
          <button
            onClick={onSave}
            disabled={loading || saving || outOfRange}
            className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg disabled:opacity-50"
            data-testid="save-runtime-settings"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          {activeTab === 'decaytick' && (
            <button
              onClick={onSaveAndRunNow}
              disabled={loading || saving || outOfRange || tickInProgress}
              className="px-3 py-1.5 text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg disabled:opacity-50 inline-flex items-center gap-1"
              data-testid="save-and-run-now"
              title={tickInProgress && !saving ? 'Tick is already running globally (cron, MCP or another tab)' : undefined}
            >
              <Play size={11} />
              {saving
                ? 'Saving…'
                : tickInProgress
                  ? 'Tick in progress…'
                  : 'Save & run now'}
            </button>
          )}
        </div>
      </div>

      {showDeadLetter && currentBoard && (
        <DeadLetterInspectorModal
          boardId={currentBoard.id}
          onClose={() => setShowDeadLetter(false)}
        />
      )}
    </div>
  );
}

interface TabsNavProps {
  activeTab: ActiveTab;
  onChange: (tab: ActiveTab) => void;
}

function TabsNav({ activeTab, onChange }: TabsNavProps) {
  const tabClass = (tab: ActiveTab) =>
    activeTab === tab
      ? 'px-4 py-2 text-xs font-medium text-blue-600 dark:text-blue-400 border-b-2 border-blue-600 -mb-px'
      : 'px-4 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 border-b-2 border-transparent -mb-px';

  return (
    <div className="px-6 pt-3 border-b border-gray-200 dark:border-gray-800">
      <div className="flex gap-1" role="tablist">
        <button
          role="tab"
          aria-selected={activeTab === 'graphdb'}
          className={tabClass('graphdb')}
          onClick={() => onChange('graphdb')}
          data-testid="tab-graphdb"
        >
          <span className="inline-flex items-center gap-1.5">
            <Settings size={12} />
            Graph DB
          </span>
        </button>
        <button
          role="tab"
          aria-selected={activeTab === 'eventqueue'}
          className={tabClass('eventqueue')}
          onClick={() => onChange('eventqueue')}
          data-testid="tab-eventqueue"
        >
          <span className="inline-flex items-center gap-1.5">
            <Zap size={12} />
            Event Queue
            <span className="ml-1 inline-flex items-center justify-center px-1.5 py-0.5 text-[9px] font-semibold bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 rounded-full">
              live
            </span>
          </span>
        </button>
        <button
          role="tab"
          aria-selected={activeTab === 'decaytick'}
          className={tabClass('decaytick')}
          onClick={() => onChange('decaytick')}
          data-testid="tab-decaytick"
        >
          <span className="inline-flex items-center gap-1.5">
            <Clock size={12} />
            Decay Tick
          </span>
        </button>
      </div>
    </div>
  );
}

interface DecayTickTabProps {
  draft: DraftState;
  onChange: (key: keyof DraftState, raw: string) => void;
}

/**
 * Spec 54399628 (Wave 2 NC f9732afc) — KG decay tick controllability.
 *
 * Three persisted settings with hot-reload via APScheduler.reschedule_job:
 * Tick interval (5min-7d), Staleness threshold (1-365d), and the optional
 * Max age cap (0=no cap, useful for legacy boards). Companion endpoint
 * POST /api/v1/kg/tick/run-now is reachable via the "Save & run now"
 * button on the save bar AND via the dedicated button on the
 * KGHealthView SchemaTickCard.
 */
function DecayTickTab({ draft, onChange }: DecayTickTabProps) {
  return (
    <div className="px-6 py-5 space-y-4">
      <div
        className="px-3 py-2 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800/50 text-[11px] text-blue-900 dark:text-blue-200 rounded-lg space-y-1"
        data-testid="decay-tick-banner"
      >
        <div>
          <strong>Hot-reload.</strong> Changing the interval triggers
          APScheduler.reschedule_job — the next tick honours the new value
          without a server restart.
        </div>
        <div className="text-[10px] opacity-90">
          <strong>Save & run now</strong> on the bar below persists settings AND
          immediately fires a tick (HTTP 202 with tick_id; HTTP 409 when one
          is already running — settings still saved in either case).
        </div>
      </div>

      <SettingField
        label="Tick interval (minutes)"
        description="How often the KG decay tick runs. Default 1440 (1 day). Range 5 (5 min) to 10080 (7 days)."
        value={draft.kg_decay_tick_interval_minutes}
        range={RANGES.kg_decay_tick_interval_minutes}
        onChange={(v) => onChange('kg_decay_tick_interval_minutes', v)}
        testId="input-tick-interval-minutes"
      />
      <SettingField
        label="Staleness threshold (days)"
        description="Only nodes with last_recomputed_at older than N days are recomputed. Default 7."
        value={draft.kg_decay_tick_staleness_days}
        range={RANGES.kg_decay_tick_staleness_days}
        onChange={(v) => onChange('kg_decay_tick_staleness_days', v)}
        testId="input-tick-staleness-days"
      />
      <SettingField
        label="Max age cap (days)"
        description="0 = no cap (default). > 0 forces recompute even of fresh nodes older than N days. Useful for legacy boards."
        value={draft.kg_decay_tick_max_age_days}
        range={RANGES.kg_decay_tick_max_age_days}
        onChange={(v) => onChange('kg_decay_tick_max_age_days', v)}
        testId="input-tick-max-age-days"
      />
    </div>
  );
}

interface GraphDBTabProps {
  draft: DraftState;
  onChange: (key: keyof DraftState, raw: string) => void;
  budgetMb: number;
}

function GraphDBTab({ draft, onChange, budgetMb }: GraphDBTabProps) {
  return (
    <div className="px-6 py-5 space-y-4">
      <SettingField
        label="Kùzu buffer pool per board (MB)"
        description="Recommended 32-128 MB. Safe default: 48."
        value={draft.kg_kuzu_buffer_pool_mb}
        range={RANGES.kg_kuzu_buffer_pool_mb}
        onChange={(v) => onChange('kg_kuzu_buffer_pool_mb', v)}
        testId="input-buffer-pool-mb"
      />
      <SettingField
        label="Kùzu max database size per board (GB)"
        description="Virtual address space. Does not commit memory until used."
        value={draft.kg_kuzu_max_db_size_gb}
        range={RANGES.kg_kuzu_max_db_size_gb}
        onChange={(v) => onChange('kg_kuzu_max_db_size_gb', v)}
        testId="input-max-db-size-gb"
      />
      <SettingField
        label="Connection pool cap (simultaneous boards)"
        description="Boards kept alive in the LRU cache."
        value={draft.kg_connection_pool_size}
        range={RANGES.kg_connection_pool_size}
        onChange={(v) => onChange('kg_connection_pool_size', v)}
        testId="input-pool-size"
      />

      <div
        className="mt-4 px-3 py-2 bg-gray-50 dark:bg-gray-800 rounded-lg text-xs text-gray-600 dark:text-gray-300"
        data-testid="budget-display"
      >
        <strong>Estimated budget:</strong>{' '}
        {draft.kg_connection_pool_size} × {draft.kg_kuzu_buffer_pool_mb} +{' '}
        {BUDGET_BASELINE_MB} = <strong>{budgetMb} MB</strong>
        <span className="text-gray-400">
          {' '}committed (non-Kùzu baseline {BUDGET_BASELINE_MB} MB)
        </span>
      </div>
    </div>
  );
}

interface EventQueueTabProps {
  draft: DraftState;
  onChange: (key: keyof DraftState, raw: string) => void;
  isActive: boolean;
  onOpenDeadLetterInspector: () => void;
}

function EventQueueTab({
  draft,
  onChange,
  isActive,
  onOpenDeadLetterInspector,
}: EventQueueTabProps) {
  const health = useQueueHealth(isActive);
  return (
    <>
      <div className="px-6 py-5 grid grid-cols-2 gap-x-6 gap-y-4">
        <SettingField
          label="Max concurrent workers"
          description="Asyncio worker pool size. Items on the same board serialize in commit_coordinator; gain only with multi-board workloads."
          value={draft.kg_queue_max_concurrent_workers}
          range={RANGES.kg_queue_max_concurrent_workers}
          onChange={(v) => onChange('kg_queue_max_concurrent_workers', v)}
          testId="input-max-workers"
        />
        <SettingField
          label="Min interval between claims (ms)"
          description="Per-worker SQLite throttling on the queue table. 0 = no throttling."
          value={draft.kg_queue_min_interval_ms}
          range={RANGES.kg_queue_min_interval_ms}
          onChange={(v) => onChange('kg_queue_min_interval_ms', v)}
          testId="input-min-interval-ms"
        />
        <SettingField
          label="Claim timeout (seconds)"
          description="Crash recovery threshold. Should exceed p99 of consolidate+commit time."
          value={draft.kg_queue_claim_timeout_s}
          range={RANGES.kg_queue_claim_timeout_s}
          onChange={(v) => onChange('kg_queue_claim_timeout_s', v)}
          testId="input-claim-timeout-s"
        />
        <SettingField
          label="Max attempts before dead-letter"
          description="Exponential backoff capped at 5min after each failure."
          value={draft.kg_queue_max_attempts}
          range={RANGES.kg_queue_max_attempts}
          onChange={(v) => onChange('kg_queue_max_attempts', v)}
          testId="input-max-attempts"
        />
        <div className="col-span-2">
          <SettingField
            label="Alert threshold (queue depth)"
            description="Alerting only — never rejects events. Replaces deprecated kg_max_queue_depth."
            value={draft.kg_queue_alert_threshold}
            range={RANGES.kg_queue_alert_threshold}
            onChange={(v) => onChange('kg_queue_alert_threshold', v)}
            testId="input-alert-threshold"
          />
        </div>
      </div>

      <LiveQueueHealthPanel health={health} />

      <div className="px-6 pb-3">
        <button
          type="button"
          onClick={onOpenDeadLetterInspector}
          className="text-[10px] text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1"
          data-testid="dead-letter-inspector-link"
        >
          <Database size={10} />
          Open dead-letter inspector
        </button>
      </div>
    </>
  );
}

interface LiveQueueHealthPanelProps {
  health: QueueHealth | null;
}

function LiveQueueHealthPanel({ health }: LiveQueueHealthPanelProps) {
  const utilization = health
    ? Math.min(
        100,
        (health.queue_depth / Math.max(1, health.alert_threshold)) * 100,
      )
    : 0;
  return (
    <div
      className="mx-6 mb-3 p-3 bg-gray-50 dark:bg-gray-800/60 rounded-lg border border-gray-200 dark:border-gray-700"
      data-testid="live-queue-health-panel"
    >
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold text-gray-700 dark:text-gray-200">
          Live queue health
        </h3>
        <span className="text-[9px] text-gray-400">
          refresh {HEALTH_POLL_INTERVAL_MS / 1000}s · /api/v1/kg/queue/health
        </span>
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Metric label="Depth" value={health?.queue_depth ?? '—'} />
        <Metric
          label="Oldest pending"
          value={health ? `${health.oldest_pending_age_s.toFixed(1)}s` : '—'}
        />
        <Metric
          label="Dead-letter"
          value={health?.dead_letter_count ?? '—'}
          tone={
            health && health.dead_letter_count > 0 ? 'amber' : 'emerald'
          }
        />
        <Metric label="Claims / min" value={health?.claims_per_min_1m ?? '—'} />
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3 pt-3 border-t border-gray-200 dark:border-gray-700">
        <div>
          <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            Workers active
          </div>
          <div className="text-sm font-semibold text-blue-600 dark:text-blue-400">
            {health?.workers_active ?? '—'}
            {health && health.workers_idle > 0 && (
              <span className="text-gray-400 text-[10px] font-normal">
                {' '}/ {health.workers_active + health.workers_idle}
              </span>
            )}
          </div>
          {health && health.claimed_boards.length > 0 && (
            <div className="text-[9px] text-gray-400 mt-0.5">
              across {health.claimed_boards.length} distinct{' '}
              {health.claimed_boards.length === 1 ? 'board' : 'boards'}
            </div>
          )}
        </div>
        <div>
          <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            Kùzu lock retries (5m)
          </div>
          <div
            className={`text-sm font-semibold ${
              health && health.kuzu_lock_retries_5m > 0
                ? 'text-amber-600 dark:text-amber-400'
                : 'text-gray-700 dark:text-gray-300'
            }`}
          >
            {health?.kuzu_lock_retries_5m ?? '—'}
          </div>
          {health && health.kuzu_lock_retries_5m > 0 && (
            <div className="text-[9px] text-gray-400 mt-0.5">
              cross-process contention
            </div>
          )}
        </div>
        <div>
          <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            Queue utilization
          </div>
          <div className="flex items-center gap-2 mt-1">
            <div className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                className={
                  health?.alert_active
                    ? 'h-full bg-amber-500'
                    : 'h-full bg-emerald-500'
                }
                style={{ width: `${utilization.toFixed(2)}%` }}
              />
            </div>
            <span className="text-[10px] text-gray-500 tabular-nums">
              {utilization.toFixed(1)}%
            </span>
          </div>
          <div className="text-[9px] text-gray-400 mt-0.5">
            vs alert threshold
          </div>
        </div>
      </div>
    </div>
  );
}

interface MetricProps {
  label: string;
  value: number | string;
  tone?: 'emerald' | 'amber' | 'default';
}

function Metric({ label, value, tone = 'default' }: MetricProps) {
  const valueClass =
    tone === 'emerald'
      ? 'text-emerald-600 dark:text-emerald-400'
      : tone === 'amber'
        ? 'text-amber-600 dark:text-amber-400'
        : 'text-gray-900 dark:text-white';
  return (
    <div>
      <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">
        {label}
      </div>
      <div className={`text-base font-semibold ${valueClass}`}>{value}</div>
    </div>
  );
}

/**
 * Polls /api/v1/kg/queue/health every 2000ms while the EventQueueTab is
 * active. Cleanup runs on unmount AND when the tab becomes inactive — so
 * switching to Graph DB tab stops the polling immediately (TR10 + AC12).
 */
function useQueueHealth(active: boolean): QueueHealth | null {
  const [health, setHealth] = useState<QueueHealth | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!active) {
      // Cancel any in-flight request so React doesn't get a setState after
      // unmount/blur.
      abortRef.current?.abort();
      abortRef.current = null;
      return;
    }

    let cancelled = false;
    const tick = async () => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const data = await getQueueHealth(controller.signal);
        if (!cancelled) setHealth(data);
      } catch (err: any) {
        if (err?.name === 'AbortError') return;
        // Tolerar erros transitórios — UI mostra "—" e tenta de novo no próximo tick.
      }
    };

    void tick();
    const interval = setInterval(tick, HEALTH_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(interval);
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [active]);

  return health;
}

interface SettingFieldProps {
  label: string;
  description: string;
  value: number;
  range: { min: number; max: number };
  onChange: (raw: string) => void;
  testId: string;
}

function SettingField({
  label,
  description,
  value,
  range,
  onChange,
  testId,
}: SettingFieldProps) {
  const outOfRange = !Number.isFinite(value) || value < range.min || value > range.max;
  return (
    <div>
      <label className="text-xs font-medium text-gray-700 dark:text-gray-200 block mb-0.5">
        {label}
      </label>
      <p className="text-[10px] text-gray-400 mb-1.5">{description}</p>
      <div className="flex items-center gap-2">
        <input
          type="number"
          min={range.min}
          max={range.max}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          data-testid={testId}
          className={`w-28 text-xs px-2 py-1 border rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 ${
            outOfRange
              ? 'border-red-400 dark:border-red-700'
              : 'border-gray-300 dark:border-gray-600'
          }`}
        />
        <span className="text-[10px] text-gray-400">
          {range.min}-{range.max}
        </span>
      </div>
    </div>
  );
}
