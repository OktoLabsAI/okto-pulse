import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  AlertCircle,
  Bug,
  CheckSquare,
  CircleDot,
  ExternalLink,
  FileText,
  FlaskConical,
  GitBranch,
  Lightbulb,
  Maximize2,
  Minimize2,
  RefreshCw,
  Route,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useModalStack } from '@/contexts/ModalStackContext';
import { useDashboardApi } from '@/services/api';
import { useDashboardStore } from '@/store/dashboard';
import type { LineageGraphNode, LineageGraphResponse } from '@/types';
import {
  LINEAGE_GRAPH_EVENT,
  type OpenLineageGraphDetail,
} from './lineageGraphEvents';

interface Props {
  boardId: string;
}

const STAGE_X = 290;
const NODE_Y = 136;

type LineageFlowNodeData = Record<string, unknown> & {
  lineageNode: LineageGraphNode;
  selected: boolean;
  onOpenDetails: (node: LineageGraphNode) => void;
};

type LineageFlowNode = Node<LineageFlowNodeData, 'lineage'>;

const stageLabels: Record<number, string> = {
  0: 'Ideation',
  1: 'Refinement',
  2: 'Spec',
  3: 'Sprint',
  4: 'Tasks / Tests',
  5: 'Bugs',
};

const relationshipLabels: Record<string, string> = {
  has_refinement: 'refines',
  direct_spec: 'spec',
  derived_spec: 'spec',
  has_sprint: 'sprint',
  contains_card: 'card',
  has_card: 'card',
  originates_bug: 'bug',
};

function nodeIcon(type: string) {
  switch (type) {
    case 'ideation':
      return <Lightbulb size={14} />;
    case 'refinement':
      return <GitBranch size={14} />;
    case 'spec':
      return <FileText size={14} />;
    case 'sprint':
      return <Route size={14} />;
    case 'test':
      return <FlaskConical size={14} />;
    case 'bug':
      return <Bug size={14} />;
    case 'task':
    case 'card':
      return <CheckSquare size={14} />;
    default:
      return <FileText size={14} />;
  }
}

const typeStyles: Record<string, {
  header: string;
  border: string;
  badge: string;
  miniMap: string;
}> = {
  ideation: {
    header: 'bg-amber-500/15 text-amber-300 border-amber-400/30',
    border: 'border-amber-400/45',
    badge: 'bg-amber-500/10 text-amber-200',
    miniMap: '#f59e0b',
  },
  refinement: {
    header: 'bg-sky-500/15 text-sky-300 border-sky-400/30',
    border: 'border-sky-400/45',
    badge: 'bg-sky-500/10 text-sky-200',
    miniMap: '#38bdf8',
  },
  spec: {
    header: 'bg-violet-500/15 text-violet-300 border-violet-400/30',
    border: 'border-violet-400/45',
    badge: 'bg-violet-500/10 text-violet-200',
    miniMap: '#8b5cf6',
  },
  sprint: {
    header: 'bg-blue-500/15 text-blue-300 border-blue-400/30',
    border: 'border-blue-400/45',
    badge: 'bg-blue-500/10 text-blue-200',
    miniMap: '#3b82f6',
  },
  task: {
    header: 'bg-emerald-500/15 text-emerald-300 border-emerald-400/30',
    border: 'border-emerald-400/45',
    badge: 'bg-emerald-500/10 text-emerald-200',
    miniMap: '#10b981',
  },
  card: {
    header: 'bg-emerald-500/15 text-emerald-300 border-emerald-400/30',
    border: 'border-emerald-400/45',
    badge: 'bg-emerald-500/10 text-emerald-200',
    miniMap: '#10b981',
  },
  test: {
    header: 'bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-400/30',
    border: 'border-fuchsia-400/45',
    badge: 'bg-fuchsia-500/10 text-fuchsia-200',
    miniMap: '#d946ef',
  },
  bug: {
    header: 'bg-rose-500/15 text-rose-300 border-rose-400/30',
    border: 'border-rose-400/45',
    badge: 'bg-rose-500/10 text-rose-200',
    miniMap: '#f43f5e',
  },
};

function getTypeStyle(type: string) {
  return typeStyles[type] || typeStyles.task;
}

function formatEntityType(type: string) {
  return type.replace(/_/g, ' ');
}

function formatStatus(status?: string | null) {
  return status ? status.replace(/_/g, ' ') : 'No status';
}

