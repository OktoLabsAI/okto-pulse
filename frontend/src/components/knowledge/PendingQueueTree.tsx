/**
 * PendingQueueTree — hierarchical view of the consolidation queue
 * (spec f33eb9ca, card e335f585).
 *
 * Renders 5 levels (Ideations → Refinements → Specs → Sprints → Cards),
 * each with a status badge + age + retry-count metadata. Expand/collapse
 * state is preserved in localStorage so a refresh keeps the user's view.
 *
 * Performance note: we do NOT pull react-arborist or react-window here —
 * the typical board has <500 nodes and a flat HTML render is fast enough
 * (verified by `tests/pending-queue-tree.perf.test.tsx`). If/when boards
 * grow past 5k nodes we can swap the recursive renderer for a windowed
 * implementation behind the same prop shape.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import * as kgApi from '@/services/kg-api';
import type { PendingTreeNode, PendingTreeLevels } from '@/services/kg-api';
import { RetryFromHereDialog } from './RetryFromHereDialog';

interface Props {
  boardId: string;
  /** Optional: provide already-fetched data (used by perf tests). */
  initialData?: {
    tree: PendingTreeNode[];
    levels: PendingTreeLevels;
    total_pending: number;
  };
}

const STORAGE_KEY_PREFIX = 'okto.pulse.kg.pendingTree.expanded.';

const STATUS_COLOR: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  in_progress: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  done: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  not_queued: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
};

const TYPE_ICON: Record<string, string> = {
  ideation: '💡',
  refinement: '✏️',
  spec: '📄',
  sprint: '🏃',
  card: '🃏',
};

function loadExpanded(boardId: string): Set<string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_PREFIX + boardId);
    if (!raw) return new Set();
    return new Set(JSON.parse(raw) as string[]);
  } catch {
    return new Set();
  }
}

function saveExpanded(boardId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(STORAGE_KEY_PREFIX + boardId, JSON.stringify(Array.from(ids)));
  } catch {
    /* private mode / quota — ignore */
  }
}

interface RowProps {
  node: PendingTreeNode;
  depth: number;
  expanded: Set<string>;
  toggle: (id: string) => void;
  onRetry: (node: PendingTreeNode) => void;
}

