import { useEffect, useMemo, useState } from 'react';
import { Download, FolderOpen, RotateCcw, Save, Trash2, X } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  CURRENT_METRICS_SCHEMA_VERSION,
  exportLocalMetrics,
  getMetricsSummary,
  purgeLocalMetrics,
  updateMetricsMode,
  type MetricsMode,
  type MetricsSummary,
} from '@/services/metrics-api';

interface MetricsSettingsPanelProps {
  onClose: () => void;
  initialPrompt?: boolean;
}

const ACK_ITEMS = [
  {
    id: 'schema',
    label: `Telemetry schema ${CURRENT_METRICS_SCHEMA_VERSION} reviewed`,
    description: 'Only the documented anonymous aggregate fields are eligible for upload.',
  },
  {
    id: 'privacy_policy',
    label: 'Privacy terms reviewed',
    description: 'The beacon is optional and can be turned off from this panel.',
  },
  {
    id: 'hourly_aggregates',
    label: 'Hourly aggregates only',
    description: 'Events are summarized before upload; raw local event files stay on this machine.',
  },
  {
    id: 'product_aggregates',
    label: 'Product aggregates reviewed',
    description: 'Feature usage, flow origins, completion counts, and work item totals are sent only as counts.',
  },
  {
    id: 'no_pii',
    label: 'No PII or project content',
    description: 'Titles, descriptions, board IDs, paths, emails, and payload bodies are not sent.',
  },
  {
    id: 'local_control',
    label: 'Local control retained',
    description: 'You can export, purge, disable, or return to local-only metrics at any time.',
  },
] as const;

const ACK_IDS = ACK_ITEMS.map((item) => item.id);

function modeLabel(mode: MetricsMode): string {
  if (mode === 'anonymous_beacon') return 'Beacon';
  if (mode === 'local_only') return 'Local';
  return 'Off';
}

function normalizeItems(items: string[]): string[] {
  return [...new Set(items)].sort();
}

function sameItems(left: string[], right: string[]): boolean {
  return normalizeItems(left).join('|') === normalizeItems(right).join('|');
}