function LineageNode({ data }: NodeProps<LineageFlowNode>) {
  const { lineageNode: node, selected } = data;
  const style = getTypeStyle(node.entity_type);

  return (
    <div
      onDoubleClick={(event) => {
        event.stopPropagation();
        data.onOpenDetails(node);
      }}
      className={[
        'w-[236px] overflow-hidden rounded-lg border bg-white text-left shadow-sm',
        'dark:bg-gray-900 dark:shadow-black/40',
        style.border,
        selected ? 'ring-2 ring-cyan-400/40 shadow-cyan-500/20' : '',
      ].join(' ')}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !border !border-gray-300 !bg-gray-700 dark:!border-gray-600"
      />
      <div
        className={[
          'flex items-center gap-2 border-b px-2.5 py-2 text-[11px] font-semibold uppercase',
          'tracking-normal',
          style.header,
        ].join(' ')}
      >
        <span className="shrink-0">
          {nodeIcon(node.entity_type)}
        </span>
        <span className="truncate" title={formatEntityType(node.entity_type)}>
          {formatEntityType(node.entity_type)}
        </span>
      </div>
      <div className="px-2.5 py-2.5">
        <div
          className="line-clamp-2 min-h-[32px] overflow-hidden text-sm font-semibold leading-4 text-gray-900 dark:text-white"
          title={node.title}
        >
          {node.title}
        </div>
        <div className="mt-2 flex min-w-0 items-center justify-between gap-2">
          <span
            className={[
              'inline-flex min-w-0 max-w-full items-center gap-1 rounded px-1.5 py-0.5 text-[10px]',
              'font-semibold uppercase tracking-normal',
              style.badge,
            ].join(' ')}
            title={formatStatus(node.status)}
          >
            <CircleDot size={9} className="shrink-0" />
            <span className="truncate">{formatStatus(node.status)}</span>
          </span>
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border !border-gray-300 !bg-gray-700 dark:!border-gray-600"
      />
    </div>
  );
}

const nodeTypes = { lineage: LineageNode };

function layoutNodes(
  graph: LineageGraphResponse,
  selectedNodeId: string | null,
  onOpenDetails: (node: LineageGraphNode) => void,
): LineageFlowNode[] {
  const groups = new Map<number, LineageGraphNode[]>();
  graph.nodes.forEach((node) => {
    const stage = Number.isFinite(node.stage) ? node.stage : 5;
    groups.set(stage, [...(groups.get(stage) || []), node]);
  });

  return graph.nodes.map((node) => {
    const stageNodes = groups.get(node.stage) || [];
    const index = stageNodes.findIndex((item) => item.id === node.id);
    const yOffset = -((stageNodes.length - 1) * NODE_Y) / 2;
    const selected = selectedNodeId === node.id;
    return {
      id: node.id,
      type: 'lineage',
      data: { lineageNode: node, selected, onOpenDetails },
      position: {
        x: node.stage * STAGE_X,
        y: yOffset + index * NODE_Y,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      draggable: false,
      style: { width: 236 },
    };
  });
}

function layoutEdges(
  graph: LineageGraphResponse,
  selectedNodeId: string | null,
): Edge[] {
  return graph.edges.map((edge) => {
    const selectedPath =
      !selectedNodeId || edge.source === selectedNodeId || edge.target === selectedNodeId;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      label: relationshipLabels[edge.relationship] || edge.relationship,
      animated: selectedPath && Boolean(selectedNodeId),
      markerEnd: { type: MarkerType.ArrowClosed },
      style: {
        stroke: selectedPath ? '#22d3ee' : '#94a3b8',
        strokeWidth: selectedPath ? 2.2 : 1.4,
        opacity: selectedPath ? 1 : 0.32,
      },
      labelStyle: {
        fill: selectedPath ? '#0891b2' : '#64748b',
        fontSize: 11,
        fontWeight: 600,
      },
      labelBgStyle: {
        fill: 'rgba(15, 23, 42, 0.72)',
        fillOpacity: 0.9,
      },
      labelBgPadding: [6, 3] as [number, number],
      labelBgBorderRadius: 4,
    };
  });
}

function miniMapNodeColor(node: Node) {
  const lineageNode = (node.data as LineageFlowNodeData).lineageNode;
  return getTypeStyle(lineageNode?.entity_type || 'task').miniMap;
}

