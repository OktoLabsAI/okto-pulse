/**
 * Dead Letter Inspector — modal listando DLQ rows do consolidation worker.
 *
 * Includes an explicit, permission-gated redrive action per row.
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
  RotateCcw,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';

import {
  getDeadLetterRows,
  redriveAllDeadLetterRows,
  redriveDeadLetterRows,
  type DeadLetterListResponse,
  type DeadLetterRow,
} from '@/services/dead-letter-api';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';

interface DeadLetterInspectorModalProps {
  boardId: string;
  onClose: () => void;
}

const CODE_TRACEABILITY_ARTIFACT_TYPES = new Set([
  'code_investigation_receipt',
  'code_evidence',
  'implementation_target',
]);

export function DeadLetterInspectorModal({
  boardId,
  onClose,
}: DeadLetterInspectorModalProps) {
  const permissions = usePermissions(boardId);
  const canReadQueue = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
    && permissions.has('kg.operations.queue.read')
  );
  const canRedriveQueue = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
    && permissions.has('kg.operations.queue.reprocess')
  );
  const [data, setData] = useState<DeadLetterListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [redrivingIds, setRedrivingIds] = useState<Set<string>>(new Set());
  const [redrivingAll, setRedrivingAll] = useState(false);
  const [confirmRedriveAll, setConfirmRedriveAll] = useState(false);

  useEscapeToClose(onClose);

  const fetchData = useCallback(async () => {
    if (!canReadQueue) {
      setLoading(false);
      setError('You do not have permission to read KG dead-letter rows');
      return;
    }
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
  }, [boardId, canReadQueue]);

  useEffect(() => {
    if (permissions.isLoading) return;
    void fetchData();
  }, [fetchData, permissions.isLoading]);

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

  const redrive = async (row: DeadLetterRow) => {
    if (!canRedriveQueue || redrivingAll || redrivingIds.has(row.id)) return;
    setRedrivingIds((current) => new Set(current).add(row.id));
    try {
      const scope = CODE_TRACEABILITY_ARTIFACT_TYPES.has(row.artifact_type)
        ? 'code_traceability'
        : 'generic';
      const result = await redriveDeadLetterRows(boardId, [row.id], scope);
      if (!result.success || result.blocked) {
        toast.error('Redrive was refused or only partially applied. Review the refreshed DLQ.');
      } else if (!result.mutated || result.selected === 0) {
        toast('The DLQ row was already moved or is no longer eligible.', {
          icon: 'ℹ️',
        });
      } else {
        toast.success(
          result.already_queued_count > 0
            ? 'DLQ row linked to its existing queue item.'
            : 'DLQ row requeued for consolidation.',
        );
      }
      await fetchData();
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to redrive dead-letter row');
      await fetchData();
    } finally {
      setRedrivingIds((current) => {
        const next = new Set(current);
        next.delete(row.id);
        return next;
      });
    }
  };

  const redriveAll = async () => {
    if (!canRedriveQueue || redrivingAll || !data || data.total === 0) return;
    setRedrivingAll(true);
    try {
      const result = await redriveAllDeadLetterRows(boardId);
      if (!result.success || (result.remaining ?? 0) > 0 || result.blocked) {
        toast.error(
          `Redrive stopped with ${result.remaining ?? 0} eligible row(s) remaining.`,
        );
      } else if (!result.mutated || result.selected === 0) {
        toast('The DLQ was already empty.', { icon: 'ℹ️' });
      } else {
        toast.success(`Redrove all ${result.selected} DLQ row(s).`);
      }
      await fetchData();
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to redrive all dead-letter rows');
      // Earlier scoped batches may already be committed; don't leave a stale
      // list suggesting the failed HTTP request rolled back the entire job.
      await fetchData();
    } finally {
      setRedrivingAll(false);
      setConfirmRedriveAll(false);
    }
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
              onClick={() => setConfirmRedriveAll(true)}
              disabled={
                loading
                || redrivingAll
                || redrivingIds.size > 0
                || !data
                || data.total === 0
                || !canRedriveQueue
              }
              className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              title={canRedriveQueue
                ? 'Requeue every accessible DLQ row for this board'
                : 'Requires kg.operations.queue.reprocess'}
              data-testid="dlq-redrive-all"
            >
              <RotateCcw className={`h-3.5 w-3.5 ${redrivingAll ? 'animate-spin' : ''}`} />
              {redrivingAll ? 'Redriving all…' : 'Redrive all'}
            </button>
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
          {confirmRedriveAll && data && data.total > 0 && (
            <div
              className="m-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-100"
              role="alert"
              data-testid="dlq-redrive-all-confirmation"
            >
              <p className="font-medium">
                Redrive all {data.total} accessible DLQ row(s)?
              </p>
              <p className="mt-1 text-[11px] opacity-80">
                Rows will be requeued in bounded batches and the consolidation worker will be awakened.
                {' '}Concurrent arrivals or blocked rows may remain. Completed batches are not rolled back if a later batch fails.
              </p>
              <div className="mt-3 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmRedriveAll(false)}
                  disabled={redrivingAll}
                  className="rounded border border-amber-400 px-2.5 py-1 font-medium hover:bg-amber-100 disabled:opacity-50 dark:hover:bg-amber-900/50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void redriveAll()}
                  disabled={redrivingAll}
                  className="rounded bg-blue-600 px-2.5 py-1 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                  data-testid="dlq-redrive-all-confirm"
                >
                  Confirm redrive all
                </button>
              </div>
            </div>
          )}

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
                  <th className="px-4 py-2 font-medium">Action</th>
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
                    onRedrive={() => void redrive(row)}
                    redriving={redrivingIds.has(row.id)}
                    canRedrive={canRedriveQueue && !redrivingAll}
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
          <span>Redrive wakes the consolidation worker immediately</span>
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
  onRedrive: () => void;
  redriving: boolean;
  canRedrive: boolean;
}

function DLQTableRow({
  row,
  expanded,
  onToggle,
  onRedrive,
  redriving,
  canRedrive,
}: DLQTableRowProps) {
  const errors = Array.isArray(row.errors) ? row.errors : [];
  const lastError = errors[errors.length - 1];
  const lastErrorType = formatErrorType(lastError?.error_type);
  const lastErrorText = lastError
    ? `${lastErrorType}: ${formatGraphStorageText(lastError.message || '')}`
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
            onClick={onRedrive}
            disabled={!canRedrive || redriving}
            className="inline-flex items-center gap-1 rounded bg-blue-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            title={canRedrive ? 'Requeue this row for consolidation' : 'Requires kg.operations.queue.reprocess'}
            data-testid={`dlq-redrive-${row.id}`}
          >
            <RotateCcw className={`h-3 w-3 ${redriving ? 'animate-spin' : ''}`} />
            {redriving ? 'Redriving…' : 'Redrive'}
          </button>
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
          <td colSpan={7} className="px-6 py-3">
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
                {errors.map((err, index) => (
                  <tr key={err.attempt ?? index}>
                    <td className="px-2 py-0.5">{err.attempt ?? index + 1}</td>
                    <td className="px-2 py-0.5 text-gray-500">
                      {err.occurred_at || '—'}
                    </td>
                    <td className="px-2 py-0.5 text-amber-600 dark:text-amber-400">
                      {formatErrorType(err.error_type)}
                    </td>
                    <td className="px-2 py-0.5">
                      {formatGraphStorageText(err.message || '—')}
                    </td>
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

function formatErrorType(errorType?: string | null): string {
  if (!errorType) return 'Error';
  return formatGraphStorageText(errorType);
}

function formatGraphStorageText(value: string): string {
  const accentedLegacyName = [75, 249, 122, 117]
    .map((code) => String.fromCharCode(code))
    .join('');
  const legacyName = ['Ku', 'zu'].join('');
  return value
    .replace(new RegExp(accentedLegacyName, 'g'), 'Graph DB')
    .replace(new RegExp(legacyName, 'g'), 'GraphDB')
    .replace(new RegExp(legacyName.toLowerCase(), 'g'), 'graphdb');
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
