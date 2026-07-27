import type { ActivityLogEntry } from '@/services/api';
import {
  ActivityHistoryList,
  type ActivityHistoryChange,
  type ActivityHistoryEntry,
} from './ActivityHistoryList';

interface ActivityLogListProps {
  entries: readonly ActivityLogEntry[];
  loading?: boolean;
}

const CARD_ACTION_LABELS: Readonly<Record<string, string>> = {
  card_created: 'Created',
  card_updated: 'Updated',
  card_moved: 'Status changed',
  card_deleted: 'Deleted',
  validation_submitted: 'Validation submitted',
  task_validated: 'Validated',
};

const CARD_ACTION_COLORS: Readonly<Record<string, string>> = {
  card_created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  card_updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  card_moved: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  card_deleted: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  validation_submitted: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  task_validated: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
};

function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function activityChanges(details: Record<string, unknown> | null | undefined): ActivityHistoryChange[] {
  if (!details) return [];

  if (Array.isArray(details.changes)) {
    const changes = details.changes.flatMap((value): ActivityHistoryChange[] => {
      if (!isRecord(value) || typeof value.field !== 'string') return [];
      if (!hasOwn(value, 'old') && !hasOwn(value, 'new')) return [];
      return [{ field: value.field, old: value.old, new: value.new }];
    });
    if (changes.length > 0) return changes;
  }

  if (hasOwn(details, 'from_status') || hasOwn(details, 'to_status')) {
    return [{ field: 'status', old: details.from_status, new: details.to_status }];
  }

  if (hasOwn(details, 'before') && hasOwn(details, 'after')) {
    return [{
      field: typeof details.field === 'string' ? details.field : 'change',
      old: details.before,
      new: details.after,
    }];
  }

  if (hasOwn(details, 'old') && hasOwn(details, 'new')) {
    return [{
      field: typeof details.field === 'string' ? details.field : 'change',
      old: details.old,
      new: details.new,
    }];
  }

  return [];
}

function inlineValue(value: unknown): string {
  if (value === null || value === undefined) return '(empty)';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function activitySummary(entry: ActivityLogEntry, changes: readonly ActivityHistoryChange[]): string {
  if (entry.action === 'card_moved') {
    const statusChange = changes.find((change) => change.field === 'status');
    if (statusChange) {
      return `Status: ${inlineValue(statusChange.old)} → ${inlineValue(statusChange.new)}`;
    }

    const legacyTransition = entry.summary?.match(/^\s*(.+?)\s*->\s*(.+?)\s*$/);
    if (legacyTransition) {
      return `Status: ${legacyTransition[1]} → ${legacyTransition[2]}`;
    }
  }

  if (entry.action === 'card_updated' && changes.length > 0) {
    return `Updated: ${changes.map((change) => change.field).join(', ')}`;
  }

  return entry.summary || entry.action;
}

function normalizeEntry(entry: ActivityLogEntry): ActivityHistoryEntry {
  const changes = activityChanges(entry.details);
  return {
    id: entry.id,
    action: entry.action,
    actor_type: entry.actor_type,
    actor_name: entry.actor_name,
    created_at: entry.created_at,
    changes,
    summary: activitySummary(entry, changes),
    version: null,
  };
}

export function ActivityLogList({ entries, loading = false }: ActivityLogListProps) {
  return (
    <div data-testid="activity-log-list">
      <ActivityHistoryList
        entries={entries.map(normalizeEntry)}
        loading={loading}
        actionLabels={CARD_ACTION_LABELS}
        actionColors={CARD_ACTION_COLORS}
      />
    </div>
  );
}
