import { ChevronDown, Clock } from 'lucide-react';
import type { ActivityLogEntry } from '@/services/api';

interface ActivityLogListProps {
  entries: ActivityLogEntry[];
  emptyMessage?: string;
}

function formatActivityTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString('en-US', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function safeJson(details: Record<string, unknown>): string {
  try {
    return JSON.stringify(details, null, 2) ?? '{}';
  } catch {
    return JSON.stringify({ error: 'Activity details could not be serialized' }, null, 2);
  }
}

function displayAction(action: string, trigger?: string | null): string {
  if (trigger && trigger !== action) return `${action} / ${trigger}`;
  return action;
}

export function ActivityLogList({
  entries,
  emptyMessage = 'No activity recorded',
}: ActivityLogListProps) {
  if (entries.length === 0) {
    return (
      <p className="text-gray-400 dark:text-gray-500 text-sm text-center py-4">
        {emptyMessage}
      </p>
    );
  }

  return (
    <div className="space-y-2" data-testid="activity-log-list">
      {entries.map((entry) => (
        <article
          key={entry.id}
          className="flex gap-3 py-3 border-b border-gray-100 dark:border-gray-700 last:border-0"
          data-testid="activity-log-entry"
        >
          <Clock size={14} className="mt-0.5 text-gray-400 dark:text-gray-500 shrink-0" />
          <div className="flex-1 min-w-0 space-y-2">
            <div className="min-w-0">
              <p className="text-sm text-gray-800 dark:text-gray-200 break-words">
                {entry.summary || entry.action}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                <span className="font-medium text-gray-600 dark:text-gray-300">
                  {entry.actor_name}
                </span>
                <span>{formatActivityTimestamp(entry.created_at)}</span>
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                  {displayAction(entry.action, entry.trigger)}
                </span>
                {entry.actor_type === 'agent' && (
                  <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[11px] text-purple-600 dark:bg-purple-900/40 dark:text-purple-300">
                    agent
                  </span>
                )}
              </div>
            </div>

            {entry.details && (
              <details className="group rounded-md border border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900">
                <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs font-medium text-gray-600 dark:text-gray-300">
                  <ChevronDown
                    size={14}
                    className="transition-transform group-open:rotate-180"
                    aria-hidden
                  />
                  Details
                </summary>
                <pre className="max-h-72 overflow-auto border-t border-gray-200 px-3 py-2 text-xs leading-relaxed text-gray-700 dark:border-gray-700 dark:text-gray-200">
                  {safeJson(entry.details)}
                </pre>
              </details>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
