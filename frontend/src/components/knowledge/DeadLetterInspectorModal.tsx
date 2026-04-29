/**
 * Dead Letter Inspector — modal listando DLQ rows do consolidation worker.
 *
 * Spec ed17b1fe (Wave 2 NC 1ede3471). Read-only MVP — sem reprocess.
 *
 * Estados:
 *  - Loading skeleton (durante fetch inicial e refresh)
 *  - Empty state (CheckCircle verde) quando rows = []
 *  - Error panel + Retry quando fetch falha
 *  - Tabela com expand row para errors[] history
 */

import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  X,
} from 'lucide-react';

import {
  getDeadLetterRows,
  type DeadLetterListResponse,
  type DeadLetterRow,
} from '@/services/dead-letter-api';

interface DeadLetterInspectorModalProps {
  boardId: string;
  onClose: () => void;
}

export function DeadLetterInspectorModal({
  boardId,
  onClose,
}: DeadLetterInspectorModalProps) {
  const [data, setData] = useState<DeadLetterListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const fresh = await getDeadLetterRows(boardId);
      setData(fresh);
    } catch (err: any) {
      setError(err?.message ?? 'Failed to load dead-letter rows');
    } finally {
      setLoading(false);
    }
  }, [boardId]);

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  const toggleExpand = (rowId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(rowId)) {
        next.delete(rowId);
      } else {
        next.add(rowId);
      }
      return next;
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
      data-testid="dead-letter-inspector-modal"
    >
      <div
        className="relative w-[768px] max-w-[92vw] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden flex flex-col max-h-[80vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 pt-5 pb-3 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-white inline-flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-500" />
              Dead Letter Inspector
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              Consolidation rows that failed all retry attempts
            </p>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={fetchData}
              disabled={loading}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg disabled:opacity-50"
              title="Refresh"
              data-testid="dlq-refresh"
              aria-label="Refresh dead-letter list"
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

        {/* Body */}
        <div className="flex-1 overflow-auto">
          {loading && !data && <SkeletonRows />}

          {error && (
            <div className="px-6 py-8 text-center" data-testid="dlq-error">
              <p className="text-sm text-rose-600 dark:text-rose-400 mb-3">
                {error}
              </p>
              <button
                type="button"
                onClick={fetchData}
                className="px-3 py-1.5 text-xs rounded-lg bg-blue-600 hover:bg-blue-700 text-white"
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !error && data && data.rows.length === 0 && (
            <EmptyState />
          )}

          {!error && data && data.rows.length > 0 && (
            <table className="w-full text-xs" data-testid="dlq-table">
              <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
                <tr className="text-left text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                  <th className="px-4 py-2 font-medium">ID</th>
                  <th className="px-4 py-2 font-medium">Artifact</th>
                  <th className="px-4 py-2 font-medium">Attempts</th>
                  <th className="px-4 py-2 font-medium">Last error</th>
                  <th className="px-4 py-2 font-medium">Dead-lettered</th>
                  <th className="px-4 py-2 font-medium w-12"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
                {data.rows.map((row) => (
                  <DLQTableRow
                    key={row.id}
                    row={row}
                    expanded={expandedIds.has(row.id)}
                    onToggle={() => toggleExpand(row.id)}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-2 border-t border-gray-200 dark:border-gray-800 flex items-center justify-between text-[10px] text-gray-500 dark:text-gray-400">
          <span>
            {data
              ? `Showing ${data.rows.length} of ${data.total} dead-lettered rows`
              : ''}
          </span>
          <span>Reprocess deferred to v2</span>
        </div>
      </div>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="px-6 py-8 space-y-3" data-testid="dlq-skeleton">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-6 bg-gray-100 dark:bg-gray-800 rounded animate-pulse"
        />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="py-12 text-center" data-testid="dlq-empty-state">
      <div className="inline-flex items-center justify-center w-14 h-14 rounded-full bg-emerald-100 dark:bg-emerald-900/30 mb-3">
        <CheckCircle2 className="w-7 h-7 text-emerald-600 dark:text-emerald-400" />
      </div>
      <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-1">
        No dead-lettered rows
      </h3>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Pipeline healthy — all consolidation attempts have succeeded.
      </p>
    </div>
  );
}

interface DLQTableRowProps {
  row: DeadLetterRow;
  expanded: boolean;
  onToggle: () => void;
}

function DLQTableRow({ row, expanded, onToggle }: DLQTableRowProps) {
  const lastError = row.errors[row.errors.length - 1];
  const lastErrorText = lastError
    ? `${lastError.error_type}: ${lastError.message}`
    : '—';
  const deadLetteredRel = row.dead_lettered_at
    ? formatRelative(row.dead_lettered_at)
    : '—';

  return (
    <>
      <tr className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
        <td
          className="px-4 py-2 font-mono text-gray-600 dark:text-gray-300 truncate max-w-[120px]"
          title={row.id}
        >
          {row.id}
        </td>
        <td
          className="px-4 py-2 text-gray-900 dark:text-white truncate max-w-[200px]"
          title={`${row.artifact_type}:${row.artifact_id}`}
        >
          {row.artifact_type}:{row.artifact_id.slice(0, 8)}…
        </td>
        <td className="px-4 py-2">
          <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
            {row.attempts}
          </span>
        </td>
        <td
          className="px-4 py-2 font-mono text-rose-600 dark:text-rose-400 truncate max-w-[200px]"
          title={lastErrorText}
        >
          {lastErrorText}
        </td>
        <td className="px-4 py-2 text-gray-500 dark:text-gray-400">
          {deadLetteredRel}
        </td>
        <td className="px-4 py-2">
          <button
            type="button"
            onClick={onToggle}
            className="p-1 text-gray-400 hover:text-blue-600"
            aria-label={expanded ? 'Collapse history' : 'Expand history'}
            data-testid={`dlq-expand-${row.id}`}
          >
            {expanded ? (
              <ChevronDown className="w-3.5 h-3.5" />
            ) : (
              <ChevronRight className="w-3.5 h-3.5" />
            )}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50 dark:bg-gray-800/30">
          <td colSpan={6} className="px-6 py-3">
            <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
              Attempt history
            </div>
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-left text-gray-400">
                  <th className="px-2 py-1 font-medium">#</th>
                  <th className="px-2 py-1 font-medium">When</th>
                  <th className="px-2 py-1 font-medium">Type</th>
                  <th className="px-2 py-1 font-medium">Message</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {row.errors.map((err) => (
                  <tr key={err.attempt}>
                    <td className="px-2 py-0.5">{err.attempt}</td>
                    <td className="px-2 py-0.5 text-gray-500">
                      {err.occurred_at}
                    </td>
                    <td className="px-2 py-0.5 text-amber-600 dark:text-amber-400">
                      {err.error_type}
                    </td>
                    <td className="px-2 py-0.5">{err.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}

function formatRelative(iso: string): string {
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const diffMs = now - then;
    const sec = Math.floor(diffMs / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const d = Math.floor(hr / 24);
    return `${d}d ago`;
  } catch {
    return iso;
  }
}
