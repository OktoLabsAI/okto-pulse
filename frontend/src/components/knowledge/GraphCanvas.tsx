/**
 * GraphCanvas — interactive KG visualization using React Flow (@xyflow/react).
 *
 * Spec 8 / Sprint 3 + Sprint 4 + Sprint 5.
 * - Positions come from the memoized force simulation (./graph/forceLayout).
 * - Node components come from ./nodes, dispatched via the module-scope
 *   `nodeTypes` map so React Flow never re-mounts them.
 * - Selection state lives *inside* GraphCanvas (useState) so selection
 *   never triggers a parent-level refetch. Parents are notified via the
 *   optional `onSelect` prop (double-click) or `onNodeClick` (any click).
 *
 * Selection interaction matrix (AC-4):
 *   click on node      → toggle selection (clicking same node clears it)
 *   double-click node  → emit onSelect(node)
 *   click empty pane   → clear selection
 *
 * Sprint 5 additions:
 *   hover on a node    → NodeTooltip appears bottom-left (AC-7)
 *   single-click       → NodePreviewPanel appears top-left (AC-8)
 *   MiniMap            → uses NODE_TYPE_CONFIG color / darkColor per theme (AC-18)
 *   filtered-empty     → data-empty-state="filtered" + clear-filters CTA (AC-19)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node as RFNode,
  type Edge as RFEdge,
  type NodeMouseHandler,
  type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { KGNode, KGEdge, KGNodeType, KGEdgeType } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG, EDGE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { nodeTypes } from './nodes';
import type { KGNodeData } from './nodes/types';
import { computeForceLayout } from './graph/forceLayout';
import { NodeTooltip } from './NodeTooltip';
import { NodePreviewPanel } from './NodePreviewPanel';

export interface GraphCanvasFilters {
  types: KGNodeType[];
  edgeTypes: KGEdgeType[];
  /** Minimum relevance_score (0..1) — slider in the controls panel.
   *  Operates on KGNode.relevance_score (the conceptually-correct field
   *  shown in the node detail panel as "Relevance"). */
  minRelevance: number;
  searchQuery: string;
}

interface Props {
  nodes: KGNode[];
  edges: KGEdge[];
  filters: GraphCanvasFilters;
  /** Fired on every single-click (used by parent to clear detail panel if needed). */
  onNodeClick?: (node: KGNode | null) => void;
  /** Fired on double-click — the canonical "open detail panel" signal. */
  onSelect?: (node: KGNode) => void;
  /** Optional initial selection (e.g. deep-link). Updates push new value into internal state. */
  initialSelectedNodeId?: string | null;
  /** When `filteredNodes.length === 0` but `nodes.length > 0`, parent can offer to reset filters (S5.4). */
  onClearFilters?: () => void;
  /** Lower the relevance threshold to a specific value (used by the empty-state CTA). */
  onAdjustRelevance?: (value: number) => void;
  /** Bumped by parent (e.g. when the sidebar collapses/expands) to request a
   *  delayed re-fit once the surrounding layout transition has settled. */
  refitTrigger?: number;
  /** Navigate to a spec reference when "Open in spec" is clicked from the preview panel (S5.2). */
  onOpenSpec?: (specRef: string) => void;
  /** Promote the inline preview to a full modal when "Show more" is clicked. */
  onShowDetails?: (node: KGNode) => void;
}

