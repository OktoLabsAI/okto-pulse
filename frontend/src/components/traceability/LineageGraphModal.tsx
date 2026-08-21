import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  BookOpen,
  Bug,
  CheckSquare,
  CircleDot,
  ExternalLink,
  FileText,
  FlaskConical,
  GitBranch,
  Lightbulb,
  Link2,
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
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import {
  LINEAGE_GRAPH_EVENT,
  type OpenLineageGraphDetail,
} from './lineageGraphEvents';

interface Props {
  boardId: string;
}

const BASE_STAGE_X = 580;
const STAGE_X = (BASE_STAGE_X * 2) / 3;
const NODE_Y = 136;
const ACCESSIBLE_RELATION_LIMIT = 500;
const MAX_ANIMATED_DEPENDENCY_EDGES = 80;

type LineageViewMode = 'lineage' | 'dependencies';

type LineageDependencyOverlayResponse = LineageGraphResponse;

interface DependencyViewProjection {
  graph: LineageGraphResponse;
  positionStages: ReadonlyMap<string, number>;
}

type LineageFlowNodeData = Record<string, unknown> & {
  lineageNode: LineageGraphNode;
  selected: boolean;
  onOpenDetails: (node: LineageGraphNode) => void;
};

type LineageFlowNode = Node<LineageFlowNodeData, 'lineage'>;

const stageLabels = [
  { stage: -1, label: 'Stories' },
  { stage: 0, label: 'Ideation' },
  { stage: 1, label: 'Refinement' },
  { stage: 2, label: 'Spec' },
  { stage: 3, label: 'Sprint' },
  { stage: 4, label: 'Tasks / Tests' },
  { stage: 5, label: 'Bugs' },
] as const;

const relationshipLabels: Record<string, string> = {
  feeds_ideation: 'feeds',
  has_refinement: 'refines',
  direct_spec: 'spec',
  derived_spec: 'spec',
  has_sprint: 'sprint',
  contains_card: 'card',
  has_card: 'card',
  originates_bug: 'bug',
  regression_test: 'test',
  precedes: 'precedes',
};

