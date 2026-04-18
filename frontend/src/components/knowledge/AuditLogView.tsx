/**
 * AuditLogView — displays consolidation audit entries for a board.
 * Shows session history: who consolidated what, when, and how many nodes/edges.
 */

import { useState, useEffect } from 'react';
import type { AuditEntry } from '@/types/knowledge-graph';
import * as kgApi from '@/services/kg-api';
import { KGRefreshButton } from './KGRefreshButton';

interface Props {
  boardId: string;
}

const STATUS_BADGE: Record<string, string> = {
  none: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  undone: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  undo_blocked: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
};

export function AuditLogView({ boardId }: Props) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadAudit();
  }, [boardId]);

  async function loadAudit() {
    setLoading(true);
    setError(null);
    try {
      const data = await kgApi.listAudit(boardId, 100);
      setEntries(data.entries || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load audit log');
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center">
        <div className="animate-pulse text-gray-400">Loading audit log...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <p className="text-red-500 mb-3">{error}</p>
        <button onClick={loadAudit} className="text-sm text-blue-600 hover:underline">
          Retry
        </button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="p-6 text-center text-gray-500 dark:text-gray-400">
        <div className="text-4xl mb-3">📋</div>
        <h3 className="font-medium mb-1">No audit entries yet</h3>
        <p className="text-sm">Consolidation sessions will appear here once the KG agent processes artifacts.</p>
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Audit Log
        </h2>
        <KGRefreshButton
          onRefresh={loadAudit}
          loading={loading}
          testId="audit-refresh"
        />
      </div>

      <div className="space-y-3">
        {entries.map((entry) => (
          <div
            key={entry.session_id}
            className="border border-gray-200 dark:border-gray-700 rounded-lg p-4"
          >
            <div className="flex items-start justify-between mb-2">
              <div className="flex items-center gap-2">
                <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_BADGE[entry.undo_status] || STATUS_BADGE.none}`}>
                  {entry.undo_status === 'none' ? 'committed' : entry.undo_status}
                </span>
                <span className="text-xs text-gray-400 font-mono">
                  {entry.session_id.slice(0, 16)}...
                </span>
              </div>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {entry.committed_at ? new Date(entry.committed_at).toLocaleString() : '—'}
              </span>
            </div>

            <div className="text-sm text-gray-700 dark:text-gray-300 mb-2">
              {entry.summary_text || `Consolidated ${entry.artifact_id?.slice(0, 8) ?? '?'}...`}
            </div>

            <div className="flex gap-4 text-xs text-gray-500 dark:text-gray-400">
              <span title="Nodes added">+{entry.nodes_added} nodes</span>
              {entry.nodes_updated > 0 && (
                <span title="Nodes updated">~{entry.nodes_updated} updated</span>
              )}
              {entry.nodes_superseded > 0 && (
                <span title="Nodes superseded">{entry.nodes_superseded} superseded</span>
              )}
              <span title="Edges added">+{entry.edges_added} edges</span>
              <span className="ml-auto" title="Agent">
                {entry.agent_id?.slice(0, 20) ?? 'unknown'}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
