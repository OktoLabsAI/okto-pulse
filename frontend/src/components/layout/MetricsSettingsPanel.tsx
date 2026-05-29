import { useEffect, useRef, useState } from 'react';
import { Info, RotateCcw, Save, X } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  CURRENT_METRICS_SCHEMA_VERSION,
  getMetricsSummary,
  markMetricsMigrationNoticeSeen,
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
    description: 'Metrics sharing is optional and can be turned off from this panel.',
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
    label: 'User control retained',
    description: 'You can turn metrics off at any time.',
  },
] as const;

const ACK_IDS = ACK_ITEMS.map((item) => item.id);

function modeLabel(mode: MetricsMode): string {
  return mode === 'anonymous_beacon' ? 'On' : 'Off';
}

export function MetricsSettingsPanel({ onClose, initialPrompt = false }: MetricsSettingsPanelProps) {
  const [data, setData] = useState<MetricsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [draftMode, setDraftMode] = useState<MetricsMode>('disabled');
  const shownMigrationNotices = useRef<Set<string>>(new Set());

  const hasUnsavedChanges = Boolean(data && draftMode !== data.mode);

  const refresh = async () => {
    setLoading(true);
    try {
      const summary = await getMetricsSummary();
      setData(summary);
      setDraftMode(summary.ui_mode === 'on' ? 'anonymous_beacon' : 'disabled');
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to load metrics');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    const notice = data?.migration_notice;
    if (!notice?.pending || notice.type !== 'local_only_to_disabled') return;
    if (shownMigrationNotices.current.has(notice.type)) return;
    shownMigrationNotices.current.add(notice.type);
    toast('Metrics were turned off');
    markMetricsMigrationNoticeSeen(notice.type).catch((err: any) => {
      shownMigrationNotices.current.delete(notice.type);
      toast.error(err?.message ?? 'Failed to confirm metrics notice');
    });
  }, [data?.migration_notice]);

  const saveSettings = async () => {
    if (!data) return;
    setSaving(true);
    try {
      await updateMetricsMode(draftMode, draftMode === 'anonymous_beacon' ? ACK_IDS : []);
      await refresh();
      toast.success('Metrics settings saved');
      if (initialPrompt) onClose();
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to update metrics');
    } finally {
      setSaving(false);
    }
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
                ? `Current setting: ${modeLabel(data.mode)}${hasUnsavedChanges ? ` · selected: ${modeLabel(draftMode)}` : ''}`
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
                Okto Pulse starts with metrics off. You can turn on anonymous hourly aggregates here.
              </div>
            )}

            <div className="flex items-center justify-between rounded-lg border border-gray-200 p-4 dark:border-gray-700">
              <div>
                <div className="text-sm font-medium text-gray-900 dark:text-gray-100">Send metrics</div>
                <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">{modeLabel(draftMode)}</div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={draftMode === 'anonymous_beacon'}
                disabled={saving}
                data-testid="metrics-on-off-toggle"
                onClick={() => {
                  const nextMode = draftMode === 'anonymous_beacon' ? 'disabled' : 'anonymous_beacon';
                  setDraftMode(nextMode);
                }}
                className={`relative h-7 w-12 rounded-full transition ${
                  draftMode === 'anonymous_beacon'
                    ? 'bg-blue-600'
                    : 'bg-gray-300 dark:bg-gray-700'
                }`}
              >
                <span
                  className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow transition ${
                    draftMode === 'anonymous_beacon' ? 'left-6' : 'left-1'
                  }`}
                />
              </button>
            </div>

            <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700" data-testid="metrics-scope">
              <div className="text-xs font-medium text-gray-900 dark:text-gray-100">Metrics scope</div>
              <div className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
                All eligible anonymous aggregate metrics are included when sending is on.
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-gray-700 dark:text-gray-300">Anonymous metrics included</div>
              </div>
              <div className="space-y-2">
                {ACK_ITEMS.map((item) => (
                  <div
                    key={item.id}
                    className="flex gap-3 rounded-md border border-gray-200 p-3 text-left dark:border-gray-700"
                  >
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center text-blue-600 dark:text-blue-300">
                      <Info size={14} aria-hidden="true" />
                    </span>
                    <span className="min-w-0">
                      <span className="block text-xs font-medium text-gray-800 dark:text-gray-100">
                        {item.label}
                      </span>
                      <span className="mt-0.5 block text-xs leading-5 text-gray-500 dark:text-gray-400">
                        {item.description}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Schema</span>
                <span className="font-mono text-gray-700 dark:text-gray-300">{data.schema_version}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Consent status</span>
                <span className="text-gray-700 dark:text-gray-300">{data.beacon_status.schema_status}</span>
              </div>
              {data.next_opt_in_prompt_after && (
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Prompt after</span>
                  <span className="text-gray-700 dark:text-gray-300">{data.next_opt_in_prompt_after.slice(0, 10)}</span>
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-gray-200 pt-3 dark:border-gray-700">
              <button type="button" onClick={refresh} className="btn btn-secondary flex items-center gap-1 text-xs">
                <RotateCcw size={14} />
                Refresh
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