export function MetricsSettingsPanel({ onClose, initialPrompt = false }: MetricsSettingsPanelProps) {
  const [data, setData] = useState<MetricsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [draftMode, setDraftMode] = useState<MetricsMode>('local_only');
  const [ack, setAck] = useState<string[]>([]);
  const [showChecklistWarning, setShowChecklistWarning] = useState(false);

  const ackComplete = ACK_IDS.every((id) => ack.includes(id));
  const eventTypes = useMemo(() => Object.entries(data?.summary.by_event_type ?? {}), [data]);
  const persistedAck = data?.consent.acknowledged_items ?? [];
  const hasUnsavedChanges = Boolean(data && (draftMode !== data.mode || !sameItems(ack, persistedAck)));

  const refresh = async () => {
    setLoading(true);
    try {
      const summary = await getMetricsSummary();
      setData(summary);
      setDraftMode(summary.mode);
      setAck(summary.consent.acknowledged_items ?? []);
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to load metrics');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const saveSettings = async () => {
    if (!data) return;
    if (draftMode === 'anonymous_beacon' && !ackComplete) {
      setShowChecklistWarning(true);
      toast.error('Check every opt-in item before enabling Beacon');
      return;
    }
    setSaving(true);
    try {
      await updateMetricsMode(draftMode, ack);
      await refresh();
      toast.success('Metrics settings saved');
      if (initialPrompt) onClose();
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to update metrics');
    } finally {
      setSaving(false);
    }
  };

  const onExport = async () => {
    try {
      const result = await exportLocalMetrics();
      toast.success(`Exported: ${result.output_path}`);
    } catch (err: any) {
      toast.error(err?.message ?? 'Export failed');
    }
  };

  const onPurge = async () => {
    if (!confirm('Purge local metrics files?')) return;
    try {
      const result = await purgeLocalMetrics();
      await refresh();
      toast.success(`Purged ${result.purged_files} files`);
    } catch (err: any) {
      toast.error(err?.message ?? 'Purge failed');
    }
  };

  const openFolder = () => {
    if (!data?.metrics_dir) return;
    navigator.clipboard?.writeText(data.metrics_dir).catch(() => undefined);
    toast.success('Metrics path copied');
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end bg-black/20 pt-14" onClick={onClose}>
      <div
        className="mr-4 max-h-[calc(100vh-4rem)] w-[520px] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-lg border border-gray-200 bg-white p-4 shadow-xl dark:border-gray-700 dark:bg-gray-900"
        onClick={(e) => e.stopPropagation()}
        data-testid="metrics-settings-panel"
      >
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              {initialPrompt ? 'Metrics opt-in' : 'Metrics'}
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {data
                ? `Current mode: ${modeLabel(data.mode)}${hasUnsavedChanges ? ` · selected: ${modeLabel(draftMode)}` : ''}`
                : 'Loading'}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-white/10 dark:hover:text-gray-200"
            aria-label="Close metrics settings"
          >
            <X size={18} />
          </button>
        </div>

        {loading && <div className="text-sm text-gray-500 dark:text-gray-400">Loading metrics...</div>}

        {!loading && data && (
          <div className="space-y-5">
            {initialPrompt && (
              <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950 dark:border-blue-900/70 dark:bg-blue-950/30 dark:text-blue-100">
                Okto Pulse starts in local-only mode. You can keep metrics local, disable capture, or opt in to an anonymous hourly beacon.
              </div>
            )}

            <div className="grid grid-cols-3 gap-1 rounded-md bg-gray-100 p-1 dark:bg-gray-800">
              {(['local_only', 'anonymous_beacon', 'disabled'] as MetricsMode[]).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  disabled={saving}
                  onClick={() => {
                    setDraftMode(mode);
                    setShowChecklistWarning(mode === 'anonymous_beacon' && !ackComplete);
                  }}
                  data-testid={`metrics-mode-${mode}`}
                  className={`rounded px-2 py-1.5 text-xs font-medium ${
                    draftMode === mode
                      ? 'bg-white text-blue-700 shadow-sm dark:bg-gray-950 dark:text-blue-300'
                      : 'text-gray-600 hover:bg-white/70 dark:text-gray-300 dark:hover:bg-gray-700'
                  }`}
                >
                  {modeLabel(mode)}
                </button>
              ))}
            </div>

            <div className="grid grid-cols-3 gap-2">
              <div className="rounded-md border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[11px] uppercase text-gray-500">Events</div>
                <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">{data.summary.event_count}</div>
              </div>
              <div className="rounded-md border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[11px] uppercase text-gray-500">Files</div>
                <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">{data.summary.files_count}</div>
              </div>
              <div className="rounded-md border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[11px] uppercase text-gray-500">Days</div>
                <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">{data.retention_days}</div>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-gray-700 dark:text-gray-300">Beacon opt-in checklist</div>
                <button
                  type="button"
                  onClick={() => {
                    setAck([...ACK_IDS]);
                    setShowChecklistWarning(false);
                  }}
                  className="text-xs font-medium text-blue-600 hover:underline dark:text-blue-300"
                >
                  Confirm all
                </button>
              </div>
              {showChecklistWarning && !ackComplete && (
                <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-200">
                  Check every item below to confirm the anonymous beacon requirements.
                </div>
              )}
              <div className="space-y-2">
                {ACK_ITEMS.map((item) => (
                  <label
                    key={item.id}
                    className={`flex gap-3 rounded-md border p-3 text-left ${
                      ack.includes(item.id)
                        ? 'border-blue-500 bg-blue-50 dark:bg-blue-950/40'
                        : 'border-gray-200 dark:border-gray-700'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={ack.includes(item.id)}
                      onChange={() => {
                        setAck((items) =>
                          items.includes(item.id)
                            ? items.filter((x) => x !== item.id)
                            : [...items, item.id],
                        );
                        setShowChecklistWarning(false);
                      }}
                      className="mt-0.5 h-4 w-4 rounded border-gray-300 text-blue-600"
                    />
                    <span className="min-w-0">
                      <span className="block text-xs font-medium text-gray-800 dark:text-gray-100">
                        {item.label}
                      </span>
                      <span className="mt-0.5 block text-xs leading-5 text-gray-500 dark:text-gray-400">
                        {item.description}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            <div className="space-y-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="text-gray-500">Path</span>
                <button type="button" onClick={openFolder} className="flex min-w-0 items-center gap-1 text-blue-600 hover:underline">
                  <FolderOpen size={14} />
                  <span className="truncate">{data.metrics_dir}</span>
                </button>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Schema</span>
                <span className="font-mono text-gray-700 dark:text-gray-300">{data.schema_version}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Beacon</span>
                <span className="text-gray-700 dark:text-gray-300">{data.beacon_status.schema_status}</span>
              </div>
              {data.next_opt_in_prompt_after && (
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Prompt after</span>
                  <span className="text-gray-700 dark:text-gray-300">{data.next_opt_in_prompt_after.slice(0, 10)}</span>
                </div>
              )}
            </div>

            {eventTypes.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-gray-700 dark:text-gray-300">Event types</div>
                {eventTypes.map(([type, count]) => (
                  <div key={type} className="flex items-center justify-between rounded bg-gray-50 px-2 py-1 text-xs dark:bg-gray-800">
                    <span className="text-gray-600 dark:text-gray-300">{type}</span>
                    <span className="font-medium text-gray-900 dark:text-gray-100">{count}</span>
                  </div>
                ))}
              </div>
            )}

            {data.product_aggregate_families.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-gray-700 dark:text-gray-300">Product aggregates</div>
                <div className="flex flex-wrap gap-1">
                  {data.product_aggregate_families.map((family) => (
                    <span
                      key={family}
                      className="rounded bg-gray-50 px-2 py-1 font-mono text-[11px] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                    >
                      {family.replace(/^product_/, '')}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-gray-200 pt-3 dark:border-gray-700">
              <button type="button" onClick={refresh} className="btn btn-secondary flex items-center gap-1 text-xs">
                <RotateCcw size={14} />
                Refresh
              </button>
              <button type="button" onClick={onExport} className="btn btn-secondary flex items-center gap-1 text-xs">
                <Download size={14} />
                Export
              </button>
              <button type="button" onClick={onPurge} className="btn btn-secondary flex items-center gap-1 text-xs text-red-600">
                <Trash2 size={14} />
                Purge
              </button>
              <button
                type="button"
                onClick={saveSettings}
                disabled={saving || !hasUnsavedChanges}
                data-testid="metrics-save"
                className="btn btn-primary flex items-center gap-1 text-xs"
              >
                <Save size={14} />
                {saving ? 'Saving...' : 'Save'}
              </button>
              {initialPrompt && (
                <button type="button" onClick={onClose} className="btn btn-secondary text-xs">
                  Not now
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
