/**
 * CancellationReasonDialog — shared confirmation dialog that captures the
 * MANDATORY justification before an item (ideation, refinement, spec, sprint
 * or card) is moved to 'cancelled' (ITEM 17).
 *
 * Also exports CancellationDetails — the shared renderer used by the
 * "Cancellation" tab of the detail modals (reason as markdown + who + when).
 */

import { useEffect, useState } from 'react';
import { Ban } from 'lucide-react';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

interface CancellationReasonDialogProps {
  open: boolean;
  /** Human label of the entity being cancelled, e.g. "spec" or "card". */
  entityLabel: string;
  submitting?: boolean;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

export function CancellationReasonDialog({
  open,
  entityLabel,
  submitting = false,
  onConfirm,
  onCancel,
}: CancellationReasonDialogProps) {
  const [reason, setReason] = useState('');

  // Reset the textarea every time the dialog opens.
  useEffect(() => {
    if (open) setReason('');
  }, [open]);

  if (!open) return null;

  const trimmed = reason.trim();

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[80] p-4">
      <div
        role="dialog"
        aria-label="Cancellation reason required"
        className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-lg flex flex-col"
      >
        <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2">
          <Ban size={18} className="text-red-500 shrink-0" />
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Cancellation Reason Required</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
              Provide a justification before cancelling this {entityLabel}.
            </p>
          </div>
        </div>
        <div className="px-6 py-4">
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why is this being cancelled? (markdown supported)"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 dark:text-gray-100 resize-none"
            rows={5}
            autoFocus
            data-testid="cancellation-reason-input"
          />
          {!trimmed && (
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
              A non-empty reason is required to cancel this {entityLabel}.
            </p>
          )}
        </div>
        <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={submitting}
            className="btn btn-secondary"
            data-testid="cancellation-dialog-dismiss"
          >
            Keep {entityLabel}
          </button>
          <button
            onClick={() => trimmed && onConfirm(trimmed)}
            disabled={!trimmed || submitting}
            className={`btn ${trimmed && !submitting ? 'btn-primary' : 'btn-secondary opacity-50 cursor-not-allowed'}`}
            data-testid="cancellation-dialog-confirm"
          >
            {submitting ? 'Cancelling...' : 'Confirm Cancellation'}
          </button>
        </div>
      </div>
    </div>
  );
}

interface CancellationDetailsProps {
  reason?: string | null;
  cancelledBy?: string | null;
  cancelledAt?: string | null;
  /** Optional resolver mapping actor ids to display names. */
  resolveActorName?: (id: string) => string;
}

/** Shared content for the "Cancellation" tab — shown only while cancelled. */
export function CancellationDetails({
  reason,
  cancelledBy,
  cancelledAt,
  resolveActorName,
}: CancellationDetailsProps) {
  const actor = cancelledBy
    ? (resolveActorName ? resolveActorName(cancelledBy) : cancelledBy)
    : 'Unknown';
  const when = cancelledAt ? new Date(cancelledAt).toLocaleString() : null;

  return (
    <div className="space-y-4" data-testid="cancellation-details">
      <div className="flex items-start gap-3 p-3 rounded-lg bg-red-50 dark:bg-red-900/10 border border-red-200 dark:border-red-800/50">
        <Ban size={18} className="text-red-500 shrink-0 mt-0.5" />
        <div className="text-sm text-red-700 dark:text-red-300">
          <p className="font-semibold">This item was cancelled</p>
          <p className="text-xs mt-0.5 text-red-600 dark:text-red-400">
            Cancelled by <span className="font-medium">{actor}</span>
            {when && <> on {when}</>}
          </p>
        </div>
      </div>
      <div>
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Reason</h4>
        {reason ? (
          <MarkdownContent content={reason} />
        ) : (
          <p className="text-sm text-gray-400 italic">No reason recorded</p>
        )}
      </div>
    </div>
  );
}
