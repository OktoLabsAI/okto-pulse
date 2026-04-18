/**
 * NodeDetailModal — centered modal wrapper around NodeDetailPanel.
 *
 * Used by:
 *   - Global Discovery: clicking a search result opens the node in a modal
 *     (fetching the full KGNode via kgApi.getNodeDetail since the search
 *     response only includes a partial projection).
 *   - NodePreviewPanel "Show more": promotes the inline preview to a full
 *     detail view without navigating away from the graph canvas.
 *
 * The modal accepts either an already-hydrated `node` or an `id + board_id`
 * pair, in which case it fetches the full node on mount.
 */

import { useEffect, useState } from 'react';
import type { KGNode } from '@/types/knowledge-graph';
import { NodeDetailPanel } from './NodeDetailPanel';
import * as kgApi from '@/services/kg-api';

interface Props {
  /** When provided, the modal renders this node immediately. */
  node?: KGNode | null;
  /** When `node` is absent, fetch the node identified by this id from `boardId`. */
  nodeId?: string;
  boardId: string;
  onClose: () => void;
}

export function NodeDetailModal({ node, nodeId, boardId, onClose }: Props) {
  const [hydrated, setHydrated] = useState<KGNode | null>(node ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (node) {
      setHydrated(node);
      return;
    }
    if (!nodeId) return;
    setLoading(true);
    setError(null);
    kgApi
      .getNodeDetail(boardId, nodeId)
      .then((n) => {
        if (!cancelled) setHydrated(n);
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? 'Failed to load node details');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [node, nodeId, boardId]);

  // Close on ESC for parity with the sidebar panel.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      data-testid="kg-node-detail-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Node detail"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-lg bg-white dark:bg-gray-900 shadow-2xl border border-gray-200 dark:border-gray-700"
        onClick={(e) => e.stopPropagation()}
      >
        {loading && (
          <div className="p-6 text-sm text-gray-500 dark:text-gray-400 animate-pulse">
            Loading node…
          </div>
        )}
        {error && (
          <div className="p-6 text-sm text-red-500">
            {error}
            <button
              type="button"
              onClick={onClose}
              className="ml-3 text-blue-600 hover:underline"
            >
              Close
            </button>
          </div>
        )}
        {hydrated && (
          <NodeDetailPanel node={hydrated} boardId={boardId} onClose={onClose} />
        )}
      </div>
    </div>
  );
}