function nodeIcon(type: string) {
  switch (type) {
    case 'story':
      return <BookOpen size={14} />;
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
  story: {
    header: 'bg-blue-500/15 text-blue-300 border-blue-400/30',
    border: 'border-blue-400/45',
    badge: 'bg-blue-500/10 text-blue-200',
    miniMap: '#60a5fa',
  },
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
        {node.dependency_role && (
          <span className="ml-auto shrink-0 rounded bg-black/10 px-1.5 py-0.5 text-[9px] font-bold normal-case dark:bg-white/10">
            {formatEntityType(node.dependency_role)}
          </span>
        )}
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

const cardEntityTypes = new Set(['task', 'test', 'bug', 'card']);

function semanticEntityType(entityType: string): string {
  return cardEntityTypes.has(entityType) ? 'card' : entityType;
}

function semanticEntityIdentity(node: Pick<LineageGraphNode, 'entity_type' | 'entity_id'>) {
  return `${semanticEntityType(node.entity_type)}\u0000${node.entity_id}`;
}

function lineageDependencyMembership(graph: LineageGraphResponse): string[] {
  return Array.from(new Set(
    graph.nodes.flatMap((node) => {
      const entityType = semanticEntityType(node.entity_type);
      return entityType === 'spec' || entityType === 'card'
        ? [`${entityType}\u0000${node.entity_id}`]
        : [];
    }),
  )).sort();
}

function dependencySubjectType(entityType: string): 'spec' | 'task' | null {
  if (entityType === 'spec') return 'spec';
  if (['task', 'test', 'bug', 'card'].includes(entityType)) return 'task';
  return null;
}

function isRequestedEntity(
  node: LineageGraphNode,
  request: OpenLineageGraphDetail,
): boolean {
  if (node.entity_id !== request.entityId) return false;
  const subjectType = dependencySubjectType(request.entityType);
  if (subjectType === 'spec') return node.entity_type === 'spec';
  if (subjectType === 'task') {
    return ['task', 'test', 'bug', 'card'].includes(node.entity_type);
  }
  return node.entity_type === request.entityType;
}

function assertDependencyGraphResponse(
  data: LineageDependencyOverlayResponse,
  lineageGraph: LineageGraphResponse,
  boardId: string,
  request: OpenLineageGraphDetail,
): void {
  const nodeIds = new Set(data.nodes.map((node) => node.id));
  const nodeById = new Map(data.nodes.map((node) => [node.id, node]));
  const semanticNodeIds = data.nodes.map(semanticEntityIdentity);
  const receivedMembership = Array.isArray(data.lineage_entities)
    ? data.lineage_entities.map((entity) => (
        entity
        && (entity.entity_type === 'spec' || entity.entity_type === 'card')
        && typeof entity.entity_id === 'string'
        && entity.entity_id
          ? `${entity.entity_type}\u0000${entity.entity_id}`
          : ''
      ))
    : [];
  const expectedMembership = lineageDependencyMembership(lineageGraph);
  const stableMembership = (
    receivedMembership.length === expectedMembership.length
    && receivedMembership.every((identity, index) => identity === expectedMembership[index])
  );
  const lineageNodeIds = Array.isArray(data.lineage_node_ids)
    ? data.lineage_node_ids
    : [];
  const receivedLineageNodeMembership = lineageNodeIds
    .map((nodeId) => nodeById.get(nodeId))
    .filter((node): node is LineageGraphNode => Boolean(node))
    .map(semanticEntityIdentity)
    .sort();
  const stableLineageNodeMembership = (
    receivedLineageNodeMembership.length === expectedMembership.length
    && receivedLineageNodeMembership.every(
      (identity, index) => identity === expectedMembership[index],
    )
  );
  const invalidEdge = data.edges.some((edge) => (
    edge.relationship !== 'precedes'
    || !nodeIds.has(edge.source)
    || !nodeIds.has(edge.target)
    || semanticEntityIdentity(nodeById.get(edge.source)!)
      === semanticEntityIdentity(nodeById.get(edge.target)!)
  ));
  if (
    data.view !== 'dependency'
    || data.dependency_scope !== 'lineage'
    || data.board_id !== boardId
    || semanticEntityType(data.selected.entity_type)
      !== semanticEntityType(request.entityType)
    || data.selected.entity_id !== request.entityId
    || nodeIds.size !== data.nodes.length
    || new Set(semanticNodeIds).size !== semanticNodeIds.length
    || !stableMembership
    || lineageNodeIds.length !== expectedMembership.length
    || new Set(lineageNodeIds).size !== lineageNodeIds.length
    || lineageNodeIds.some((nodeId) => !nodeIds.has(nodeId))
    || !stableLineageNodeMembership
    || invalidEdge
  ) {
    throw new Error(
      'The server returned an incompatible dependency graph. Refresh after updating Pulse.',
    );
  }
}

function dependencyPositionStages(
  graph: LineageGraphResponse,
  lineageNodeIds: ReadonlySet<string>,
): ReadonlyMap<string, number> {
  const dependencyEdges = graph.edges.filter((edge) => edge.relationship === 'precedes');
  if (dependencyEdges.length === 0) return new Map();

  const graphNodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const dependencyNodeIds = new Set<string>();
  const successors = new Map<string, Set<string>>();
  const undirected = new Map<string, Set<string>>();
  const indegree = new Map<string, number>();
  for (const edge of dependencyEdges) {
    dependencyNodeIds.add(edge.source);
    dependencyNodeIds.add(edge.target);
    successors.set(edge.source, successors.get(edge.source) || new Set());
    successors.set(edge.target, successors.get(edge.target) || new Set());
    undirected.set(edge.source, undirected.get(edge.source) || new Set());
    undirected.set(edge.target, undirected.get(edge.target) || new Set());
    indegree.set(edge.source, indegree.get(edge.source) || 0);
    indegree.set(edge.target, indegree.get(edge.target) || 0);
    if (!successors.get(edge.source)!.has(edge.target)) {
      successors.get(edge.source)!.add(edge.target);
      indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1);
    }
    undirected.get(edge.source)!.add(edge.target);
    undirected.get(edge.target)!.add(edge.source);
  }

  const ready = Array.from(dependencyNodeIds)
    .filter((nodeId) => (indegree.get(nodeId) || 0) === 0)
    .sort();
  const levels = new Map(Array.from(dependencyNodeIds, (nodeId) => [nodeId, 0]));
  const topological: string[] = [];
  while (ready.length > 0) {
    const current = ready.shift()!;
    topological.push(current);
    for (const dependentId of Array.from(successors.get(current) || []).sort()) {
      levels.set(
        dependentId,
        Math.max(levels.get(dependentId) || 0, (levels.get(current) || 0) + 1),
      );
      const nextIndegree = (indegree.get(dependentId) || 0) - 1;
      indegree.set(dependentId, nextIndegree);
      if (nextIndegree === 0) {
        ready.push(dependentId);
        ready.sort();
      }
    }
  }
  if (topological.length !== dependencyNodeIds.size) {
    throw new Error('The dependency overlay contains a cycle and cannot be displayed.');
  }

  const positionStages = new Map<string, number>();
  const visited = new Set<string>();
  for (const rootId of Array.from(dependencyNodeIds).sort()) {
    if (visited.has(rootId)) continue;
    const component: string[] = [];
    const pending = [rootId];
    visited.add(rootId);
    while (pending.length > 0) {
      const current = pending.pop()!;
      component.push(current);
      for (const adjacentId of undirected.get(current) || []) {
        if (!visited.has(adjacentId)) {
          visited.add(adjacentId);
          pending.push(adjacentId);
        }
      }
    }

    const lineageOffsets = component.flatMap((nodeId) => {
      const node = graphNodeById.get(nodeId);
      return lineageNodeIds.has(nodeId) && node && Number.isFinite(node.stage)
        ? [node.stage - (levels.get(nodeId) || 0)]
        : [];
    });
    const fallbackOffsets = component.flatMap((nodeId) => {
      const node = graphNodeById.get(nodeId);
      return node && Number.isFinite(node.stage)
        ? [node.stage - (levels.get(nodeId) || 0)]
        : [];
    });
    const offsets = (lineageOffsets.length > 0 ? lineageOffsets : fallbackOffsets)
      .sort((left, right) => left - right);
    const middle = Math.floor(offsets.length / 2);
    const medianOffset = offsets.length === 0
      ? 0
      : offsets.length % 2 === 1
        ? offsets[middle]
        : (offsets[middle - 1] + offsets[middle]) / 2;
    const offset = Math.round(medianOffset);
    component.forEach((nodeId) => {
      positionStages.set(nodeId, (levels.get(nodeId) || 0) + offset);
    });
  }
  return positionStages;
}

function mergeLineageDependencyOverlay(
  lineageGraph: LineageGraphResponse,
  dependencyGraph: LineageDependencyOverlayResponse,
): DependencyViewProjection {
  const nodes = [...lineageGraph.nodes];
  const lineageNodeIds = new Set(lineageGraph.nodes.map((node) => node.id));
  const usedNodeIds = new Set(lineageNodeIds);
  const identityToMergedNodeId = new Map<string, string>();
  lineageGraph.nodes.forEach((node) => {
    if (!identityToMergedNodeId.has(semanticEntityIdentity(node))) {
      identityToMergedNodeId.set(semanticEntityIdentity(node), node.id);
    }
  });

  const dependencyNodeIdToMergedNodeId = new Map<string, string>();
  for (const dependencyNode of dependencyGraph.nodes) {
    const identity = semanticEntityIdentity(dependencyNode);
    let mergedNodeId = identityToMergedNodeId.get(identity);
    if (!mergedNodeId) {
      mergedNodeId = dependencyNode.id;
      let suffix = 1;
      while (usedNodeIds.has(mergedNodeId)) {
        mergedNodeId = `dependency-overlay:${suffix}:${dependencyNode.id}`;
        suffix += 1;
      }
      const { dependency_role: _ignoredRole, ...dependencyOnlyNode } = dependencyNode;
      nodes.push({ ...dependencyOnlyNode, id: mergedNodeId });
      usedNodeIds.add(mergedNodeId);
      identityToMergedNodeId.set(identity, mergedNodeId);
    }
    dependencyNodeIdToMergedNodeId.set(dependencyNode.id, mergedNodeId);
  }

  const edges = [...lineageGraph.edges];
  const usedEdgeIds = new Set(edges.map((edge) => edge.id));
  for (const dependencyEdge of dependencyGraph.edges) {
    const source = dependencyNodeIdToMergedNodeId.get(dependencyEdge.source)!;
    const target = dependencyNodeIdToMergedNodeId.get(dependencyEdge.target)!;
    let edgeId = dependencyEdge.id;
    let suffix = 1;
    while (usedEdgeIds.has(edgeId)) {
      edgeId = `dependency-overlay:${suffix}:${dependencyEdge.id}`;
      suffix += 1;
    }
    usedEdgeIds.add(edgeId);
    edges.push({ ...dependencyEdge, id: edgeId, source, target });
  }

  const mergedGraph: LineageGraphResponse = {
    ...lineageGraph,
    view: 'dependency',
    nodes,
    edges,
    summary: {
      ...lineageGraph.summary,
      dependency_nodes: dependencyGraph.nodes.length,
      dependency_edges: dependencyGraph.edges.length,
    },
    warnings: Array.from(new Set([
      ...(lineageGraph.warnings || []),
      ...(dependencyGraph.warnings || []),
    ])),
  };
  return {
    graph: mergedGraph,
    positionStages: dependencyPositionStages(mergedGraph, lineageNodeIds),
  };
}

function layoutDependencyEdges(
  dependencyGraph: LineageGraphResponse,
  selectedNodeId: string | null,
): Edge[] {
  const animationAllowed = dependencyGraph.edges.length <= MAX_ANIMATED_DEPENDENCY_EDGES;
  return dependencyGraph.edges.map((edge) => {
    const selectedPath = !selectedNodeId
      || edge.source === selectedNodeId
      || edge.target === selectedNodeId;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      label: relationshipLabels.precedes,
      animated: animationAllowed && selectedPath && Boolean(selectedNodeId),
      markerEnd: { type: MarkerType.ArrowClosed, color: '#b45309' },
      style: {
        stroke: '#b45309',
        strokeWidth: selectedPath ? 2.6 : 2,
        strokeDasharray: '8 5',
        opacity: selectedPath ? 1 : 0.9,
      },
      labelStyle: {
        fill: '#78350f',
        fontSize: 11,
        fontWeight: 700,
      },
      labelBgStyle: {
        fill: '#fffbeb',
        fillOpacity: 0.96,
      },
      labelBgPadding: [6, 3] as [number, number],
      labelBgBorderRadius: 4,
    };
  });
}

