/**
 * KnowledgeGraphPage — container component orchestrating the KG visualization.
 *
 * Spec 8 / Sprint 4 + Sprint 5 wiring:
 *   - Holds `nodeLimit` (50/100/200/500) + `nextCursor` for paginated fetch.
 *   - Changing `nodeLimit` resets the list and refetches from scratch.
 *   - Clicking the Load More button fetches the next page and APPENDS results.
 *   - The button is only visible while `nextCursor !== null`.
 *   - Sprint 5: filter-reset callback + spec navigation + testable retry.
 *
 * Selection + highlight live inside GraphCanvas (S4.1). The detail panel
 * here is opened by the double-click callback (`onSelect`) per AC-4; the
 * single-click preview is rendered by GraphCanvas itself (AC-8).
 */

import { useCallback, useEffect, useState } from 'react';
import { GraphCanvas } from './GraphCanvas';
import { NodeDetailPanel } from './NodeDetailPanel';
import { GraphControlsPanel } from './GraphControlsPanel';
import type { Filters } from './GraphControlsPanel';
import { EmptyState } from './EmptyState';
import { AuditLogView } from './AuditLogView';
import { PendingQueueView } from './PendingQueueView';
import { PendingQueueTree } from './PendingQueueTree';
import { KGSyncIndicator } from './KGSyncIndicator';
import { SettingsView } from './SettingsView';
import { GlobalSearchView } from './GlobalSearchView';
import { KGRefreshButton } from './KGRefreshButton';
import { KGQueueProgressToast } from './KGQueueProgressToast';
import { NodeDetailModal } from './NodeDetailModal';
import { useKgLiveEvents } from '@/hooks/useKgLiveEvents';
import type { KGNode, KGEdge } from '@/types/knowledge-graph';
import * as kgApi from '@/services/kg-api';

interface Props {
  boardId: string;
}

type SubView = 'graph' | 'audit' | 'pending' | 'pending_tree' | 'settings' | 'global';

const DEFAULT_NODE_LIMIT = 100;
const DEFAULT_FILTERS: Filters = {
  types: [],
  edgeTypes: [],
  minConfidence: 0.5,
  searchQuery: '',
};