export function LineageGraphModal({ boardId }: Props) {
  const api = useDashboardApi();
  const { push } = useModalStack();
  const openCardModal = useDashboardStore((s) => s.openCardModal);
  const [request, setRequest] = useState<OpenLineageGraphDetail | null>(null);
  const [graph, setGraph] = useState<LineageGraphResponse | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<OpenLineageGraphDetail>).detail;
      if (!detail?.entityId || !detail.entityType) return;
      setRequest(detail);
      setGraph(null);
      setSelectedNodeId(null);
      setError(null);
    };
    window.addEventListener(LINEAGE_GRAPH_EVENT, handler);
    return () => window.removeEventListener(LINEAGE_GRAPH_EVENT, handler);
  }, []);

  const loadGraph = async () => {
    if (!request) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.getLineageGraph(
        boardId,
        request.entityType,
        request.entityId,
        false,
      );
      setGraph(data);
      const selected = data.nodes.find((node) => (
        node.entity_id === request.entityId
        && node.entity_type === request.entityType
      ));
      setSelectedNodeId(selected?.id || null);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load lineage graph';
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadGraph();
  }, [request?.entityId, request?.entityType, boardId]);

  const openNodeDetails = useCallback((source: LineageGraphNode | null) => {
    if (!source) return;
    if (['task', 'test', 'bug', 'card'].includes(source.entity_type)) {
      openCardModal(source.entity_id);
      push({ type: 'card', id: source.entity_id });
      return;
    }
    if (!['ideation', 'refinement', 'spec', 'sprint'].includes(source.entity_type)) return;
    push({
      type: source.entity_type as 'ideation' | 'refinement' | 'spec' | 'sprint',
      id: source.entity_id,
    });
  }, [openCardModal, push]);

  const nodes = useMemo(
    () => (graph ? layoutNodes(graph, selectedNodeId, openNodeDetails) : []),
    [graph, selectedNodeId, openNodeDetails],
  );
  const edges = useMemo(
    () => (graph ? layoutEdges(graph, selectedNodeId) : []),
    [graph, selectedNodeId],
  );
  const selectedNode = useMemo(
    () => graph?.nodes.find((node) => node.id === selectedNodeId) || null,
    [graph, selectedNodeId],
  );

  const handleNodeDoubleClick: NodeMouseHandler<LineageFlowNode> = (_, node) => {
    if (!graph) return;
    const source = graph.nodes.find((item) => item.id === node.id) || null;
    openNodeDetails(source);
  };

  if (!request) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div
        className={[
          'flex flex-col overflow-hidden bg-white dark:bg-gray-900 shadow-2xl',
          fullscreen
            ? 'h-screen w-screen rounded-none'
            : 'h-[min(900px,92vh)] w-[min(1500px,96vw)] rounded-xl',
        ].join(' ')}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
              <GitBranch size={16} className="text-cyan-500" />
              SDLC Lineage
            </div>
            <div className="mt-0.5 truncate text-xs text-gray-500 dark:text-gray-400">
              {graph?.root_ideation.title || request.entityType}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={loadGraph}
              disabled={loading}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="Refresh"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <button
              type="button"
              onClick={() => setFullscreen((value) => !value)}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button
              type="button"
              onClick={() => setRequest(null)}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="Close"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="relative flex-1 bg-gray-50 dark:bg-gray-950">
          {loading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 text-sm text-gray-500 backdrop-blur-sm dark:bg-gray-950/70 dark:text-gray-400">
              Loading...
            </div>
          )}
          {error && (
            <div className="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/60 dark:text-red-300">
              <AlertCircle size={16} />
              {error}
            </div>
          )}
          {graph && (
            <>
              <style>
                {`
                  .lineage-flow .react-flow__controls,
                  .lineage-flow .react-flow__minimap {
                    background: rgba(15, 23, 42, 0.94);
                    border: 1px solid rgba(51, 65, 85, 0.9);
                    border-radius: 8px;
                    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
                  }
                  .lineage-flow .react-flow__controls-button {
                    background: rgba(15, 23, 42, 0.94);
                    border-bottom: 1px solid rgba(51, 65, 85, 0.9);
                    color: #cbd5e1;
                    fill: #cbd5e1;
                  }
                  .lineage-flow .react-flow__controls-button:hover {
                    background: rgba(30, 41, 59, 0.96);
                    color: #67e8f9;
                    fill: #67e8f9;
                  }
                  .lineage-flow .react-flow__controls-button svg {
                    fill: currentColor;
                  }
                  .lineage-flow .react-flow__minimap-mask {
                    fill: rgba(8, 13, 24, 0.62);
                  }
                `}
              </style>
              <div className="absolute left-4 top-4 z-10 flex max-w-[420px] gap-2 overflow-x-auto rounded-lg border border-gray-200 bg-white/95 px-2 py-1.5 shadow-sm dark:border-gray-800 dark:bg-gray-900/95">
                {Object.entries(stageLabels).map(([stage, label]) => (
                  <span
                    key={stage}
                    className="whitespace-nowrap rounded bg-gray-100 px-2 py-1 text-[11px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                  >
                    {label}
                  </span>
                ))}
              </div>
              {selectedNode && (
                <div className="absolute right-4 top-4 z-10 max-w-sm rounded-lg border border-gray-200 bg-white/95 p-3 shadow-sm dark:border-gray-800 dark:bg-gray-900/95">
                  <div className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
                    <span className="text-cyan-600 dark:text-cyan-300">
                      {nodeIcon(selectedNode.entity_type)}
                    </span>
                    <span className="truncate">{selectedNode.title}</span>
                  </div>
                  <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {selectedNode.entity_type}
                    {selectedNode.status ? ` / ${selectedNode.status}` : ''}
                  </div>
                  <button
                    type="button"
                    onClick={() => openNodeDetails(selectedNode)}
                    className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-cyan-600 px-2.5 py-1.5 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-cyan-500"
                  >
                    <ExternalLink size={13} />
                    Show details
                  </button>
                </div>
              )}
              <ReactFlow
                className="lineage-flow"
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.22 }}
                minZoom={0.25}
                maxZoom={1.6}
                onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                onPaneClick={() => setSelectedNodeId(null)}
                onNodeDoubleClick={handleNodeDoubleClick}
                proOptions={{ hideAttribution: true }}
              >
                <Background color="#64748b" gap={24} size={1} />
                <MiniMap
                  pannable
                  zoomable
                  nodeColor={miniMapNodeColor}
                  nodeStrokeColor="#0f172a"
                  nodeStrokeWidth={3}
                />
                <Controls showInteractive={false} />
              </ReactFlow>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