function prefersDarkMode(): boolean {
  if (typeof window === 'undefined' || typeof document === 'undefined') return false;
  // The app uses Tailwind's `class` strategy: dark mode is signalled by a
  // `dark` class on <html>, not by the OS preference. Check that first and
  // fall back to the OS hint only when the class isn't present.
  if (document.documentElement.classList.contains('dark')) return true;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

export function GraphCanvas({
  nodes,
  edges,
  filters,
  onNodeClick,
  onSelect,
  initialSelectedNodeId = null,
  onClearFilters,
  onAdjustRelevance,
  onOpenSpec,
  onShowDetails,
  refitTrigger = 0,
}: Props) {
  // Selection is internal to the canvas — parent lives independently of it.
  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedNodeId);
  // Hover state drives the NodeTooltip (S5.1 / AC-7).
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [isDark, setIsDark] = useState<boolean>(() => prefersDarkMode());
  // Drag overrides — user-moved positions persist across re-layouts but NOT
  // across full graph reloads (no backend persistence per Fase 1 decision).
  const dragOverridesRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const [dragTick, setDragTick] = useState(0);
  // ReactFlow instance handle so we can refit the viewport when the node set
  // changes (e.g. after "Load more" appends a new page). React Flow only
  // runs `fitView` once on mount; without this, newly-loaded nodes drift
  // off-screen.
  const rfInstanceRef = useRef<ReactFlowInstance<RFNode<KGNodeData>, RFEdge> | null>(null);

  // Allow parents to programmatically clear/set selection through a prop change.
  useEffect(() => {
    setSelectedId(initialSelectedNodeId);
  }, [initialSelectedNodeId]);

  // React to theme toggles (Tailwind class strategy) AND OS-level changes
  // so the MiniMap colors stay in sync regardless of which mechanism flipped
  // the theme.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const sync = () => setIsDark(prefersDarkMode());
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    });
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    mq?.addEventListener?.('change', sync);
    return () => {
      observer.disconnect();
      mq?.removeEventListener?.('change', sync);
    };
  }, []);

  const filteredNodes = useMemo(() => {
    let result = nodes;
    if (filters.types.length > 0) {
      result = result.filter((n) => filters.types.includes(n.node_type));
    }
    if (filters.minRelevance > 0) {
      result = result.filter((n) => (n.relevance_score ?? 0) >= filters.minRelevance);
    }
    if (filters.searchQuery) {
      const q = filters.searchQuery.toLowerCase();
      result = result.filter(
        (n) =>
          n.title.toLowerCase().includes(q) ||
          (n.content?.toLowerCase().includes(q) ?? false),
      );
    }
    return result;
  }, [nodes, filters]);

  const filteredNodeIds = useMemo(
    () => new Set(filteredNodes.map((n) => n.id)),
    [filteredNodes],
  );

  const filteredEdges = useMemo(() => {
    return edges.filter((e) => {
      if (!filteredNodeIds.has(e.source) || !filteredNodeIds.has(e.target)) {
        return false;
      }
      if (filters.edgeTypes.length > 0 && !filters.edgeTypes.includes(e.edge_type)) {
        return false;
      }
      return true;
    });
  }, [edges, filteredNodeIds, filters.edgeTypes]);

  // Layout only depends on graph *shape*, not on selection — AC-15.
  const positions = useMemo(
    () => computeForceLayout(filteredNodes, filteredEdges),
    [filteredNodes, filteredEdges],
  );

  const connectedNodeIds = useMemo(() => {
    if (!selectedId) return new Set<string>();
    const set = new Set<string>();
    for (const e of filteredEdges) {
      if (e.source === selectedId) set.add(e.target);
      if (e.target === selectedId) set.add(e.source);
    }
    return set;
  }, [filteredEdges, selectedId]);

  const rfNodes: RFNode<KGNodeData>[] = useMemo(() => {
    const hasSelection = selectedId !== null && filteredNodeIds.has(selectedId);
    return filteredNodes.map((n) => {
      const override = dragOverridesRef.current.get(n.id);
      const pos = override ?? positions.get(n.id) ?? { x: 0, y: 0 };
      const isSelected = n.id === selectedId;
      const isConnectedToSelected = connectedNodeIds.has(n.id);
      return {
        id: n.id,
        type: n.node_type,
        position: pos,
        data: {
          kgNode: n,
          isSelected,
          isConnectedToSelected,
          hasSelection,
        } satisfies KGNodeData,
      };
    });
    // dragTick forces recompute after a drag-stop commits a new override.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredNodes, positions, selectedId, connectedNodeIds, filteredNodeIds, dragTick]);

  const rfEdges: RFEdge[] = useMemo(() => {
    return filteredEdges.map((e) => {
      const cfg = EDGE_TYPE_CONFIG[e.edge_type];
      const touchesSelection =
        selectedId !== null && (e.source === selectedId || e.target === selectedId);
      return {
        id: e.id || `${e.source}-${e.edge_type}-${e.target}`,
        source: e.source,
        target: e.target,
        label: e.edge_type,
        type: 'default',
        // Edges animate when (a) they are *always* noisy (contradicts) or (b)
        // they are attached to the currently-selected node (S4.3).
        animated: e.edge_type === 'contradicts' || touchesSelection,
        style: {
          stroke: cfg?.color ?? '#9CA3AF',
          strokeWidth: touchesSelection ? 2 : 1.5,
          opacity: selectedId === null || touchesSelection ? 1 : 0.5,
        },
        labelStyle: { fontSize: 10, fill: '#6B7280' },
      };
    });
  }, [filteredEdges, selectedId]);

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_event, rfNode) => {
      setSelectedId((current) => (current === rfNode.id ? null : rfNode.id));
      if (onNodeClick) {
        const kgNode = nodes.find((n) => n.id === rfNode.id) ?? null;
        onNodeClick(kgNode);
      }
    },
    [nodes, onNodeClick],
  );

  const handleNodeDoubleClick: NodeMouseHandler = useCallback(
    (_event, rfNode) => {
      if (!onSelect) return;
      const kgNode = nodes.find((n) => n.id === rfNode.id);
      if (kgNode) onSelect(kgNode);
    },
    [nodes, onSelect],
  );

  const handleNodeMouseEnter: NodeMouseHandler = useCallback((_event, rfNode) => {
    setHoveredId(rfNode.id);
  }, []);

  const handleNodeMouseLeave: NodeMouseHandler = useCallback(() => {
    setHoveredId(null);
  }, []);

  const handlePaneClick = useCallback(() => {
    setSelectedId(null);
    onNodeClick?.(null);
  }, [onNodeClick]);

  const handleNodeDragStop: NodeMouseHandler = useCallback((_event, rfNode) => {
    dragOverridesRef.current.set(rfNode.id, {
      x: rfNode.position.x,
      y: rfNode.position.y,
    });
    setDragTick((t) => t + 1);
  }, []);

  // Wipe drag overrides when the graph shape changes (new load / filter that
  // changes the node set). Keeps user layout during selection/hover churn but
  // not across data refetches — AC for Fase 1.3.
  useEffect(() => {
    dragOverridesRef.current = new Map();
  }, [nodes]);

  // Re-fit the viewport when the visible node count changes (initial load,
  // pagination, filter changes). Debounced to coalesce React Flow's own
  // layout settling.
  useEffect(() => {
    if (filteredNodes.length === 0) return;
    const handle = window.setTimeout(() => {
      rfInstanceRef.current?.fitView({ padding: 0.2, duration: 350 });
    }, 80);
    return () => window.clearTimeout(handle);
  }, [filteredNodes.length]);

  // Re-fit after a surrounding layout transition (sidebar collapse/expand)
  // completes. The 320ms delay matches the sidebar width transition; the
  // fitView call itself runs another 350ms tween for a smooth re-frame.
  useEffect(() => {
    if (refitTrigger === 0 || filteredNodes.length === 0) return;
    const handle = window.setTimeout(() => {
      rfInstanceRef.current?.fitView({ padding: 0.2, duration: 350 });
    }, 320);
    return () => window.clearTimeout(handle);
  }, [refitTrigger, filteredNodes.length]);

  const selectedNode = useMemo(() => {
    if (!selectedId) return null;
    return filteredNodes.find((n) => n.id === selectedId) ?? null;
  }, [filteredNodes, selectedId]);

  const hoveredNode = useMemo(() => {
    if (!hoveredId) return null;
    return filteredNodes.find((n) => n.id === hoveredId) ?? null;
  }, [filteredNodes, hoveredId]);

  const handlePreviewClose = useCallback(() => {
    setSelectedId(null);
    onNodeClick?.(null);
  }, [onNodeClick]);

  // S5.4 — distinguish "no data yet" (nodes empty) from "filters hid everything"
  // (nodes > 0 but filteredNodes = 0). Parent controls the "yet" case; we render
  // the filtered variant in-canvas with a clear-filters CTA. When the relevance
  // slider is what's hiding everything, also offer to lower it to the maximum
  // relevance present so the user can see *something*.
  if (filteredNodes.length === 0) {
    const isFilteredEmpty = nodes.length > 0;
    const maxRelevancePresent = isFilteredEmpty
      ? nodes.reduce((m, n) => Math.max(m, n.relevance_score ?? 0), 0)
      : 0;
    const relevanceIsCulprit =
      isFilteredEmpty &&
      filters.minRelevance > 0 &&
      maxRelevancePresent < filters.minRelevance;
    return (
      <div
        className="h-full flex flex-col items-center justify-center gap-3"
        data-testid="kg-canvas-empty"
        data-empty-state={isFilteredEmpty ? 'filtered' : 'yet'}
      >
        <p className="text-gray-400 dark:text-gray-500 text-sm">
          {relevanceIsCulprit
            ? `No nodes match the relevance filter (max present: ${(maxRelevancePresent * 100).toFixed(0)}%, filter: ${(filters.minRelevance * 100).toFixed(0)}%).`
            : isFilteredEmpty
              ? 'No nodes match the current filters.'
              : 'No nodes to display.'}
        </p>
        {isFilteredEmpty && (
          <div className="flex gap-2">
            {relevanceIsCulprit && onAdjustRelevance && (
              <button
                type="button"
                onClick={() => onAdjustRelevance(maxRelevancePresent)}
                data-testid="kg-adjust-relevance"
                className="px-4 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700"
              >
                Lower to {(maxRelevancePresent * 100).toFixed(0)}%
              </button>
            )}
            {onClearFilters && (
              <button
                type="button"
                onClick={onClearFilters}
                data-testid="kg-clear-filters"
                className="px-4 py-1.5 text-xs rounded bg-gray-200 text-gray-800 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-100 dark:hover:bg-gray-600"
              >
                Clear filters
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      className="h-full w-full relative"
      role="region"
      aria-label={`Knowledge graph with ${filteredNodes.length} nodes`}
      data-testid="kg-canvas"
      data-selected-id={selectedId ?? ''}
      data-empty-state="populated"
    >
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onNodeMouseEnter={handleNodeMouseEnter}
        onNodeMouseLeave={handleNodeMouseLeave}
        onNodeDragStop={handleNodeDragStop}
        onPaneClick={handlePaneClick}
        onInit={(instance) => {
          rfInstanceRef.current = instance;
        }}
        colorMode={isDark ? 'dark' : 'light'}
        nodesDraggable
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} size={1} />
        <Controls />
        <MiniMap
          nodeColor={(node) => {
            const kgNode = (node.data as KGNodeData | undefined)?.kgNode;
            const nodeType = kgNode?.node_type ?? (node.type as KGNodeType);
            const cfg = NODE_TYPE_CONFIG[nodeType];
            if (!cfg) return '#6B7280';
            return isDark ? cfg.darkColor : cfg.color;
          }}
          maskColor={isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.1)'}
          pannable
          zoomable
          style={{
            height: 100,
            width: 160,
            backgroundColor: isDark ? '#0f172a' : '#ffffff',
            border: `1px solid ${isDark ? '#334155' : '#e5e7eb'}`,
            borderRadius: 6,
          }}
          data-testid="kg-minimap"
        />
      </ReactFlow>
      <NodeTooltip node={hoveredNode} />
      <NodePreviewPanel
        node={selectedNode}
        onClose={handlePreviewClose}
        onOpenSpec={onOpenSpec}
        onShowDetails={onShowDetails}
      />
    </div>
  );
}
