import { useId } from 'react';
import { Ban } from 'lucide-react';

import { MarkdownContent } from '@/components/shared/MarkdownContent';

export interface CancellationPanelProps {
  id?: string;
  entityLabel?: string;
  reason?: string | null;
  cancelledBy?: string | null;
  cancelledAt?: string | null;
  previousStatus?: string | null;
  resolveActorName?: (id: string) => string;
  testId?: string;
  className?: string;
}

function cancellationTimestamp(value?: string | null): string | null {
  if (!value) return null;
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime())
    ? value
    : timestamp.toLocaleString();
}

/**
 * Inline cancellation audit panel for an entity's Details workspace.
 *
 * `previousStatus` is deliberately optional: current entity projections do
 * not all expose the pre-cancellation status, so callers must never infer it.
 */
export function CancellationPanel({
  id,
  entityLabel = 'item',
  reason,
  cancelledBy,
  cancelledAt,
  previousStatus,
  resolveActorName,
  testId = 'cancellation-details',
  className = '',
}: CancellationPanelProps) {
  const titleId = useId();
  const generatedPanelId = useId();
  const panelId = id ?? `cancellation-${generatedPanelId.replace(/[^A-Za-z0-9_-]/g, '-')}`;
  const actor = cancelledBy
    ? (resolveActorName ? resolveActorName(cancelledBy) : cancelledBy)
    : 'Unknown';
  const when = cancellationTimestamp(cancelledAt);

  return (
    <section
      id={panelId}
      aria-labelledby={titleId}
      tabIndex={-1}
      data-testid={testId}
      className={`space-y-4 ${className}`.trim()}
    >
      <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-3 dark:border-red-800/50 dark:bg-red-900/10">
        <Ban size={18} className="mt-0.5 shrink-0 text-red-500" />
        <div className="text-sm text-red-700 dark:text-red-300">
          <p id={titleId} className="font-semibold">
            This {entityLabel} was cancelled
          </p>
          <p className="mt-0.5 text-xs text-red-600 dark:text-red-400">
            Cancelled by <span className="font-medium">{actor}</span>
            {when && <> on {when}</>}
          </p>
          {previousStatus && (
            <p className="mt-1 text-xs text-red-600 dark:text-red-400">
              Previous status:{' '}
              <span className="font-medium">{previousStatus}</span>
            </p>
          )}
        </div>
      </div>
      <div>
        <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">
          Reason
        </h4>
        {reason ? (
          <MarkdownContent content={reason} />
        ) : (
          <p className="text-sm italic text-gray-400">No reason recorded</p>
        )}
      </div>
    </section>
  );
}