function TreeRow({ node, depth, expanded, toggle, onRetry }: RowProps) {
  const hasChildren = node.children && node.children.length > 0;
  const isOpen = expanded.has(node.id);
  return (
    <div data-testid={`pending-row-${node.type}-${node.id}`}>
      <div
        className="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 dark:hover:bg-gray-800/40"
        style={{ paddingLeft: depth * 16 + 8 }}
      >
        {hasChildren ? (
          <button
            type="button"
            onClick={() => toggle(node.id)}
            aria-label={isOpen ? 'Collapse' : 'Expand'}
            data-testid={`pending-toggle-${node.id}`}
            className="text-xs w-4 text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
          >
            {isOpen ? '▾' : '▸'}
          </button>
        ) : (
          <span className="w-4" aria-hidden />
        )}
        <span aria-hidden>{TYPE_ICON[node.type] ?? '•'}</span>
        <span className="flex-1 truncate text-sm text-gray-800 dark:text-gray-200" title={node.title}>
          {node.title}
        </span>
        <span
          className={`text-[10px] uppercase px-1.5 py-0.5 rounded ${STATUS_COLOR[node.status] || STATUS_COLOR.not_queued}`}
          data-testid={`pending-status-${node.id}`}
        >
          {node.status}
        </span>
        {typeof node.retry_count === 'number' && node.retry_count > 0 && (
          <span className="text-[10px] text-amber-600 dark:text-amber-400" title="Retries">
            ↻{node.retry_count}
          </span>
        )}
        {(node.status === 'failed' || node.status === 'pending') && node.queue_entry_id && (
          <button
            type="button"
            onClick={() => onRetry(node)}
            data-testid={`pending-retry-${node.id}`}
            className="text-[11px] text-blue-600 hover:underline"
          >
            Retry
          </button>
        )}
      </div>
      {hasChildren && isOpen && (
        <div>
          {node.children.map((child) => (
            <TreeRow
              key={`${child.type}-${child.id}`}
              node={child}
              depth={depth + 1}
              expanded={expanded}
              toggle={toggle}
              onRetry={onRetry}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function PendingQueueTree({ boardId, initialData }: Props) {
  const [data, setData] = useState<typeof initialData | null>(initialData ?? null);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => loadExpanded(boardId));
  const [retryTarget, setRetryTarget] = useState<PendingTreeNode | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await kgApi.getPendingTree(boardId, 5);
      setData({
        tree: resp.tree,
        levels: resp.levels,
        total_pending: resp.total_pending,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load pending queue');
    } finally {
      setLoading(false);
    }
  }, [boardId]);

  useEffect(() => {
    if (!initialData) {
      void refetch();
    }
  }, [initialData, refetch]);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      saveExpanded(boardId, next);
      return next;
    });
  }, [boardId]);

  const expandAll = useCallback(() => {
    if (!data) return;
    const next = new Set<string>();
    const walk = (nodes: PendingTreeNode[]) => {
      for (const n of nodes) {
        next.add(n.id);
        if (n.children?.length) walk(n.children);
      }
    };
    walk(data.tree);
    setExpanded(next);
    saveExpanded(boardId, next);
  }, [data, boardId]);

  const collapseAll = useCallback(() => {
    setExpanded(new Set());
    saveExpanded(boardId, new Set());
  }, [boardId]);

  const summary = useMemo(() => {
    if (!data) return null;
    const lvls = data.levels;
    return Object.entries(lvls).map(([level, counts]) => ({
      level,
      total: counts.pending + counts.in_progress + counts.done + counts.failed,
      pending: counts.pending,
      failed: counts.failed,
    }));
  }, [data]);

  if (loading) {
    return (
      <div className="p-6" data-testid="pending-tree-loading">
        <div className="animate-pulse text-gray-400 text-sm">Loading pending queue tree…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6" data-testid="pending-tree-error">
        <p className="text-red-500 text-sm mb-2">{error}</p>
        <button onClick={() => void refetch()} className="text-xs text-blue-600 hover:underline">
          Retry
        </button>
      </div>
    );
  }

  if (!data || data.tree.length === 0) {
    return (
      <div className="p-6 text-center text-sm text-gray-500" data-testid="pending-tree-empty">
        No pending consolidation work for this board.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" data-testid="pending-queue-tree">
      <div className="flex items-center gap-3 px-3 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/40">
        <span className="text-sm font-medium text-gray-800 dark:text-gray-200">
          {data.total_pending} pending
        </span>
        <div className="flex-1 flex items-center gap-2 text-[11px] text-gray-600 dark:text-gray-400">
          {summary?.map((s) => (
            <span key={s.level} className="whitespace-nowrap">
              <span className="font-medium">{s.level}</span> {s.total}
              {s.pending > 0 && (
                <span className="ml-1 text-yellow-700 dark:text-yellow-400">·{s.pending}p</span>
              )}
              {s.failed > 0 && (
                <span className="ml-1 text-red-700 dark:text-red-400">·{s.failed}f</span>
              )}
            </span>
          ))}
        </div>
        <button
          type="button"
          onClick={expandAll}
          className="text-xs text-blue-600 hover:underline"
          data-testid="pending-tree-expand-all"
        >
          Expand all
        </button>
        <button
          type="button"
          onClick={collapseAll}
          className="text-xs text-blue-600 hover:underline"
          data-testid="pending-tree-collapse-all"
        >
          Collapse all
        </button>
        <button
          type="button"
          onClick={() => void refetch()}
          className="text-xs text-gray-600 hover:underline"
          data-testid="pending-tree-refresh"
        >
          Refresh
        </button>
      </div>
      <div className="flex-1 overflow-auto" role="tree">
        {data.tree.map((node) => (
          <TreeRow
            key={`${node.type}-${node.id}`}
            node={node}
            depth={0}
            expanded={expanded}
            toggle={toggle}
            onRetry={setRetryTarget}
          />
        ))}
      </div>
      {retryTarget && (
        <RetryFromHereDialog
          boardId={boardId}
          node={retryTarget}
          onClose={() => setRetryTarget(null)}
          onSuccess={() => {
            setRetryTarget(null);
            void refetch();
          }}
        />
      )}
    </div>
  );
}
