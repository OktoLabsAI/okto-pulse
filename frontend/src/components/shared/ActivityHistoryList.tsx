import { useState } from 'react';
import { ArrowRight, ChevronDown, ChevronUp, History } from 'lucide-react';

export interface ActivityHistoryChange {
  field: string;
  old: unknown;
  new: unknown;
}

export interface ActivityHistoryEntry {
  id: string;
  action: string;
  actor_type: string;
  actor_name: string;
  created_at: string;
  changes?: readonly ActivityHistoryChange[] | null;
  summary?: string | null;
  version?: number | null;
}

export interface ActivityHistoryListProps {
  entries: readonly ActivityHistoryEntry[];
  loading?: boolean;
  actionLabels?: Readonly<Record<string, string>>;
  actionColors?: Readonly<Record<string, string>>;
  versionLabel?: (version: number) => { text: string; title?: string };
}

const DEFAULT_ACTION_LABELS: Readonly<Record<string, string>> = {
  created: 'Created',
  updated: 'Updated',
  status_changed: 'Status changed',
  cards_derived: 'Cards derived',
  knowledge_added: 'Knowledge added',
  knowledge_removed: 'Knowledge removed',
  qa_added: 'Question added',
  qa_answered: 'Question answered',
  dependency_added: 'Dependency added',
  dependency_removed: 'Dependency removed',
  spec_dependency_added: 'Spec dependency added',
  spec_dependency_removed: 'Spec dependency removed',
};

const DEFAULT_ACTION_COLORS: Readonly<Record<string, string>> = {
  created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  status_changed: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  cards_derived: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  dependency_added: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300',
  dependency_removed: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  spec_dependency_added: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300',
  spec_dependency_removed: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
};

const FALLBACK_ACTION_COLOR = 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300';

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '(empty)';
  if (Array.isArray(value)) {
    if (value.length === 0) return '(empty list)';
    return value
      .map((item, index) => `${index + 1}. ${item !== null && typeof item === 'object' ? JSON.stringify(item) : String(item)}`)
      .join('\n');
  }
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

export function ActivityHistoryList({
  entries,
  loading = false,
  actionLabels,
  actionColors,
  versionLabel = (version) => ({ text: `v${version}` }),
}: ActivityHistoryListProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (loading) {
    return (
      <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">
        Loading history...
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-center py-6">
        <History size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No history yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {entries.map((entry) => {
        const isExpanded = expandedId === entry.id;
        const actionColor = actionColors?.[entry.action]
          ?? DEFAULT_ACTION_COLORS[entry.action]
          ?? FALLBACK_ACTION_COLOR;
        const actionLabel = actionLabels?.[entry.action]
          ?? DEFAULT_ACTION_LABELS[entry.action]
          ?? entry.action;
        const hasChanges = Boolean(entry.changes?.length);
        const changesId = `activity-history-changes-${entry.id}`;
        const displayedVersion = entry.version
          ? versionLabel(entry.version)
          : null;

        return (
          <div
            key={entry.id}
            className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
          >
            <button
              type="button"
              className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/30 w-full text-left"
              onClick={() => hasChanges && setExpandedId(isExpanded ? null : entry.id)}
              disabled={!hasChanges}
              aria-expanded={hasChanges ? isExpanded : undefined}
              aria-controls={hasChanges ? changesId : undefined}
            >
              {/* Timeline dot */}
              <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 shrink-0" />

              {/* Action badge */}
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${actionColor}`}>
                {actionLabel}
              </span>

              {/* Summary */}
              <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">
                {entry.summary || entry.action}
              </span>

              {/* Actor + time */}
              <span className="flex items-center gap-2 shrink-0 text-[10px] text-gray-400">
                <span className={`px-1 py-0.5 rounded ${
                  entry.actor_type === 'agent'
                    ? 'bg-violet-100 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300'
                    : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                }`}>
                  {entry.actor_name}
                </span>
                {displayedVersion && (
                  <span title={displayedVersion.title}>{displayedVersion.text}</span>
                )}
                <span>{new Date(entry.created_at).toLocaleString()}</span>
              </span>

              {hasChanges && (
                <span className="text-gray-400 shrink-0" aria-hidden="true">
                  {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
              )}
            </button>

            {/* Expanded diff view */}
            {isExpanded && hasChanges && (
              <div
                id={changesId}
                className="px-3 py-2 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 space-y-2"
              >
                {entry.changes!.map((change, index) => (
                  <div key={`${change.field}-${index}`} className="text-sm">
                    <div className="font-medium text-gray-700 dark:text-gray-300 text-xs uppercase tracking-wide mb-1">
                      {change.field}
                    </div>
                    <div className="flex items-start gap-2">
                      {/* Old value */}
                      <div
                        className="flex-1 min-w-0"
                        role="region"
                        aria-label={`${change.field} before value`}
                      >
                        <div className="text-[10px] text-red-500 font-medium mb-0.5">Before</div>
                        <pre className="text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                          {formatValue(change.old)}
                        </pre>
                      </div>
                      <ArrowRight size={14} className="text-gray-400 mt-4 shrink-0" aria-hidden="true" />
                      {/* New value */}
                      <div
                        className="flex-1 min-w-0"
                        role="region"
                        aria-label={`${change.field} after value`}
                      >
                        <div className="text-[10px] text-green-500 font-medium mb-0.5">After</div>
                        <pre className="text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                          {formatValue(change.new)}
                        </pre>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
