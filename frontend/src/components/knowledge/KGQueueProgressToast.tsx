/**
 * KGQueueProgressToast — persistent toast surfacing the consolidation
 * queue's live progress. Visible whenever `pending + claimed > 0` and
 * auto-dismisses when the queue drains. Fed by `useKgLiveEvents` via its
 * new `kg.queue.progress` SSE event.
 */

import type { KgQueueProgress } from '@/hooks/useKgLiveEvents';

interface Props {
  progress: KgQueueProgress | null;
}

export function KGQueueProgressToast({ progress }: Props) {
  if (!progress) return null;
  const activeWork = progress.pending + progress.claimed;
  if (activeWork === 0) return null;

  const done = progress.done;
  const total = activeWork + done + progress.failed;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="kg-queue-progress-toast"
      className="fixed bottom-4 left-4 z-40 w-80 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg p-3 text-sm"
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="font-semibold text-gray-800 dark:text-gray-100">
          Consolidation in progress
        </span>
        <span className="text-xs text-gray-500">{pct}%</span>
      </div>

      <div className="h-1.5 rounded bg-gray-100 dark:bg-gray-800 overflow-hidden mb-2">
        <div
          className="h-full bg-blue-500 transition-all duration-500"
          style={{ width: `${pct}%` }}
          aria-hidden
        />
      </div>

      <div className="grid grid-cols-4 gap-1 text-[11px] text-gray-600 dark:text-gray-400">
        <Stat label="Pending" value={progress.pending} />
        <Stat label="Running" value={progress.claimed} accent="blue" />
        <Stat label="Done" value={progress.done} accent="green" />
        {progress.failed > 0 ? (
          <Stat label="Failed" value={progress.failed} accent="red" />
        ) : (
          <Stat label="Paused" value={progress.paused} />
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  accent = 'neutral',
}: {
  label: string;
  value: number;
  accent?: 'blue' | 'green' | 'red' | 'neutral';
}) {
  const color =
    accent === 'blue'
      ? 'text-blue-600 dark:text-blue-400'
      : accent === 'green'
        ? 'text-green-600 dark:text-green-400'
        : accent === 'red'
          ? 'text-red-600 dark:text-red-400'
          : 'text-gray-700 dark:text-gray-300';
  return (
    <div className="flex flex-col">
      <span className="text-gray-500">{label}</span>
      <span className={`font-semibold ${color}`}>{value}</span>
    </div>
  );
}
