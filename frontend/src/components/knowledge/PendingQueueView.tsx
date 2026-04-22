/**
 * PendingQueueView — displays the consolidation queue for a board.
 * Shows pending, claimed, and recently processed artifacts.
 */

import { useState, useEffect } from 'react';
import * as kgApi from '@/services/kg-api';
import { KGRefreshButton } from './KGRefreshButton';

interface Props {
  boardId: string;
}

interface QueueEntry {
  id: string;
  board_id: string;
  artifact_id: string;
  artifact_type: string;
  priority: string;
  source: string;
  status: string;
  triggered_at: string | null;
  claimed_by_session_id: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  claimed: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
  done: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  paused: 'bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400',
};

const PRIORITY_ICON: Record<string, string> = {
  high: '🔴',
  low: '🔵',
};

export function PendingQueueView({ boardId }: Props) {
  const [entries, setEntries] = useState<QueueEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadPending();
  }, [boardId]);

  async function loadPending() {
    setLoading(true);
    setError(null);
    try {
      const data = await kgApi.listPending(boardId);
      setEntries(data.entries || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load pending queue');
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center">
        <div className="animate-pulse text-gray-400">Loading pending queue...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <p className="text-red-500 mb-3">{error}</p>
        <button onClick={loadPending} className="text-sm text-blue-600 hover:underline">
          Retry
        </button>
      </div>
    );
  }

  const pendingCount = entries.filter(e => e.status === 'pending').length;
  const claimedCount = entries.filter(e => e.status === 'claimed').length;

  if (entries.length === 0) {
    return (
      <div className="p-6 text-center text-gray-500 dark:text-gray-400">
        <div className="text-4xl mb-3">📭</div>
        <h3 className="font-medium mb-1">Queue is empty</h3>
        <p className="text-sm">No artifacts are queued for consolidation. New items appear when specs, sprints, or cards change state.</p>
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Consolidation Queue
        </h2>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500">
            {pendingCount} pending, {claimedCount} in progress
          </span>
          <KGRefreshButton
            onRefresh={loadPending}
            loading={loading}
            testId="pending-refresh"
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400">
              <th className="pb-2 pr-3">Status</th>
              <th className="pb-2 pr-3">Type</th>
              <th className="pb-2 pr-3">Artifact</th>
              <th className="pb-2 pr-3">Priority</th>
              <th className="pb-2 pr-3">Source</th>
              <th className="pb-2">Triggered</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id} className="border-b border-gray-100 dark:border-gray-800">
                <td className="py-2.5 pr-3">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES[entry.status] || STATUS_STYLES.pending}`}>
                    {entry.status}
                  </span>
                </td>
                <td className="py-2.5 pr-3 text-gray-700 dark:text-gray-300">
                  {entry.artifact_type}
                </td>
                <td className="py-2.5 pr-3 font-mono text-xs text-gray-600 dark:text-gray-400">
                  {entry.artifact_id?.slice(0, 12)}...
                </td>
                <td className="py-2.5 pr-3">
                  <span title={entry.priority}>
                    {PRIORITY_ICON[entry.priority] || entry.priority}
                  </span>
                </td>
                <td className="py-2.5 pr-3 text-gray-500 dark:text-gray-400">
                  {entry.source}
                </td>
                <td className="py-2.5 text-xs text-gray-500 dark:text-gray-400">
                  {entry.triggered_at ? new Date(entry.triggered_at).toLocaleString() : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
