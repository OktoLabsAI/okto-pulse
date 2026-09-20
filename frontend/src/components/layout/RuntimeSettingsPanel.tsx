/**
 * Runtime Settings panel — Grafx | Event Queue | Decay Tick.
 *
 * Two-tab layout:
 *   * **Grafx** (default tab): storage geometry and descriptor validation.
 *     Changes are constructor-time and therefore require a restart.
 *   * **Event Queue** (new in v0.2.0): consolidation queue throughput
 *     knobs (max workers, throttle, claim timeout, max attempts, alert
 *     threshold). Queue health is observed in consolidated KG Health.
 *
 * Both tabs share the same draft buffer so a single Save persists
 * partial PUTs across both tab states. Switching tabs preserves the
 * draft (no fetch, no reset).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Clock, Settings, X, Zap } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  getRuntimeSettings,
  putRuntimeSettings,
  type RuntimeSettings,
  type RuntimeSettingsPatch,
  type RuntimeSettingsValues,
  type GrafxSettingDescriptor,
} from '@/services/runtime-settings-api';
import { useDashboardStore } from '@/store/dashboard';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import { GrafxAdvancedSettings, SettingHelp } from './GrafxAdvancedSettings';
import { GrafxBranding } from '@/components/shared/GrafxBranding';

interface RuntimeSettingsPanelProps {
  onClose: () => void;
  initialTab?: ActiveTab;
}

// Spec 818748f2 (Board panel migration): the per-board NC-9 evidence gate
// toggle moved out of this modal into the Header's "Board" panel alongside
// the other skip_*_coverage_global toggles. Keep this surface focused on
// runtime knobs (graph storage, queue, decay tick).
type ActiveTab = 'graphdb' | 'eventqueue' | 'decaytick';

const GRAFX_PAGE_SIZE_OPTIONS = [4096, 8192, 16384, 32768] as const;

type NumericSettingKey = Exclude<
  keyof RuntimeSettingsValues,
  'kg_grafx_descriptor_revalidation' | 'kg_grafx_options'
>;

const RANGES: Record<NumericSettingKey, { min: number; max: number }> = {
  // Grafx tab
  kg_grafx_page_size: { min: 4096, max: 32768 },
  kg_grafx_buffer_pool_mb: { min: 1, max: Number.MAX_SAFE_INTEGER },
  kg_grafx_read_participants: { min: 1, max: 8 },
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


type DraftState = Required<RuntimeSettingsValues>;

const ZERO_DRAFT: DraftState = {
  kg_grafx_page_size: 0,
  kg_grafx_descriptor_revalidation: 'generation',
  kg_grafx_buffer_pool_mb: 64,
  kg_grafx_read_participants: 2,
  kg_grafx_options: {},
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
  const editableValues = {
    ...data,
    ...(data.desired_values ?? {}),
  };
  return {
    kg_grafx_page_size: editableValues.kg_grafx_page_size,
    kg_grafx_descriptor_revalidation:
      editableValues.kg_grafx_descriptor_revalidation,
    kg_grafx_buffer_pool_mb: editableValues.kg_grafx_buffer_pool_mb ?? 64,
    kg_grafx_read_participants: editableValues.kg_grafx_read_participants ?? 2,
    kg_grafx_options: editableValues.kg_grafx_options ?? {},
    kg_queue_max_concurrent_workers: editableValues.kg_queue_max_concurrent_workers,
    kg_queue_min_interval_ms: editableValues.kg_queue_min_interval_ms,
    kg_queue_claim_timeout_s: editableValues.kg_queue_claim_timeout_s,
    kg_queue_max_attempts: editableValues.kg_queue_max_attempts,
    kg_queue_alert_threshold: editableValues.kg_queue_alert_threshold,
    kg_decay_tick_interval_minutes: editableValues.kg_decay_tick_interval_minutes,
    kg_decay_tick_staleness_days: editableValues.kg_decay_tick_staleness_days,
    kg_decay_tick_max_age_days: editableValues.kg_decay_tick_max_age_days,
  };
}

export function RuntimeSettingsPanel({
  onClose,
  initialTab = 'graphdb',
}: RuntimeSettingsPanelProps) {
  useEscapeToClose(onClose);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [values, setValues] = useState<DraftState | null>(null);
  // Draft state lets the user type freely without triggering saves;
  // shared across both tabs so Save persists partial PUTs in one shot.
  const [draft, setDraft] = useState<DraftState>(ZERO_DRAFT);
  const [grafxCatalog, setGrafxCatalog] = useState<GrafxSettingDescriptor[]>([]);
  const [graphProviders, setGraphProviders] = useState({
    board: 'grafx',
    global: 'grafx',
  });
  // True once a successful PUT happens AND the changes touched a Graph DB
  // key (graph database startup-time). Event Queue mutations never set this.
  const [restartRequired, setRestartRequired] = useState(false);
  const [activeTab, setActiveTab] = useState<ActiveTab>(initialTab);
  const currentBoard = useDashboardStore((s) => s.currentBoard);
  const permissions = usePermissions(currentBoard?.id);
  const policyReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canReadRuntime = policyReady && permissions.has('runtime.settings.read');
  const canWriteRuntime = policyReady && permissions.has('runtime.settings.write');
  useEffect(() => {
    if (permissions.isLoading) return;
    if (!canReadRuntime) {
      setLoading(false);
      setError('You do not have permission to read runtime settings');
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    getRuntimeSettings()
      .then((data) => {
        if (!active) return;
        const editableValues = snapshotDraft(data);
        setValues(editableValues);
        setDraft(editableValues);
        setGrafxCatalog(data.grafx_settings_catalog ?? []);
        setGraphProviders({
          board: data.kg_graph_backend,
          global: data.kg_global_graph_backend,
        });
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
  }, [canReadRuntime, permissions.isLoading]);

  const outOfRange = useMemo(() => {
    if (Object.entries(draft.kg_grafx_options).some(([key, value]) => {
      const item = grafxCatalog.find((entry) => entry.name === key);
      if (!item || !item.editable) return true;
      if (value === null) return !item.nullable;
      if (item.choices) return !item.choices.includes(String(value));
      return typeof value !== 'number' || !Number.isFinite(value)
        || Math.abs(value) > Number.MAX_SAFE_INTEGER
        || (item.minimum != null && value < item.minimum)
        || (item.maximum != null && value > item.maximum)
        || (!key.endsWith('_seconds') && !Number.isInteger(value));
    })) return true;
    return (Object.keys(RANGES) as NumericSettingKey[]).some((key) => {
      const v = draft[key];
      const { min, max } = RANGES[key];
      if (
        key === 'kg_grafx_page_size'
        && !GRAFX_PAGE_SIZE_OPTIONS.includes(v as typeof GRAFX_PAGE_SIZE_OPTIONS[number])
      ) {
        return true;
      }
      return !Number.isSafeInteger(v) || v < min || v > max;
    });
  }, [draft, grafxCatalog]);

  const onInputChange = (key: NumericSettingKey, raw: string) => {
    const parsed = Number(raw);
    setDraft((d) => ({
      ...d,
      [key]: Number.isFinite(parsed) ? parsed : 0,
    }));
  };

  const buildPatch = (): RuntimeSettingsPatch => {
    return Object.fromEntries(
      (Object.keys(ZERO_DRAFT) as Array<keyof DraftState>)
        .filter((key) => values === null || draft[key] !== values[key])
        .map((key) => [key, draft[key]]),
    ) as RuntimeSettingsPatch;
  };

  const onReset = () => {
    if (!values) return;
    setDraft({ ...values });
  };

  const inFlightRef = useRef(false);

  const onSave = async () => {
    if (!canWriteRuntime || outOfRange || inFlightRef.current) return;
    inFlightRef.current = true;
    setSaving(true);
    try {
      const resp = await putRuntimeSettings(buildPatch());
      const editableValues = snapshotDraft(resp);
      setValues(editableValues);
      setDraft(editableValues);
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

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative w-[760px] max-w-[92vw] max-h-[92vh] overflow-y-auto bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800"
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
          <GrafxBranding className="mt-2" />
        </div>

        <TabsNav activeTab={activeTab} onChange={setActiveTab} />

        {restartRequired && activeTab === 'graphdb' && (
          <div
            className="px-6 py-2.5 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800/50 text-xs text-amber-900 dark:text-amber-200"
            data-testid="restart-required-banner"
          >
            <strong>Restart required.</strong> Grafx settings are persisted but only
            take effect after restarting the Okto Pulse process
            (Grafx startup-time).
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
              <strong>Per-board lock:</strong> The graph database serializes commits per
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
            onDescriptorChange={(value) => setDraft((current) => ({
              ...current,
              kg_grafx_descriptor_revalidation: value,
            }))}
            providers={graphProviders}
            catalog={grafxCatalog}
            onOptionsChange={(value) => setDraft((current) => ({ ...current, kg_grafx_options: value }))}
          />
        ) : activeTab === 'eventqueue' ? (
          <EventQueueTab
            draft={draft}
            onChange={onInputChange}
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
            disabled={loading || saving || !canWriteRuntime || outOfRange}
            className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg disabled:opacity-50"
            data-testid="save-runtime-settings"
            title={
              !canWriteRuntime
                ? 'Requires runtime.settings.write'
                : undefined
            }
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>


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
            Grafx
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
  onChange: (key: NumericSettingKey, raw: string) => void;
}

/**
 * Spec 54399628 (Wave 2 NC f9732afc) — KG decay tick controllability.
 *
 * Three persisted settings with hot-reload via APScheduler.reschedule_job:
 * Tick interval (5min-7d), Staleness threshold (1-365d), and the optional
 * Max age cap (0=no cap, useful for legacy boards).
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
  onChange: (key: NumericSettingKey, raw: string) => void;
  onDescriptorChange: (value: 'strict' | 'generation') => void;
  providers: { board: string; global: string };
  catalog: GrafxSettingDescriptor[];
  onOptionsChange: (value: DraftState['kg_grafx_options']) => void;
}

function GraphDBTab({
  draft,
  onChange,
  onDescriptorChange,
  providers,
  catalog,
  onOptionsChange,
}: GraphDBTabProps) {
  return (
    <div className="px-6 py-5 space-y-4">
      <div
        className="grid grid-cols-2 gap-3 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
        data-testid="grafx-provider-status"
      >
        <span>Board graph: <strong>{formatProvider(providers.board)}</strong></span>
        <span>Global discovery: <strong>{formatProvider(providers.global)}</strong></span>
      </div>

      <GrafxPageSizeField
        value={draft.kg_grafx_page_size}
        onChange={(v) => onChange('kg_grafx_page_size', v)}
      />

      <div>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300 block mb-0.5">
          Descriptor revalidation
          <SettingHelp label="Descriptor revalidation" text={catalog.find((item) => item.name === 'descriptor_revalidation')?.description ?? 'Generation is faster for Pulse-owned directories. Strict checks cached descriptor identities for shared/forensic use. Both preserve WAL, OCC and snapshots. Restart required.'} />
        </label>
        <p className="text-[10px] text-gray-400 mb-1.5">
          Generation is faster for directories exclusively managed by Pulse. Strict revalidates every cached descriptor and is intended for forensic or externally shared directories.
        </p>
        <select
          value={draft.kg_grafx_descriptor_revalidation}
          onChange={(event) => onDescriptorChange(event.target.value as 'strict' | 'generation')}
          data-testid="input-descriptor-revalidation"
          className="w-full text-xs px-2 py-1.5 border rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600"
        >
          <option value="generation">Generation — recommended for Pulse</option>
          <option value="strict">Strict — maximum descriptor checking</option>
        </select>
      </div>

      <SettingField label="Buffer pool per handle (MiB)"
        description="64 MiB default. Multiply by one writer plus the configured readers, and by resident boards. Global handles add their own allowance. This is not total process RAM. Restart required."
        value={draft.kg_grafx_buffer_pool_mb} range={RANGES.kg_grafx_buffer_pool_mb}
        onChange={(v) => onChange('kg_grafx_buffer_pool_mb', v)} testId="input-grafx-buffer-pool-mb" />
      <SettingField label="Board / Global read participants"
        description="Independent read handles per board and for Global Discovery (1–8 each; default 2). More handles allow overlapping snapshot reads but multiply page caches, index caches and descriptors. Global keeps one writer plus this many reader handles. This does not change writer authority. Restart required."
        value={draft.kg_grafx_read_participants} range={RANGES.kg_grafx_read_participants}
        onChange={(v) => onChange('kg_grafx_read_participants', v)} testId="input-grafx-read-participants" />
      <GrafxAdvancedSettings catalog={catalog} value={draft.kg_grafx_options} onChange={onOptionsChange} />

      <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-[10px] text-blue-900 dark:border-blue-800/50 dark:bg-blue-900/20 dark:text-blue-200">
        Page size is fixed for an existing Grafx generation. A changed value applies only to newly created generations after restart; existing bindings keep their recorded geometry. Native WAL recovery remains automatic and fail-closed.
      </div>
    </div>
  );
}

interface EventQueueTabProps {
  draft: DraftState;
  onChange: (key: NumericSettingKey, raw: string) => void;
}

function EventQueueTab({
  draft,
  onChange,
}: EventQueueTabProps) {
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


    </>
  );
}

interface GrafxPageSizeFieldProps {
  value: number;
  onChange: (raw: string) => void;
}

function GrafxPageSizeField({
  value,
  onChange,
}: GrafxPageSizeFieldProps) {
  const currentIndex = Math.max(
    0,
    GRAFX_PAGE_SIZE_OPTIONS.findIndex((option) => option === value),
  );
  const displayed =
    GRAFX_PAGE_SIZE_OPTIONS[currentIndex] ?? GRAFX_PAGE_SIZE_OPTIONS[0];

  return (
    <div>
      <label className="text-xs font-medium text-gray-700 dark:text-gray-200 block mb-0.5">
        Page size
        <SettingHelp label="Page size" text="Physical page size in bytes. Larger pages trade I/O granularity for memory and write amplification. A change applies only to new generations after restart; existing databases keep their stored geometry." />
      </label>
      <p className="text-[10px] text-gray-400 mb-1.5">
        Physical Grafx page geometry. 8192 bytes is the Pulse default.
      </p>
      <div className="flex items-center gap-3">
        <input
          type="range"
          min={0}
          max={GRAFX_PAGE_SIZE_OPTIONS.length - 1}
          step={1}
          value={currentIndex}
          onChange={(e) => {
            const idx = Number(e.target.value);
            onChange(String(GRAFX_PAGE_SIZE_OPTIONS[idx]));
          }}
          data-testid="input-grafx-page-size"
          className="flex-1 accent-blue-600"
        />
        <span className="w-20 text-xs font-semibold tabular-nums text-gray-700 dark:text-gray-100">
          {displayed} bytes
        </span>
      </div>
      <div className="mt-1 flex justify-between text-[9px] text-gray-400">
        {GRAFX_PAGE_SIZE_OPTIONS.map((option) => (
          <span key={option}>{option}</span>
        ))}
      </div>
    </div>
  );
}

function formatProvider(provider: string): string {
  return provider.toLowerCase() === 'grafx' ? 'Okto Grafx' : provider;
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
        <SettingHelp label={label} text={description} />
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
          {range.max === Number.MAX_SAFE_INTEGER ? `≥ ${range.min}` : `${range.min}-${range.max}`}
        </span>
      </div>
    </div>
  );
}
