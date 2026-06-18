/**
 * R7 IMP4 — Canonical Partition Integrity Inspector (read-only drilldown).
 *
 * Opened from the aggregate `canonical_partition_integrity` health issue's
 * drill_down_tool. Lists the per-node partition signals (go-forward cognitive
 * holds, historical canonical debt, mixed-evidence deferred, provenance-only
 * observed) that KG Health intentionally keeps OUT of the aggregate.
 *
 * READ-ONLY by contract: there is NO skip / resolve / retry affordance here. An
 * R7 hold/debt is human-only to skip/dismiss, and that lives on the
 * cognitive-readiness surface — never an agent-style button in this inspector.
 */

import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, Layers, RefreshCw, X } from 'lucide-react';

import {
  getCanonicalPartitionIntegrity,
  type CanonicalPartitionIntegrityItem,
  type CanonicalPartitionIntegrityResponse,
} from '@/services/kg-health-api';

interface CanonicalPartitionIntegrityInspectorModalProps {
  boardId: string;
  onClose: () => void;
}

const STATUS_LABEL: Record<string, string> = {
  cognitive_pending: 'Go-forward hold',
  canonical_debt: 'Historical debt',
  mixed_evidence_deferred: 'Mixed (working deferred)',
  provenance_only_observed: 'Provenance-only (observed)',
};

const STATUS_CLASS: Record<string, string> = {
  cognitive_pending: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  canonical_debt: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
  mixed_evidence_deferred: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  provenance_only_observed: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
};

export function CanonicalPartitionIntegrityInspectorModal({
  boardId,
  onClose,
}: CanonicalPartitionIntegrityInspectorModalProps) {
  const [data, setData] = useState<CanonicalPartitionIntegrityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const fresh = await getCanonicalPartitionIntegrity(boardId);
      setData(fresh);
    } catch (err: any) {
      setError(err?.message ?? 'Failed to load canonical partition integrity');
    } finally {
      setLoading(false);
    }
  }, [boardId]);

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
      data-testid="cpi-inspector-modal"
    >
      <div
        className="relative w-[860px] max-w-[94vw] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden flex flex-col max-h-[82vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 pt-5 pb-3 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-white inline-flex items-center gap-2">
              <Layers className="w-4 h-4 text-violet-500" />
              Canonical Partition Integrity
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              Bug-derived canonical Learning without canonical Bug evidence.
              Read-only — R7 holds/debt are human-only to skip/dismiss.
            </p>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={fetchData}
              disabled={loading}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg disabled:opacity-50"
              title="Refresh"
              data-testid="cpi-refresh"
              aria-label="Refresh canonical partition integrity"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg"
              aria-label="Close inspector"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Counts */}
        {data && (
          <div
            className="px-6 py-2 flex flex-wrap gap-2 border-b border-gray-100 dark:border-gray-800"
            data-testid="cpi-counts"
          >
            {Object.entries(data.counts).map(([status, n]) => (
              <span
                key={status}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${
                  STATUS_CLASS[status] ?? 'bg-gray-100 text-gray-600'
                }`}
                data-testid={`cpi-count-${status}`}
              >
                {STATUS_LABEL[status] ?? status}: {n}
              </span>
            ))}
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-auto">
          {loading && !data && (
            <div className="px-6 py-8 space-y-3" data-testid="cpi-skeleton">
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  className="h-6 bg-gray-100 dark:bg-gray-800 rounded animate-pulse"
                />
              ))}
            </div>
          )}

          {error && (
            <div className="px-6 py-8 text-center" data-testid="cpi-error">
              <p className="text-sm text-rose-600 dark:text-rose-400 mb-3">{error}</p>
              <button
                type="button"
                onClick={fetchData}
                className="px-3 py-1.5 text-xs rounded-lg bg-blue-600 hover:bg-blue-700 text-white"
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !error && data && data.items.length === 0 && (
            <div className="py-12 text-center" data-testid="cpi-empty-state">
              <div className="inline-flex items-center justify-center w-14 h-14 rounded-full bg-emerald-100 dark:bg-emerald-900/30 mb-3">
                <CheckCircle2 className="w-7 h-7 text-emerald-600 dark:text-emerald-400" />
              </div>
              <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-1">
                No partition-integrity signals
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Every canonical Learning has canonical Bug evidence.
              </p>
            </div>
          )}

          {!error && data && data.items.length > 0 && (
            <table className="w-full text-xs" data-testid="cpi-table">
              <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
                <tr className="text-left text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Reason</th>
                  <th className="px-4 py-2 font-medium">Source</th>
                  <th className="px-4 py-2 font-medium">Layer</th>
                  <th className="px-4 py-2 font-medium">Canonical°</th>
                  <th className="px-4 py-2 font-medium">Working endpoints</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
                {data.items.map((item, idx) => (
                  <CPIRow key={`${item.node_id ?? item.source_artifact_ref}-${idx}`} item={item} />
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-2 border-t border-gray-200 dark:border-gray-800 flex items-center justify-between text-[10px] text-gray-500 dark:text-gray-400">
          <span>{data ? `Showing ${data.items.length} of ${data.total} signals` : ''}</span>
          <span>Read-only · skip/dismiss is human-only</span>
        </div>
      </div>
    </div>
  );
}

function CPIRow({ item }: { item: CanonicalPartitionIntegrityItem }) {
  return (
    <tr className="hover:bg-gray-50 dark:hover:bg-gray-800/50" data-testid="cpi-row">
      <td className="px-4 py-2">
        <span
          className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ${
            STATUS_CLASS[item.status] ?? 'bg-gray-100 text-gray-600'
          }`}
          data-testid={`cpi-status-${item.status}`}
        >
          {STATUS_LABEL[item.status] ?? item.status}
        </span>
      </td>
      <td
        className="px-4 py-2 font-mono text-gray-600 dark:text-gray-300 truncate max-w-[220px]"
        title={item.reason_code}
        data-testid="cpi-reason-code"
      >
        {item.reason_code}
      </td>
      <td
        className="px-4 py-2 text-gray-900 dark:text-white truncate max-w-[200px]"
        title={item.source_artifact_ref}
      >
        {item.source_artifact_ref}
      </td>
      <td className="px-4 py-2 text-gray-500 dark:text-gray-400">{item.graph_layer}</td>
      <td className="px-4 py-2 text-gray-500 dark:text-gray-400">{item.canonical_degree}</td>
      <td
        className="px-4 py-2 font-mono text-gray-500 dark:text-gray-400 truncate max-w-[200px]"
        title={item.working_endpoint_refs.join(', ')}
      >
        {item.working_endpoint_refs.length > 0
          ? item.working_endpoint_refs.join(', ')
          : '—'}
      </td>
    </tr>
  );
}
