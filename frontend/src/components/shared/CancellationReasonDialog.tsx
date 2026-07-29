/**
 * CancellationReasonDialog — shared confirmation dialog that captures the
 * MANDATORY justification before an item (ideation, refinement, spec, sprint
 * or card) is moved to 'cancelled' (ITEM 17).
 *
 * Also exports the backwards-compatible CancellationDetails wrapper.
 */

import { useEffect, useState } from 'react';
import { Ban } from 'lucide-react';
import {
  CancellationPanel,
  type CancellationPanelProps,
} from '@/components/shared/CancellationPanel';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';

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

  useEscapeToClose(onCancel, { enabled: open, canClose: !submitting, priority: 20 });

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

export type CancellationDetailsProps = CancellationPanelProps;

/** @deprecated Use CancellationPanel in new Details workspaces. */
export function CancellationDetails(props: CancellationDetailsProps) {
  return <CancellationPanel {...props} />;
}
