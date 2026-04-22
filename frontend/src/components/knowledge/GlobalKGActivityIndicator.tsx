/**
 * GlobalKGActivityIndicator — cross-app feedback of cognitive consolidation.
 *
 * Mounted once at the App shell level and subscribes to the SSE stream of
 * the currently active board (``useKgLiveEvents``). Surfaces two things
 * the user needs to see from anywhere in the app:
 *
 *   * A small pill in the bottom-right whenever the pipeline has work to
 *     do (``pending + claimed > 0``). Tells the user "we are still
 *     processing" without forcing them to the KG page.
 *   * The existing ``KGQueueProgressToast`` (detailed card in the bottom
 *     left) so the user can see counts / progress without navigating.
 *
 * Both render nothing when the queue is drained — no persistent chrome.
 *
 * Used by App.tsx; `boardId` is optional so screens without a selected
 * board (fresh install, no boards yet) still render cleanly.
 */

import { Brain } from 'lucide-react';
import { useKgLiveEvents } from '@/hooks/useKgLiveEvents';
import { KGQueueProgressToast } from './KGQueueProgressToast';

interface Props {
  boardId: string | null | undefined;
}

export function GlobalKGActivityIndicator({ boardId }: Props) {
  const enabled = Boolean(boardId);
  const live = useKgLiveEvents(boardId ?? '', { enabled });

  if (!enabled) return null;

  const progress = live.queueProgress;
  const activeWork = progress ? progress.pending + progress.claimed : 0;
  const showPill = activeWork > 0;

  return (
    <>
      {showPill && (
        <div
          role="status"
          aria-live="polite"
          data-testid="kg-global-activity-pill"
          className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/70 px-3 py-1.5 shadow"
          title={
            progress
              ? `Cognitive pipeline working: ${progress.claimed} running, ${progress.pending} pending`
              : 'Cognitive pipeline working'
          }
        >
          <Brain size={14} className="text-blue-600 dark:text-blue-300 animate-pulse" />
          <span className="text-xs font-medium text-blue-800 dark:text-blue-200">
            {progress?.claimed ? `${progress.claimed} running` : 'processing'}
            {progress && progress.pending > 0 ? ` · ${progress.pending} pending` : ''}
          </span>
        </div>
      )}

      <KGQueueProgressToast progress={progress} />
    </>
  );
}