function layoutNodes(
  graph: LineageGraphResponse,
  selectedNodeId: string | null,
  onOpenDetails: (node: LineageGraphNode) => void,
  positionStages: ReadonlyMap<string, number> = new Map(),
): LineageFlowNode[] {
  const groups = new Map<number, LineageGraphNode[]>();
  graph.nodes.forEach((node) => {
    const requestedStage = positionStages.get(node.id) ?? node.stage;
    const stage = Number.isFinite(requestedStage) ? requestedStage : 5;
    const stageNodes = groups.get(stage) || [];
    stageNodes.push(node);
    groups.set(stage, stageNodes);
  });

  return graph.nodes.map((node) => {
    const requestedStage = positionStages.get(node.id) ?? node.stage;
    const stage = Number.isFinite(requestedStage) ? requestedStage : 5;
    const stageNodes = groups.get(stage) || [];
    const index = stageNodes.findIndex((item) => item.id === node.id);
    const yOffset = -((stageNodes.length - 1) * NODE_Y) / 2;
    const selected = selectedNodeId === node.id;
    return {
      id: node.id,
      type: 'lineage',
      data: { lineageNode: node, selected, onOpenDetails },
      position: {
        x: stage * STAGE_X,
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

function canOpenDetails(node: LineageGraphNode | null) {
  if (!node) return false;
  return ['story', 'task', 'test', 'bug', 'card', 'ideation', 'refinement', 'spec', 'sprint'].includes(node.entity_type);
}

export function LineageGraphModal({ boardId }: Props) {
  const api = useDashboardApi();
  const { push } = useModalStack();
  const openCardModal = useDashboardStore((s) => s.openCardModal);
  const lineageLoadGeneration = useRef(0);
  const dependencyLoadGeneration = useRef(0);
  const [request, setRequest] = useState<OpenLineageGraphDetail | null>(null);
  const [graph, setGraph] = useState<LineageGraphResponse | null>(null);
  const [dependencyGraph, setDependencyGraph] = useState<LineageDependencyOverlayResponse | null>(null);
  const [lineageRevision, setLineageRevision] = useState(0);
  const [dependencyRevision, setDependencyRevision] = useState(0);
  const [viewMode, setViewMode] = useState<LineageViewMode>('lineage');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dependencyLoading, setDependencyLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dependencyError, setDependencyError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const { dialogRef, onKeyDown } = useDialogFocusTrap(
    Boolean(request),
    '[data-lineage-initial-focus]',
  );

  const closeModal = useCallback(() => {
    lineageLoadGeneration.current += 1;
    dependencyLoadGeneration.current += 1;
    setRequest(null);
  }, []);

  useEscapeToClose(closeModal, { enabled: Boolean(request) });

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<OpenLineageGraphDetail>).detail;
      if (!detail?.entityId || !detail.entityType) return;
      lineageLoadGeneration.current += 1;
      dependencyLoadGeneration.current += 1;
      setRequest(detail);
      setGraph(null);
      setDependencyGraph(null);
      setLineageRevision(0);
      setDependencyRevision(0);
      setViewMode('lineage');
      setSelectedNodeId(null);
      setError(null);
      setDependencyError(null);
      setDependencyLoading(false);
    };
    window.addEventListener(LINEAGE_GRAPH_EVENT, handler);
    return () => window.removeEventListener(LINEAGE_GRAPH_EVENT, handler);
  }, []);

  const loadGraph = useCallback(async () => {
    if (!request) return;
    const generation = ++lineageLoadGeneration.current;
    dependencyLoadGeneration.current += 1;
    setLoading(true);
    setError(null);
    setDependencyGraph(null);
    setDependencyError(null);
    setDependencyLoading(false);
    try {
      const data = await api.getLineageGraph(
        boardId,
        request.entityType,
        request.entityId,
        false,
      );
      if (generation !== lineageLoadGeneration.current) return;
      dependencyLoadGeneration.current += 1;
      setDependencyGraph(null);
      setDependencyError(null);
      setDependencyLoading(false);
      setGraph(data);
      setLineageRevision((revision) => revision + 1);
    } catch (err) {
      if (generation !== lineageLoadGeneration.current) return;
      const message = err instanceof Error ? err.message : 'Failed to load lineage graph';
      setError(message);
      toast.error(message);
    } finally {
      if (generation === lineageLoadGeneration.current) setLoading(false);
    }
  }, [api, boardId, request]);

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  const loadDependencyGraph = useCallback(async () => {
    if (!request || !graph || loading) return;
    const generation = ++dependencyLoadGeneration.current;
    setDependencyLoading(true);
    setDependencyError(null);
    try {
      const data = await api.getLineageGraph(
        boardId,
        request.entityType,
        request.entityId,
        false,
        'dependency',
        'lineage',
      );
      if (generation !== dependencyLoadGeneration.current) return;
      assertDependencyGraphResponse(data, graph, boardId, request);
      // Preflight the topology while the error can still be projected without
      // replacing the already-rendered lineage base.
      mergeLineageDependencyOverlay(graph, data);
      setDependencyGraph(data);
      setDependencyRevision((revision) => revision + 1);
    } catch (err) {
      if (generation !== dependencyLoadGeneration.current) return;
      const message = err instanceof Error
        ? err.message
        : 'Failed to load dependency graph';
      setDependencyGraph(null);
      setDependencyError(message);
      toast.error(message);
    } finally {
      if (generation === dependencyLoadGeneration.current) {
        setDependencyLoading(false);
      }
    }
  }, [api, boardId, graph, loading, request]);

  useEffect(() => {
    if (
      viewMode === 'dependencies'
      && graph
      && !loading
      && !dependencyGraph
      && !dependencyLoading
      && !dependencyError
    ) {
      void loadDependencyGraph();
    }
  }, [
    dependencyError,
    dependencyGraph,
    dependencyLoading,
    graph,
    loadDependencyGraph,
    loading,
    viewMode,
  ]);

  const openNodeDetails = useCallback((source: LineageGraphNode | null) => {
    if (!canOpenDetails(source)) return;
    if (!source) return;
    if (['task', 'test', 'bug', 'card'].includes(source.entity_type)) {
      openCardModal(source.entity_id);
      push({ type: 'card', id: source.entity_id });
      return;
    }
    if (source.entity_type === 'story') {
      push({ type: 'story', id: source.entity_id });
      return;
    }
    if (!['ideation', 'refinement', 'spec', 'sprint'].includes(source.entity_type)) return;
    push({
      type: source.entity_type as 'ideation' | 'refinement' | 'spec' | 'sprint',
      id: source.entity_id,
    });
  }, [openCardModal, push]);

  const dependencyProjection = useMemo(
    () => (graph && dependencyGraph
      ? mergeLineageDependencyOverlay(graph, dependencyGraph)
      : null),
    [dependencyGraph, graph],
  );
  const activeGraph = viewMode === 'dependencies'
    ? dependencyProjection?.graph || graph
    : graph;
  const activePositionStages = viewMode === 'dependencies'
    ? dependencyProjection?.positionStages
    : undefined;
  const dependencyViewSupported = Boolean(request && graph);
  const requestedNodeTitle = request
    ? graph?.nodes.find((node) => isRequestedEntity(node, request))?.title
    : undefined;
  const activeGraphTitle = graph?.root_ideation.title
    || requestedNodeTitle
    || request?.entityType
    || 'lineage';
  const activeGraphRevision = viewMode === 'dependencies'
    ? `${lineageRevision}:${dependencyRevision}`
    : String(lineageRevision);
  const accessibleDependencyRelationships = useMemo(() => {
    if (viewMode !== 'dependencies' || !dependencyGraph || !activeGraph) return [];
    const titlesByNodeId = new Map(
      activeGraph.nodes.map((node) => [node.id, node.title]),
    );
    return activeGraph.edges
      .filter((edge) => edge.relationship === 'precedes')
      .slice(0, ACCESSIBLE_RELATION_LIMIT)
      .map((edge) => ({
        id: edge.id,
        description: `${titlesByNodeId.get(edge.source) || edge.source} precedes ${titlesByNodeId.get(edge.target) || edge.target}`,
      }));
  }, [activeGraph, dependencyGraph, viewMode]);

  useEffect(() => {
    if (!request || !activeGraph) return;
    const selected = activeGraph.nodes.find((node) => isRequestedEntity(node, request));
    setSelectedNodeId(selected?.id || null);
  }, [activeGraph, request, viewMode]);

  const nodes = useMemo(
    () => (activeGraph
      ? layoutNodes(
          activeGraph,
          selectedNodeId,
          openNodeDetails,
          activePositionStages,
        )
      : []),
    [activeGraph, activePositionStages, selectedNodeId, openNodeDetails],
  );
  const edges = useMemo(
    () => {
      if (!activeGraph) return [];
      if (viewMode !== 'dependencies') return layoutEdges(activeGraph, selectedNodeId);
      const lineageEdges = activeGraph.edges.filter(
        (edge) => edge.relationship !== 'precedes',
      );
      const dependencyEdges = activeGraph.edges.filter(
        (edge) => edge.relationship === 'precedes',
      );
      return [
        ...layoutEdges({ ...activeGraph, edges: lineageEdges }, selectedNodeId),
        ...layoutDependencyEdges({ ...activeGraph, edges: dependencyEdges }, selectedNodeId),
      ];
    },
    [activeGraph, selectedNodeId, viewMode],
  );
  const selectedNode = useMemo(
    () => activeGraph?.nodes.find((node) => node.id === selectedNodeId) || null,
    [activeGraph, selectedNodeId],
  );
  const selectedResourceCounts = useMemo(() => {
    if (!selectedNode) return undefined;
    if (selectedNode.resource_counts) return selectedNode.resource_counts;
    if (!graph) return undefined;
    const graphCountsBelongToSelectedNode = (
      graph.selected.entity_type === selectedNode.entity_type
      && graph.selected.entity_id === selectedNode.entity_id
    );
    return graphCountsBelongToSelectedNode ? graph.resource_counts : undefined;
  }, [graph, selectedNode]);

  const handleNodeDoubleClick: NodeMouseHandler<LineageFlowNode> = (_, node) => {
    if (!activeGraph) return;
    const source = activeGraph.nodes.find((item) => item.id === node.id) || null;
    openNodeDetails(source);
  };

  if (!request) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="lineage-graph-title"
        aria-describedby="lineage-graph-subtitle"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={[
          'flex flex-col overflow-hidden bg-white dark:bg-gray-900 shadow-2xl',
          fullscreen
            ? 'h-screen w-screen rounded-none'
            : 'h-[min(900px,92vh)] w-[min(1500px,96vw)] rounded-xl',
        ].join(' ')}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-800">
          <div className="min-w-0">
            <div
              id="lineage-graph-title"
              className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white"
            >
              <GitBranch size={16} className="text-cyan-500" />
              SDLC Lineage
            </div>
            <div
              id="lineage-graph-subtitle"
              className="mt-0.5 truncate text-xs text-gray-500 dark:text-gray-400"
            >
              {activeGraphTitle}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {dependencyViewSupported && (
              <div
                role="group"
                aria-label="Graph view"
                className="mr-1 flex rounded-lg border border-gray-200 bg-gray-100 p-0.5 dark:border-gray-700 dark:bg-gray-800"
              >
                {([
                  ['lineage', 'Origin / derivation', GitBranch],
                  ['dependencies', 'Dependencies', Link2],
                ] as const).map(([mode, label, Icon]) => {
                  const active = viewMode === mode;
                  return (
                    <button
                      key={mode}
                      type="button"
                      aria-pressed={active}
                      aria-controls="lineage-graph-region"
                      onClick={() => setViewMode(mode)}
                      className={[
                        'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-semibold transition-colors',
                        active
                          ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white'
                          : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100',
                      ].join(' ')}
                    >
                      <Icon size={13} aria-hidden="true" />
                      {label}
                    </button>
                  );
                })}
              </div>
            )}
            <button
              type="button"
              onClick={() => {
                if (viewMode === 'dependencies') {
                  void loadDependencyGraph();
                } else {
                  void loadGraph();
                }
              }}
              disabled={loading || dependencyLoading}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="Refresh"
            >
              <RefreshCw
                size={16}
                className={loading || dependencyLoading ? 'animate-spin' : ''}
              />
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
              onClick={closeModal}
              data-lineage-initial-focus
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="Close"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div
          id="lineage-graph-region"
          role="region"
          aria-label={`${viewMode === 'dependencies' ? 'Dependencies' : 'Origin and derivation'} graph for ${activeGraphTitle}`}
          aria-busy={loading || (viewMode === 'dependencies' && dependencyLoading)}
          className="relative flex-1 bg-gray-50 dark:bg-gray-950"
        >
          {(loading || (viewMode === 'dependencies' && dependencyLoading)) && (
            <div
              role="status"
              aria-live="polite"
              className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 text-sm text-gray-500 backdrop-blur-sm dark:bg-gray-950/70 dark:text-gray-400"
            >
              {viewMode === 'dependencies' && dependencyLoading
                ? 'Loading dependency overlay...'
                : 'Loading...'}
            </div>
          )}
          {(viewMode === 'dependencies' ? dependencyError || error : error) && (
            <div
              role="alert"
              className="absolute left-4 top-4 z-20 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/60 dark:text-red-300"
            >
              <AlertCircle size={16} />
              <span>{viewMode === 'dependencies' ? dependencyError || error : error}</span>
              {viewMode === 'dependencies' && dependencyError && (
                <>
                  <button
                    type="button"
                    onClick={() => void loadDependencyGraph()}
                    className="ml-1 rounded border border-red-300 px-2 py-0.5 text-xs font-semibold hover:bg-red-100 dark:border-red-800 dark:hover:bg-red-900/40"
                  >
                    Retry overlay
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setViewMode('lineage');
                      void loadGraph();
                    }}
                    className="rounded border border-red-300 px-2 py-0.5 text-xs font-semibold hover:bg-red-100 dark:border-red-800 dark:hover:bg-red-900/40"
                  >
                    Refresh lineage
                  </button>
                </>
              )}
            </div>
          )}
          {activeGraph && (
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
                  @media (prefers-reduced-motion: reduce) {
                    .lineage-flow .react-flow__edge.animated path {
                      animation: none !important;
                    }
                  }
                `}
              </style>
              <div
                data-testid="lineage-stage-bar"
                data-view={viewMode}
                role="group"
                aria-label={viewMode === 'dependencies'
                  ? 'Graph legend: SDLC stages and dependency order'
                  : 'Graph legend: SDLC stages'}
                className="absolute left-4 top-4 z-10 flex flex-wrap gap-2 overflow-visible rounded-lg border border-gray-200 bg-white/95 px-2 py-1.5 shadow-sm dark:border-gray-800 dark:bg-gray-900/95"
                style={{ maxWidth: 'min(760px, calc(100% - 2rem))' }}
              >
                {stageLabels.map(({ stage, label }) => (
                  <span
                    key={stage}
                    className="whitespace-nowrap rounded bg-gray-100 px-2 py-1 text-[11px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                  >
                    {label}
                  </span>
                ))}
                {viewMode === 'dependencies' && (
                  <>
                    <span className="sr-only">
                      Horizontal position follows dependency order.
                    </span>
                    <span
                      aria-hidden="true"
                      className="mx-0.5 self-stretch border-l border-gray-300 dark:border-gray-700"
                    />
                    <span className="whitespace-nowrap rounded bg-amber-100 px-2 py-1 text-[11px] font-semibold text-amber-900 dark:bg-amber-950/50 dark:text-amber-200">
                      Spec / Task dependencies
                    </span>
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap px-1 py-1 text-[11px] font-semibold text-amber-700 dark:text-amber-300">
                      <span
                        aria-hidden="true"
                        className="inline-block w-7 border-t-2 border-dashed border-amber-700 dark:border-amber-500"
                      />
                      precedes
                    </span>
                  </>
                )}
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
                  {selectedResourceCounts && (
                    <div
                      className="mt-2 grid grid-cols-3 gap-1.5"
                      data-testid="lineage-resource-counts"
                    >
                      {[
                        ['Roots', selectedResourceCounts.unique_effective_count],
                        ['Physical', selectedResourceCounts.raw_attachment_count],
                        [
                          'Versions',
                          selectedResourceCounts.unique_root_version_count
                            ?? selectedResourceCounts.workspace_item_count,
                        ],
                      ].map(([label, value]) => (
                        <div key={String(label)} className="rounded bg-gray-100 px-1.5 py-1 text-center dark:bg-gray-800">
                          <div className="text-[9px] uppercase tracking-wide text-gray-400">{label}</div>
                          <div className="text-xs font-semibold text-gray-700 dark:text-gray-200">{value}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {canOpenDetails(selectedNode) && (
                    <button
                      type="button"
                      onClick={() => openNodeDetails(selectedNode)}
                      className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-cyan-600 px-2.5 py-1.5 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-cyan-500"
                    >
                      <ExternalLink size={13} />
                      Show details
                    </button>
                  )}
                </div>
              )}
              {viewMode === 'dependencies'
                && dependencyGraph
                && dependencyGraph.edges.length === 0
                && !dependencyLoading
                && (
                  <div
                    role="status"
                    aria-live="polite"
                    className="absolute bottom-5 left-1/2 z-10 -translate-x-1/2 rounded-lg border border-gray-200 bg-white/95 px-3 py-2 text-xs font-medium text-gray-600 shadow-sm dark:border-gray-800 dark:bg-gray-900/95 dark:text-gray-300"
                  >
                    No active Spec or Task dependencies in this lineage.
                  </div>
              )}
              {viewMode === 'dependencies' && dependencyGraph && (
                <ul className="sr-only" aria-label="Dependency relationships">
                  {accessibleDependencyRelationships.map((relationship) => (
                    <li key={relationship.id}>{relationship.description}</li>
                  ))}
                  {dependencyGraph.edges.length > ACCESSIBLE_RELATION_LIMIT && (
                    <li>
                      {dependencyGraph.edges.length - ACCESSIBLE_RELATION_LIMIT} additional
                      relationships are available visually.
                    </li>
                  )}
                </ul>
              )}
              <ReactFlow
                key={`${request.entityType}:${request.entityId}:${viewMode}:${activeGraphRevision}`}
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
