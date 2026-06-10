/**
 * GraphCanvas — interactive KG visualization rendered with Sigma.js (WebGL).
 *
 * Rendering migrated from React Flow to Sigma + graphology + ForceAtlas2
 * (worker) — the same stack used by Marginalia's GraphView — for a modern,
 * fluid and responsive canvas that stays smooth at thousands of nodes.
 *
 * Every capability of the previous React Flow canvas is preserved:
 *   - filters (types / edgeTypes / minRelevance / searchQuery) — client-side
 *   - selection interaction matrix (AC-4):
 *       click on node      → toggle selection (clicking same node clears it)
 *       double-click node  → emit onSelect(node) (opens the detail panel)
 *       click empty pane   → clear selection
 *   - hover tooltip after 500ms (AC-7) + single-click NodePreviewPanel (AC-8)
 *   - node dragging with positions kept until the next data reload
 *   - viewport refit on node-set change and on `refitTrigger`
 *   - dark/light theme reactivity (Tailwind class strategy)
 *   - filtered-empty state with clear-filters / lower-relevance CTAs (AC-19)
 *   - MiniMap (custom canvas — colors per NODE_TYPE_CONFIG) + zoom controls
 *
 * New capabilities inherited from the Marginalia renderer:
 *   - animated ForceAtlas2 layout in a Web Worker (with settling indicator
 *     and an explicit "Re-run layout" action)
 *   - hover dimming: everything not adjacent to the hovered node fades
 *   - edge hover highlight with forced edge labels
 *   - WebGL-unavailable fallback that keeps the node list inspectable (an
 *     accessible list with the same selection semantics, instead of a crash)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Graph from 'graphology';
import { Sigma } from 'sigma';
import FA2Layout from 'graphology-layout-forceatlas2/worker';
import { inferSettings } from 'graphology-layout-forceatlas2';
import { Loader2, Maximize, MonitorX, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react';
import type { KGNode, KGEdge, KGNodeType, KGEdgeType } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG, EDGE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeTooltip } from './NodeTooltip';
import { NodePreviewPanel } from './NodePreviewPanel';
import { SigmaMiniMap } from './graph/SigmaMiniMap';

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

// Sigma renders via WebGL and throws the moment it can't get a context —
// headless browsers (jsdom in tests), GPU disabled. Probe up front so we can
// render an accessible fallback instead of crashing the whole SPA.
function hasWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return Boolean(
      canvas.getContext('webgl2') ||
        canvas.getContext('webgl') ||
        canvas.getContext('experimental-webgl'),
    );
  } catch {
    return false;
  }
}

// Deterministic-ish initial placement on a circle. Sigma needs x/y on every
// node before render or it piles everything at the origin; FA2 then spreads them.
function seedCoords(i: number, n: number): { x: number; y: number } {
  const a = (2 * Math.PI * i) / Math.max(n, 1);
  const r = 50 + (i % 7) * 8;
  return { x: Math.cos(a) * r, y: Math.sin(a) * r };
}

// Degree → node radius. Sub-linear (sqrt) so a few very-high-degree hubs
// don't dwarf everything; clamped to a sane pixel range for sigma.
function nodeSize(degree: number): number {
  const r = 4 + Math.sqrt(degree) * 1.8;
  return Math.min(r, 22);
}

const LABEL_MAX = 42;
function truncateLabel(title: string): string {
  return title.length > LABEL_MAX ? `${title.slice(0, LABEL_MAX - 1)}…` : title;
}

const THEME = {
  light: {
    dimNode: '#E5E7EB',
    dimEdge: '#F1F5F9',
    edge: '#CBD5E1',
    edgeHover: '#475569',
    label: '#111827',
    edgeLabel: '#64748B',
  },
  dark: {
    dimNode: '#1F2937',
    dimEdge: '#111827',
    edge: '#334155',
    edgeHover: '#94A3B8',
    label: '#E5E7EB',
    edgeLabel: '#94A3B8',
  },
} as const;

const LAYOUT_SETTLE_MS = 2500;

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
  const [settling, setSettling] = useState(false);
  // Evaluated once — WebGL support doesn't change within a page load.
  const [webglOk] = useState(hasWebGL);
  // Bumped by the "Re-run layout" button to re-fling node positions.
  const [layoutEpoch, setLayoutEpoch] = useState(0);

  // Imperative sigma/graphology handles — kept out of React state on purpose.
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  // Read inside sigma reducers without re-binding them every render.
  const selectedRef = useRef<string | null>(selectedId);
  selectedRef.current = selectedId;
  const hoveredSigmaRef = useRef<string | null>(null);
  const hoveredEdgeRef = useRef<string | null>(null);
  const isDarkRef = useRef(isDark);
  isDarkRef.current = isDark;
  // True when the next data rebuild should re-center the camera.
  const freshLoadRef = useRef(true);
  // Drag overrides — user-moved positions persist across filter re-layouts
  // but NOT across full graph reloads (no backend persistence per Fase 1).
  const dragOverridesRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  // Latest node list for event handlers (avoid stale closures in sigma events).
  const nodesRef = useRef<KGNode[]>(nodes);
  nodesRef.current = nodes;
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  // Allow parents to programmatically clear/set selection through a prop change.
  useEffect(() => {
    setSelectedId(initialSelectedNodeId);
  }, [initialSelectedNodeId]);

  // React to theme toggles (Tailwind class strategy) AND OS-level changes.
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

  // Degree per visible node — drives node size (Marginalia convention).
  const degrees = useMemo(() => {
    const map = new Map<string, number>();
    for (const e of filteredEdges) {
      map.set(e.source, (map.get(e.source) ?? 0) + 1);
      map.set(e.target, (map.get(e.target) ?? 0) + 1);
    }
    return map;
  }, [filteredEdges]);

  // ── sigma lifecycle ────────────────────────────────────────────────────────
  // The container div unmounts whenever the filtered set goes empty (the
  // early-return empty state) and remounts when nodes come back — so sigma's
  // lifetime is tied to the CONTAINER via a callback ref, not to the
  // component mount. A plain effect would leave a renderer bound to a
  // detached div after an empty→populated round-trip (blank canvas).
  const teardownRef = useRef<(() => void) | null>(null);
  const [sigmaEpoch, setSigmaEpoch] = useState(0);
  const containerCallbackRef = useCallback((el: HTMLDivElement | null) => {
    teardownRef.current?.();
    teardownRef.current = null;
    sigmaRef.current = null;
    graphRef.current = null;
    containerRef.current = el;
    if (!el || !webglOk) return;
    const graph = new Graph({ multi: true });
    graphRef.current = graph;
    const renderer = new Sigma(graph, el, {
      // The container can be mid-layout (width 0) at mount; the
      // ResizeObserver below resizes the renderer as soon as it settles.
      allowInvalidContainer: true,
      renderLabels: true,
      renderEdgeLabels: true,
      enableEdgeEvents: true,
      labelRenderedSizeThreshold: 6,
      labelFont: 'ui-sans-serif, system-ui, sans-serif',
      labelSize: 11,
      edgeLabelFont: 'ui-sans-serif, system-ui, sans-serif',
      edgeLabelSize: 9,
      minCameraRatio: 0.05,
      maxCameraRatio: 12,
      // Theme-dependent colors resolved per render via reducers below.
      labelColor: { color: isDarkRef.current ? THEME.dark.label : THEME.light.label },
      edgeLabelColor: { color: isDarkRef.current ? THEME.dark.edgeLabel : THEME.light.edgeLabel },
    });
    sigmaRef.current = renderer;
    const resizeObserver =
      typeof ResizeObserver !== 'undefined'
        ? new ResizeObserver(() => {
            renderer.resize();
            renderer.refresh();
          })
        : null;
    resizeObserver?.observe(el);

    // ── node dragging (capability preserved from React Flow) ────────────────
    // While dragging we move the node to the cursor's graph position and
    // suppress both camera panning and the click-toggle that follows mouseup.
    let draggedNode: string | null = null;
    let dragMoved = false;
    renderer.on('downNode', ({ node }) => {
      draggedNode = node;
      dragMoved = false;
    });
    const mouseCaptor = renderer.getMouseCaptor();
    mouseCaptor.on('mousemovebody', (e) => {
      if (!draggedNode) return;
      dragMoved = true;
      const pos = renderer.viewportToGraph(e);
      graph.setNodeAttribute(draggedNode, 'x', pos.x);
      graph.setNodeAttribute(draggedNode, 'y', pos.y);
      // Don't pan the camera while a node is being dragged.
      e.preventSigmaDefault();
      e.original.preventDefault();
      e.original.stopPropagation();
    });
    mouseCaptor.on('mouseup', () => {
      if (draggedNode && dragMoved) {
        dragOverridesRef.current.set(draggedNode, {
          x: graph.getNodeAttribute(draggedNode, 'x') as number,
          y: graph.getNodeAttribute(draggedNode, 'y') as number,
        });
      }
      draggedNode = null;
    });

    // ── hover (dim non-neighbors immediately; tooltip handled by React) ─────
    renderer.on('enterNode', ({ node }) => {
      hoveredSigmaRef.current = node;
      scheduleTooltip(node);
      renderer.refresh();
    });
    renderer.on('leaveNode', () => {
      hoveredSigmaRef.current = null;
      cancelTooltip();
      renderer.refresh();
    });
    renderer.on('enterEdge', ({ edge }) => {
      hoveredEdgeRef.current = edge;
      renderer.refresh();
    });
    renderer.on('leaveEdge', () => {
      hoveredEdgeRef.current = null;
      renderer.refresh();
    });

    // ── selection interaction matrix (AC-4, unchanged semantics) ────────────
    renderer.on('clickNode', ({ node }) => {
      if (dragMoved) return; // a drag is not a click
      setSelectedId((current) => (current === node ? null : node));
      if (onNodeClickRef.current) {
        const kgNode = nodesRef.current.find((n) => n.id === node) ?? null;
        onNodeClickRef.current(kgNode);
      }
    });
    renderer.on('doubleClickNode', ({ node, event }) => {
      event.preventSigmaDefault(); // don't zoom on double-click-select
      const kgNode = nodesRef.current.find((n) => n.id === node);
      if (kgNode && onSelectRef.current) onSelectRef.current(kgNode);
    });
    renderer.on('clickStage', () => {
      setSelectedId(null);
      onNodeClickRef.current?.(null);
    });

    // ── reducers: per-render appearance from hover/selection/theme ──────────
    // Graph data stays clean (idiomatic sigma v3); theme colors are resolved
    // here so a dark/light flip only needs a refresh, not a data rebuild.
    renderer.setSetting('nodeReducer', (node, data) => {
      const theme = isDarkRef.current ? THEME.dark : THEME.light;
      const cfg = NODE_TYPE_CONFIG[data.nodeType as KGNodeType];
      const res = { ...data };
      res.color = cfg ? (isDarkRef.current ? cfg.darkColor : cfg.color) : '#6B7280';
      const sel = selectedRef.current;
      const hov = hoveredSigmaRef.current;
      if (sel === node) {
        res.highlighted = true;
        res.zIndex = 2;
        res.size = (data.size as number) + 3;
      } else if (sel && graph.hasNode(sel)) {
        // Tiered fade (Spec 8 / S4.2): neighbors of the selection stay full;
        // everything else dims gently (not hidden).
        const neighbors = new Set(graph.neighbors(sel));
        if (!neighbors.has(node)) {
          res.color = theme.dimNode;
        }
      }
      if (hov && graph.hasNode(hov)) {
        const neighbors = new Set(graph.neighbors(hov));
        neighbors.add(hov);
        if (!neighbors.has(node)) {
          res.color = theme.dimNode;
          res.label = '';
        }
      }
      return res;
    });
    renderer.setSetting('edgeReducer', (edge, data) => {
      const theme = isDarkRef.current ? THEME.dark : THEME.light;
      const res = { ...data };
      const sel = selectedRef.current;
      const hov = hoveredSigmaRef.current;
      const hovEdge = hoveredEdgeRef.current;
      // Base stroke comes from EDGE_TYPE_CONFIG (stored on the edge); slightly
      // muted until hover/selection pulls it forward.
      res.color = (data.baseColor as string) ?? theme.edge;
      if (hovEdge === edge) {
        res.color = theme.edgeHover;
        res.size = 2.2;
        res.forceLabel = true;
        res.zIndex = 2;
      }
      if (sel && graph.hasNode(sel)) {
        const [s, t] = graph.extremities(edge);
        if (s === sel || t === sel) {
          res.size = Math.max((data.size as number) ?? 1, 1.8);
          res.forceLabel = true;
          res.zIndex = 1;
        } else {
          res.color = theme.dimEdge;
        }
      }
      if (hov && graph.hasNode(hov)) {
        const [s, t] = graph.extremities(edge);
        if (s === hov || t === hov) {
          res.forceLabel = true;
          res.zIndex = 2;
        } else {
          res.hidden = true;
        }
      }
      return res;
    });

    teardownRef.current = () => {
      resizeObserver?.disconnect();
      layoutRef.current?.kill();
      layoutRef.current = null;
      renderer.kill();
    };
    // A fresh renderer starts empty — bump the epoch so the data-rebuild
    // effect repopulates it (covers empty→populated container remounts).
    freshLoadRef.current = true;
    setSigmaEpoch((k) => k + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [webglOk]);

  // Tear the renderer down with the component (callback refs only fire with
  // null on unmount of the DOM node, which this covers too — but be safe).
  useEffect(() => {
    return () => {
      teardownRef.current?.();
      teardownRef.current = null;
      sigmaRef.current = null;
      graphRef.current = null;
    };
  }, []);

  // The reducers only run on a sigma render; selection/theme changes don't
  // move the camera, so force a refresh to repaint the styles.
  useEffect(() => {
    sigmaRef.current?.refresh();
  }, [selectedId, isDark]);

  // ── data rebuild + FA2 layout ──────────────────────────────────────────────
  useEffect(() => {
    const graph = graphRef.current;
    const renderer = sigmaRef.current;
    if (!graph || !renderer) return;

    // Stop any running layout before mutating the graph.
    layoutRef.current?.kill();
    layoutRef.current = null;

    // Preserve coords for nodes already placed (avoids a full re-fling on
    // filter changes); explicit re-layout (layoutEpoch) drops them.
    const prevCoords = new Map<string, { x: number; y: number }>();
    if (layoutEpoch === 0 || !freshLoadRef.current) {
      graph.forEachNode((id, attr) => {
        prevCoords.set(id, { x: attr.x as number, y: attr.y as number });
      });
    }

    graph.clear();
    hoveredSigmaRef.current = null;
    hoveredEdgeRef.current = null;
    filteredNodes.forEach((n, i) => {
      const c =
        dragOverridesRef.current.get(n.id) ??
        prevCoords.get(n.id) ??
        seedCoords(i, filteredNodes.length);
      graph.addNode(n.id, {
        x: c.x,
        y: c.y,
        size: nodeSize(degrees.get(n.id) ?? 0),
        label: truncateLabel(n.title || n.id),
        nodeType: n.node_type,
      });
    });
    for (const e of filteredEdges) {
      if (graph.hasNode(e.source) && graph.hasNode(e.target)) {
        const cfg = EDGE_TYPE_CONFIG[e.edge_type];
        graph.addEdgeWithKey(e.id || `${e.source}|${e.edge_type}|${e.target}`, e.source, e.target, {
          type: 'line',
          label: e.edge_type,
          // `contradicts` is always-noisy by spec (S4.3): thicker + its
          // own red so conflicts stay visible without animation.
          size: e.edge_type === 'contradicts' ? 2.2 : 1.2,
          baseColor: cfg?.color ?? '#9CA3AF',
        });
      }
    }
    renderer.refresh();

    if (graph.order < 2) {
      setSettling(false);
      return;
    }
    // Worker-based FA2 so the main thread never freezes; stop after settling.
    const layout = new FA2Layout(graph, {
      settings: { ...inferSettings(graph), slowDown: 10 },
    });
    layoutRef.current = layout;
    setSettling(true);
    layout.start();
    const recenter = freshLoadRef.current;
    freshLoadRef.current = false;
    const timer = window.setTimeout(() => {
      layout.stop();
      setSettling(false);
      if (recenter) renderer.getCamera().animatedReset({ duration: 300 });
    }, LAYOUT_SETTLE_MS);

    return () => {
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredNodes, filteredEdges, degrees, layoutEpoch, sigmaEpoch]);

  // Wipe drag overrides + recenter when the underlying data reloads (new
  // fetch / pagination) — same semantics as the React Flow implementation.
  useEffect(() => {
    dragOverridesRef.current = new Map();
    freshLoadRef.current = true;
  }, [nodes]);

  // Re-fit after a surrounding layout transition (sidebar collapse/expand)
  // completes. The 320ms delay matches the sidebar width transition.
  useEffect(() => {
    if (refitTrigger === 0 || filteredNodes.length === 0) return;
    const handle = window.setTimeout(() => {
      sigmaRef.current?.getCamera().animatedReset({ duration: 350 });
    }, 320);
    return () => window.clearTimeout(handle);
  }, [refitTrigger, filteredNodes.length]);

  // ── hover tooltip (500ms delay — AC-7 scrub guard) ─────────────────────────
  const hoverTimerRef = useRef<number | null>(null);
  const cancelTooltip = useCallback(() => {
    if (hoverTimerRef.current !== null) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
    setHoveredId(null);
  }, []);
  const scheduleTooltip = useCallback((id: string) => {
    if (hoverTimerRef.current !== null) window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      setHoveredId(id);
      hoverTimerRef.current = null;
    }, 500);
  }, []);

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

  const rerunLayout = useCallback(() => {
    dragOverridesRef.current = new Map();
    freshLoadRef.current = true;
    setLayoutEpoch((k) => k + 1);
  }, []);

  const zoomIn = useCallback(() => {
    sigmaRef.current?.getCamera().animatedZoom({ duration: 200 });
  }, []);
  const zoomOut = useCallback(() => {
    sigmaRef.current?.getCamera().animatedUnzoom({ duration: 200 });
  }, []);
  const fitView = useCallback(() => {
    sigmaRef.current?.getCamera().animatedReset({ duration: 300 });
  }, []);

  // Accessible fallback selection handlers (no-WebGL path keeps the same
  // selection semantics so the data stays explorable everywhere).
  const fallbackClick = useCallback(
    (n: KGNode) => {
      setSelectedId((current) => (current === n.id ? null : n.id));
      onNodeClick?.(n);
    },
    [onNodeClick],
  );
  const fallbackDoubleClick = useCallback(
    (n: KGNode) => {
      onSelect?.(n);
    },
    [onSelect],
  );

  // S5.4 — distinguish "no data yet" (nodes empty) from "filters hid everything"
  // (nodes > 0 but filteredNodes = 0).
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
      data-renderer={webglOk ? 'sigma' : 'fallback'}
    >
      {webglOk ? (
        <>
          {/* sigma mounts here — lifecycle bound to the container element */}
          <div ref={containerCallbackRef} className="absolute inset-0" data-testid="kg-sigma-container" />

          {/* layout status + re-run (Marginalia) */}
          <div className="absolute bottom-3 right-3 z-20 flex items-center gap-2">
            {settling && (
              <span
                className="flex items-center gap-1.5 text-[11px] text-blue-500 dark:text-blue-400"
                data-testid="kg-layout-settling"
              >
                <Loader2 size={12} className="animate-spin" /> settling layout…
              </span>
            )}
            <button
              type="button"
              onClick={rerunLayout}
              data-testid="kg-rerun-layout"
              title="Re-run force layout"
              className="flex items-center gap-1.5 rounded-md border border-gray-200 dark:border-gray-700 bg-white/90 dark:bg-gray-900/90 px-2 py-1 text-[11px] text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              <RotateCcw size={11} /> Re-run layout
            </button>
          </div>

          {/* zoom controls (React Flow Controls parity) */}
          <div
            className="absolute bottom-3 left-3 z-20 flex flex-col rounded-md border border-gray-200 dark:border-gray-700 bg-white/90 dark:bg-gray-900/90 shadow-sm overflow-hidden"
            data-testid="kg-zoom-controls"
          >
            <button
              type="button"
              onClick={zoomIn}
              aria-label="Zoom in"
              className="p-1.5 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              <ZoomIn size={14} />
            </button>
            <button
              type="button"
              onClick={zoomOut}
              aria-label="Zoom out"
              className="p-1.5 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 border-t border-gray-200 dark:border-gray-700"
            >
              <ZoomOut size={14} />
            </button>
            <button
              type="button"
              onClick={fitView}
              aria-label="Fit view"
              className="p-1.5 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 border-t border-gray-200 dark:border-gray-700"
            >
              <Maximize size={14} />
            </button>
          </div>

          {/* MiniMap (React Flow MiniMap parity — custom canvas) */}
          <SigmaMiniMap
            sigmaRef={sigmaRef}
            isDark={isDark}
            epoch={`${sigmaEpoch}:${filteredNodes.length}:${layoutEpoch}:${settling}`}
          />

          <p className="pointer-events-none absolute bottom-3 left-12 z-10 text-[10px] text-gray-400 dark:text-gray-600">
            scroll to zoom · drag to pan · drag node to move · click to preview · double-click for details
          </p>
        </>
      ) : (
        // WebGL unavailable → never instantiate sigma. Keep the data fully
        // explorable through an accessible list with the SAME selection
        // semantics (click = preview/toggle, double-click = detail panel).
        <div className="absolute inset-0 overflow-y-auto p-4" data-testid="kg-webgl-fallback">
          <div className="mb-3 flex items-center gap-2 text-amber-500 dark:text-amber-400">
            <MonitorX size={16} aria-hidden />
            <span className="text-xs">
              WebGL is unavailable — showing the {filteredNodes.length} visible node(s) as a list.
            </span>
          </div>
          <ul className="flex flex-col gap-1">
            {filteredNodes.map((n) => {
              const cfg = NODE_TYPE_CONFIG[n.node_type];
              const isSelected = n.id === selectedId;
              return (
                <li key={n.id}>
                  <button
                    type="button"
                    onClick={() => fallbackClick(n)}
                    onDoubleClick={() => fallbackDoubleClick(n)}
                    data-testid={`kg-node-${n.node_type.toLowerCase()}`}
                    data-node-id={n.id}
                    data-selected={isSelected ? 'true' : 'false'}
                    className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs ${
                      isSelected
                        ? 'bg-blue-50 dark:bg-blue-950/40 ring-1 ring-blue-400'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-800'
                    }`}
                  >
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: isDark ? cfg?.darkColor : cfg?.color }}
                      aria-hidden
                    />
                    <span className="truncate text-gray-800 dark:text-gray-200">{n.title}</span>
                    <span className="ml-auto shrink-0 text-[10px] text-gray-400">{n.node_type}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

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
