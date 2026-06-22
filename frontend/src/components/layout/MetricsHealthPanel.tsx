import { useEffect, useState } from 'react';
import { Activity, AlertTriangle, RefreshCw, ShieldCheck, X } from 'lucide-react';

import {
  getPublishHealth,
  HEALTH_SOURCE_UNAVAILABLE,
  type PublishHealth,
  type PublishHealthSource,
} from '@/services/metrics-health-api';

interface MetricsHealthPanelProps {
  onClose: () => void;
}

// Presentation only — the visual style is derived from dto.status, NEVER recomputed.
// `degraded`/`stale`/`unavailable` map to amber/red so a gap is never shown green-ok.
const STATUS_STYLES: Record<string, { badge: string; dot: string }> = {
  healthy: { badge: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300', dot: 'bg-green-500' },
  recovering: { badge: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300', dot: 'bg-blue-500' },
  degraded: { badge: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300', dot: 'bg-amber-500' },
  stale: { badge: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300', dot: 'bg-amber-500' },
  failing: { badge: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300', dot: 'bg-red-500' },
  disabled: { badge: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300', dot: 'bg-gray-400' },
  unavailable: { badge: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300', dot: 'bg-gray-400' },
};
const FALLBACK_STYLE = { badge: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300', dot: 'bg-gray-400' };

function statusStyle(status: string | undefined) {
  return (status && STATUS_STYLES[status]) || FALLBACK_STYLE;
}

function fmt(value: string | null | undefined): string {
  return value && typeof value === 'string' ? value : '—';
}

function fmtAge(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || Number.isNaN(seconds)) return '—';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function Field({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="flex items-center justify-between gap-2 py-0.5">
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
      <span className="text-xs font-medium text-gray-900 dark:text-gray-100" data-testid={testId}>
        {value}
      </span>
    </div>
  );
}

function SourceRow({ source }: { source: PublishHealthSource }) {
  const style = statusStyle(source?.status);
  return (
    <div
      className="flex items-start justify-between gap-2 border-t border-gray-100 py-1.5 dark:border-gray-800"
      data-testid={`health-source-${source?.name ?? 'unknown'}`}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className={`inline-block h-2 w-2 rounded-full ${style.dot}`} />
          <span className="text-xs font-medium text-gray-900 dark:text-gray-100">{fmt(source?.name)}</span>
          <span className="text-[11px] text-gray-500 dark:text-gray-400">({fmt(source?.status)})</span>
        </div>
        <p className="mt-0.5 text-[11px] leading-snug text-gray-500 dark:text-gray-400">{fmt(source?.message)}</p>
      </div>
      <span className="shrink-0 text-[11px] text-gray-400">{fmt(source?.last_success_at)}</span>
    </div>
  );
}

export function MetricsHealthPanel({ onClose }: MetricsHealthPanelProps) {
  const [data, setData] = useState<PublishHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const health = await getPublishHealth();
      setData(health);
    } catch (err: any) {
      setError(err?.message ?? 'Failed to load publish health');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  // status is the SOURCE OF TRUTH — rendered verbatim, never recomputed/upgraded.
  const status = data?.status ?? (data?.error ? 'unavailable' : 'unknown');
  const style = statusStyle(status);
  const sources: PublishHealthSource[] = Array.isArray(data?.sources) ? data!.sources! : [];
  const sourceUnavailable = data?.error === HEALTH_SOURCE_UNAVAILABLE;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end bg-black/20 pt-14" onClick={onClose}>
      <div
        className="mr-4 max-h-[calc(100vh-4rem)] w-[520px] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-lg border border-gray-200 bg-white p-4 shadow-xl dark:border-gray-700 dark:bg-gray-900"
        onClick={(e) => e.stopPropagation()}
        data-testid="metrics-health-panel"
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
            <Activity size={15} />
            Metrics Publish Health
          </h2>
          <div className="flex items-center gap-1">
            <button
              onClick={refresh}
              className="rounded p-1 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
              title="Refresh"
              data-testid="health-refresh"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : undefined} />
            </button>
            <button
              onClick={onClose}
              className="rounded p-1 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
              title="Close"
              data-testid="health-close"
            >
              <X size={14} />
            </button>
          </div>
        </div>

        {loading && !data ? (
          <p className="py-6 text-center text-xs text-gray-500 dark:text-gray-400" data-testid="health-loading">
            Loading publish health…
          </p>
        ) : error ? (
          <div className="flex items-start gap-2 rounded border border-red-200 bg-red-50 p-2 dark:border-red-900/50 dark:bg-red-900/20" data-testid="health-error">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-red-600 dark:text-red-400" />
            <p className="text-xs text-red-700 dark:text-red-300">{error}</p>
          </div>
        ) : (
          <>
            <div className="mb-2 flex items-center gap-2">
              <span
                className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-xs font-semibold uppercase ${style.badge}`}
                data-testid="health-status"
              >
                <span className={`inline-block h-2 w-2 rounded-full ${style.dot}`} />
                {status}
              </span>
              {data?.severity ? (
                <span className="text-[11px] text-gray-500 dark:text-gray-400" data-testid="health-severity">
                  severity: {data.severity}
                </span>
              ) : null}
            </div>

            {/* next action / guidance — the redacted classifier message, not recomputed */}
            <p className="mb-3 text-xs leading-snug text-gray-700 dark:text-gray-300" data-testid="health-message">
              {fmt(data?.message)}
            </p>

            {sourceUnavailable ? (
              <p className="mb-3 rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800 dark:border-amber-900/50 dark:bg-amber-900/20 dark:text-amber-300" data-testid="health-source-unavailable">
                No publish-health source could be read; status cannot be confirmed.
              </p>
            ) : null}

            <div className="rounded border border-gray-100 p-2 dark:border-gray-800">
              <Field label="Reason" value={fmt(data?.reason_code) === '—' ? fmt(data?.reason_category) : fmt(data?.reason_code)} testId="health-reason" />
              <Field label="Last success" value={fmt(data?.last_success_at)} testId="health-last-success" />
              <Field label="Last failure" value={fmt(data?.last_failure_at)} testId="health-last-failure" />
              <Field label="Next retry" value={fmt(data?.next_retry_at)} testId="health-next-retry" />
              <Field label="Retry count" value={String(data?.retry_count ?? 0)} testId="health-retry-count" />
              <Field label="Freshness" value={fmtAge(data?.freshness?.age_seconds)} testId="health-freshness" />
              {data?.freshness?.is_stale ? (
                <Field label="Freshness state" value="stale" />
              ) : null}
            </div>

            <div className="mt-3">
              <h3 className="mb-1 text-xs font-semibold text-gray-700 dark:text-gray-300">Sources</h3>
              {sources.length === 0 ? (
                <p className="text-[11px] text-gray-400" data-testid="health-no-sources">No source signals.</p>
              ) : (
                <div data-testid="health-sources">
                  {sources.map((source, idx) => (
                    <SourceRow key={`${source?.name ?? 'source'}-${idx}`} source={source} />
                  ))}
                </div>
              )}
            </div>

            <div className="mt-3 flex items-center gap-1.5 border-t border-gray-100 pt-2 text-[11px] text-gray-400 dark:border-gray-800">
              <ShieldCheck size={12} />
              <span data-testid="health-install-id">install: {fmt(data?.install_id_redacted)}</span>
              <span className="ml-auto">secrets are never shown</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