export function KnowledgeGraphPage({ boardId }: Props) {
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<KGNode | null>(null);
  const [modalNode, setModalNode] = useState<KGNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [subView, setSubView] = useState<SubView>('graph');
  const [nodeLimit, setNodeLimit] = useState<number>(DEFAULT_NODE_LIMIT);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);

  const loadGraph = useCallback(
    async (limit: number) => {
      setLoading(true);
      setError(null);
      try {
        const data = await kgApi.getSubgraph(boardId, { limit });
        setNodes(data.nodes || []);
        setEdges(data.edges || []);
        setNextCursor(data.next_cursor ?? null);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load graph');
      } finally {
        setLoading(false);
      }
    },
    [boardId],
  );

  useEffect(() => {
    loadGraph(nodeLimit);
  }, [boardId, nodeLimit, loadGraph]);

  // Wire SSE live events. When a commit burst settles, auto-refetch the
  // graph so the canvas reflects the new state — the sync indicator chip
  // surfaces unseen commits + connection state to the user.
  const liveEvents = useKgLiveEvents(boardId, {
    onFlush: () => {
      if (subView === 'graph') loadGraph(nodeLimit);
    },
  });

  // Keyboard shortcut `R` is wired by KGRefreshButton itself via `shortcut`.

  const handleNodeLimitChange = useCallback((limit: number) => {
    setSelectedNode(null);
    setNodeLimit(limit);
  }, []);

  const handleLoadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const data = await kgApi.getSubgraph(boardId, {
        limit: nodeLimit,
        cursor: nextCursor,
      });
      // Dedupe appended nodes/edges on id to defend against overlap if the
      // cursor was advanced by a concurrent consolidation.
      setNodes((prev) => {
        const seen = new Set(prev.map((n) => n.id));
        const appended = (data.nodes ?? []).filter((n) => !seen.has(n.id));
        return [...prev, ...appended];
      });
      setEdges((prev) => {
        const seen = new Set(prev.map((e) => e.id));
        const appended = (data.edges ?? []).filter((e) => !seen.has(e.id));
        return [...prev, ...appended];
      });
      setNextCursor(data.next_cursor ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load more');
    } finally {
      setLoadingMore(false);
    }
  }, [boardId, nodeLimit, nextCursor, loadingMore]);

  const handleClearFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS);
  }, []);

  const handleOpenSpec = useCallback((specRef: string) => {
    if (typeof window !== 'undefined') {
      window.location.href = `/specs/${specRef}`;
    }
  }, []);

  if (loading) {
    return (
      <div
        className="flex items-center justify-center h-full"
        data-testid="kg-loading"
      >
        <div className="animate-pulse text-gray-400 dark:text-gray-600">
          Loading Knowledge Graph...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div
        className="flex flex-col items-center justify-center h-full gap-4"
        data-testid="kg-error"
        role="alert"
      >
        <p className="text-red-500" data-testid="kg-error-message">
          {error}
        </p>
        <button
          type="button"
          onClick={() => loadGraph(nodeLimit)}
          data-testid="kg-error-retry"
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Retry
        </button>
      </div>
    );
  }

  if (nodes.length === 0 && subView === 'graph') {
    return (
      <div data-empty-state="yet" data-testid="kg-empty-yet" className="h-full">
        <EmptyState boardId={boardId} onRefresh={() => loadGraph(nodeLimit)} />
      </div>
    );
  }

  return (
    <div className="flex h-full">
      {/* Left: Controls */}
      <div className="w-64 border-r border-gray-200 dark:border-gray-700 overflow-y-auto">
        <GraphControlsPanel
          filters={filters}
          onFiltersChange={setFilters}
          subView={subView}
          onSubViewChange={setSubView}
          nodeCount={nodes.length}
          nodeLimit={nodeLimit}
          onNodeLimitChange={handleNodeLimitChange}
        />
      </div>

      {/* Center: Graph or sub-view */}
      <div className="flex-1 relative">
        {subView === 'graph' ? (
          <>
            {/* KG Refresh button — spec f33eb9ca, BR `Empty State CTA` adjacent
                requirement. Invalidates current graph state and re-fetches.
                Keyboard shortcut `R` works when the canvas has focus. */}
            <div
              className="absolute top-3 right-3 z-10 flex items-center gap-2"
              data-testid="kg-toolbar"
            >
              <KGSyncIndicator
                connectionState={liveEvents.connectionState}
                unseenCommits={liveEvents.unseenCommits}
                lastEventAt={liveEvents.lastEvent?.created_at ?? null}
                onApply={() => {
                  liveEvents.markSeen();
                  loadGraph(nodeLimit);
                }}
              />
            <KGRefreshButton
              onRefresh={() => loadGraph(nodeLimit)}
              loading={loading}
              label="Refresh"
              shortcut
              testId="kg-refresh"
            />
            </div>
            <GraphCanvas
              nodes={nodes}
              edges={edges}
              filters={filters}
              onSelect={setSelectedNode}
              initialSelectedNodeId={selectedNode?.id ?? null}
              onClearFilters={handleClearFilters}
              onOpenSpec={handleOpenSpec}
              onShowDetails={setModalNode}
            />
            {nextCursor && (
              <button
                type="button"
                onClick={handleLoadMore}
                disabled={loadingMore}
                data-testid="kg-load-more"
                className="absolute bottom-4 right-4 px-4 py-2 rounded-md shadow bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-wait"
              >
                {loadingMore ? 'Loading…' : 'Load more'}
              </button>
            )}
          </>
        ) : subView === 'audit' ? (
          <AuditLogView boardId={boardId} />
        ) : subView === 'pending' ? (
          <PendingQueueView boardId={boardId} />
        ) : subView === 'pending_tree' ? (
          <PendingQueueTree boardId={boardId} />
        ) : subView === 'settings' ? (
          <SettingsView boardId={boardId} />
        ) : subView === 'global' ? (
          <GlobalSearchView boardId={boardId} />
        ) : null}
      </div>

      {/* Right: Node detail */}
      {selectedNode && (
        <div className="w-80 border-l border-gray-200 dark:border-gray-700 overflow-y-auto">
          <NodeDetailPanel
            node={selectedNode}
            boardId={boardId}
            onClose={() => setSelectedNode(null)}
            onNodeNavigate={(nodeId) => {
              const target = nodes.find((n) => n.id === nodeId);
              if (target) setSelectedNode(target);
            }}
          />
        </div>
      )}

      {modalNode && (
        <NodeDetailModal
          node={modalNode}
          boardId={boardId}
          onClose={() => setModalNode(null)}
        />
      )}

      <KGQueueProgressToast progress={liveEvents.queueProgress} />
    </div>
  );
}
