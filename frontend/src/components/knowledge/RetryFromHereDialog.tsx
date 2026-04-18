/**
 * RetryFromHereDialog — confirmation modal for re-enqueuing a queue entry
 * (spec f33eb9ca, card b5a5cc73). Optional recursive checkbox lets the
 * user fan out to descendant artifacts in the hierarchy.
 */

import { useState } from 'react';
import * as kgApi from '@/services/kg-api';
import type { PendingTreeNode } from '@/services/kg-api';

interface Props {
  boardId: string;
  node: PendingTreeNode;
  onClose: () => void;
  onSuccess?: (response: Awaited<ReturnType<typeof kgApi.retryPending>>) => void;
}

export function RetryFromHereDialog({ boardId, node, onClose, onSuccess }: Props) {
  const [recursive, setRecursive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const queueEntryId = node.queue_entry_id;
  const canRetry = Boolean(queueEntryId);

  async function handleSubmit() {
    if (!queueEntryId) {
      setError('This artifact does not have a queue entry to retry.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const resp = await kgApi.retryPending(boardId, queueEntryId, recursive);
      onSuccess?.(resp);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Retry failed');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="retry-dialog-title"
      data-testid="retry-from-here-dialog"
    >
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl max-w-md w-full p-5">
        <h2 id="retry-dialog-title" className="text-lg font-semibold text-gray-800 dark:text-gray-100">
          Retry consolidation
        </h2>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
          Re-enqueue <span className="font-medium">{node.title}</span> ({node.type}) for the
          consolidation worker. The worker will skip writes when the artifact's content_hash
          is unchanged.
        </p>

        <label className="mt-4 flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
          <input
            type="checkbox"
            checked={recursive}
            onChange={(e) => setRecursive(e.target.checked)}
            data-testid="retry-recursive-checkbox"
            className="mt-1"
          />
          <span>
            Also re-enqueue <strong>descendants</strong> (failed/skipped children).
            <span className="block text-xs text-gray-500">
              Walks the Ideation → Refinement → Spec → Sprint → Card hierarchy below this entry.
            </span>
          </span>
        </label>

        {error && (
          <p className="mt-3 text-sm text-red-600" data-testid="retry-error">
            {error}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-sm rounded-md text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800"
            data-testid="retry-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canRetry || submitting}
            className="px-3 py-1.5 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            data-testid="retry-confirm"
          >
            {submitting ? 'Retrying…' : recursive ? 'Retry with descendants' : 'Retry'}
          </button>
        </div>
      </div>
    </div>
  );
}
