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

import { useCallback, useEffect, useRef, useState } from 'react';
import { HelpCircle, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { GraphCanvas } from './GraphCanvas';
import { KGHelpModal } from './KGHelpModal';
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
  // Default 0% so the slider doesn't hide nodes until the user opts in.
  // Backend currently sends constant relevance_score for most nodes, so a
  // non-zero default would silently filter without producing visible value.
  minRelevance: 0,
  searchQuery: '',
};

const SIDEBAR_WIDTH_KEY = 'kg-sidebar-width';
const SIDEBAR_COLLAPSED_KEY = 'kg-sidebar-collapsed';
const SIDEBAR_DEFAULT = 256;
const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 520;

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
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return SIDEBAR_DEFAULT;
    const stored = window.localStorage.getItem(SIDEBAR_WIDTH_KEY);
    const parsed = stored ? Number(stored) : NaN;
    return Number.isFinite(parsed) && parsed >= SIDEBAR_MIN && parsed <= SIDEBAR_MAX
      ? parsed
      : SIDEBAR_DEFAULT;
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false;
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';
  });
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);

  // Shift+/ opens the help modal while the KG page is mounted. We bind on
  // window but only while the page is active; preventDefault stops the
  // browser Find-in-page from swallowing the keypress.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === '?' && !helpOpen) {
        const target = e.target as HTMLElement | null;
        const tag = target?.tagName ?? '';
        // Ignore when the user is typing in an input / textarea / contentEditable
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable) return;
        e.preventDefault();
        setHelpOpen(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [helpOpen]);

  const [refitTrigger, setRefitTrigger] = useState(0);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? '1' : '0');
      return next;
    });
    // Re-fit the canvas after the width transition completes so the graph
    // re-frames into the new available area.
    setRefitTrigger((k) => k + 1);
  }, []);

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

  const handleAdjustRelevance = useCallback((value: number) => {
    setFilters((prev) => ({ ...prev, minRelevance: value }));
  }, []);

  // Drag-to-resize on the divider between the controls panel and the canvas.
  // Width is clamped + persisted to localStorage so it survives reloads.
  const handleDividerMouseDown = useCallback(
    (e: React.MouseEvent) => {
      dragState.current = { startX: e.clientX, startWidth: sidebarWidth };
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },
    [sidebarWidth],
  );

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = dragState.current;
      if (!drag) return;
      const delta = e.clientX - drag.startX;
      const next = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, drag.startWidth + delta));
      setSidebarWidth(next);
    };
    const onUp = () => {
      if (!dragState.current) return;
      dragState.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [sidebarWidth]);

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
      {/* Left: Controls panel — width animates between sidebarWidth and 0
          when the user toggles collapse. The inner content keeps its full
          width during the transition (via the inline style on the inner
          wrapper) so it doesn't reflow as the outer width shrinks; the
          outer overflow:hidden clips it cleanly. */}
      <div
        style={{ width: sidebarCollapsed ? 0 : sidebarWidth, flexShrink: 0 }}
        className="relative border-r border-gray-200 dark:border-gray-700 overflow-hidden transition-[width] duration-300 ease-in-out"
        data-testid="kg-sidebar"
        data-collapsed={sidebarCollapsed}
      >
        <div
          style={{ width: sidebarWidth, height: '100%' }}
          className="overflow-y-auto"
        >
          <button
            type="button"
            onClick={toggleSidebar}
            data-testid="kg-sidebar-collapse"
            title="Hide controls panel"
            aria-label="Hide controls panel"
            className="absolute top-2 right-2 z-10 p-1 rounded text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100 hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
          >
            <PanelLeftClose size={14} />
          </button>
          <GraphControlsPanel
            filters={filters}
            onFiltersChange={setFilters}
            subView={subView}
            onSubViewChange={setSubView}
            nodeCount={nodes.length}
            nodeLimit={nodeLimit}
            onNodeLimitChange={handleNodeLimitChange}
            boardId={boardId}
            relevanceScores={nodes.map((n) => n.relevance_score ?? 0)}
          />
        </div>
      </div>

      {/* Resizable divider — width animates to 0 alongside the panel so
          the whole left column collapses smoothly. Drag is disabled in
          collapsed state. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize controls panel"
        data-testid="kg-sidebar-divider"
        onMouseDown={sidebarCollapsed ? undefined : handleDividerMouseDown}
        onDoubleClick={() => {
          if (sidebarCollapsed) return;
          setSidebarWidth(SIDEBAR_DEFAULT);
          window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(SIDEBAR_DEFAULT));
        }}
        title="Drag to resize (double-click to reset)"
        style={{ width: sidebarCollapsed ? 0 : 4 }}
        className="cursor-col-resize bg-gray-200 dark:bg-gray-700 hover:bg-blue-500 dark:hover:bg-blue-500 transition-[width,background-color] duration-300 ease-in-out flex-shrink-0 overflow-hidden"
      />

      {/* Re-open strip — width animates 0 → 28 when collapsed. Always in
          the DOM so the icon can fade in via the same transition. */}
      <button
        type="button"
        onClick={toggleSidebar}
        data-testid="kg-sidebar-expand"
        title="Show controls panel"
        aria-label="Show controls panel"
        style={{ width: sidebarCollapsed ? 28 : 0 }}
        className="flex-shrink-0 overflow-hidden flex items-start justify-center pt-2 border-r border-gray-200 dark:border-gray-700 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-[width] duration-300 ease-in-out"
      >
        <PanelLeftOpen size={16} />
      </button>

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
              <button
                type="button"
                onClick={() => setHelpOpen(true)}
                data-testid="kg-help-button"
                title="Help (Shift+/)"
                aria-label="Open Knowledge Graph help"
                className="p-1.5 rounded-md text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 border border-gray-200 dark:border-gray-700 transition-colors"
              >
                <HelpCircle size={18} />
              </button>
              {nextCursor && (
                <button
                  type="button"
                  onClick={handleLoadMore}
                  disabled={loadingMore}
                  data-testid="kg-load-more"
                  className="px-3 py-1.5 rounded-md text-xs bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-wait"
                >
                  {loadingMore
                    ? 'Loading…'
                    : `Load more (${nodes.length}${nextCursor ? '+' : ''})`}
                </button>
              )}
            </div>
            <GraphCanvas
              nodes={nodes}
              edges={edges}
              filters={filters}
              onSelect={setSelectedNode}
              initialSelectedNodeId={selectedNode?.id ?? null}
              onClearFilters={handleClearFilters}
              onAdjustRelevance={handleAdjustRelevance}
              onOpenSpec={handleOpenSpec}
              onShowDetails={setModalNode}
              refitTrigger={refitTrigger}
            />
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

      {helpOpen && <KGHelpModal onClose={() => setHelpOpen(false)} />}

      {/* Queue progress now surfaced globally via GlobalKGActivityIndicator
          in App.tsx — removed the per-page toast to avoid duplication. */}
    </div>
  );
}
