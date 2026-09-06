import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as kgApi from '@/services/kg-api';
import * as kgHealthApi from '@/services/kg-health-api';
import { GraphVisibilityMismatchState, KnowledgeGraphPage } from '../KnowledgeGraphPage';
import type { GraphMetadata } from '@/services/kg-api';
import type { KGHealth } from '@/services/kg-health-api';
import type { KGEdge, KGNode } from '@/types/knowledge-graph';

const permissionHas = vi.hoisted(() => vi.fn((_flag: string) => true));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ preset: 'Full Control', isLoading: false, error: null, ownerReviewRequired: false, has: permissionHas }),
}));

vi.mock('../GraphCanvas', () => ({
  GraphCanvas: ({ nodes, edges }: { nodes: KGNode[]; edges: KGEdge[] }) => (
    <div data-testid="mock-graph-canvas">
      canvas nodes: {nodes.length}; edges: {edges.length}; titles: {nodes.map((node) => node.title).join('|')}
    </div>
  ),
}));

vi.mock('../GraphControlsPanel', () => ({
  GraphControlsPanel: ({
    subView,
    nodeCount,
    visibleNodeCount,
    nodeTypeCounts,
  }: {
    subView: string;
    nodeCount: number;
    visibleNodeCount: number;
    nodeTypeCounts?: Record<string, number>;
  }) => (
    <div data-testid="mock-graph-controls">
      controls: {subView}; loaded: {nodeCount}; visible: {visibleNodeCount}; counts: {nodeTypeCounts?.Decision ?? 'pending'}
    </div>
  ),
}));

vi.mock('../KGSyncIndicator', () => ({
  KGSyncIndicator: () => <div data-testid="mock-sync-indicator" />,
}));

vi.mock('../KGRefreshButton', () => ({
  KGRefreshButton: () => <button type="button">Refresh</button>,
}));

vi.mock('../NodeDetailPanel', () => ({
  NodeDetailPanel: () => <div data-testid="mock-node-detail" />,
}));

vi.mock('../NodeDetailModal', () => ({
  NodeDetailModal: () => <div data-testid="mock-node-modal" />,
}));

vi.mock('../AuditLogView', () => ({
  AuditLogView: () => <div data-testid="mock-audit" />,
}));

vi.mock('../PendingQueueView', () => ({
  PendingQueueView: () => <div data-testid="mock-pending" />,
}));

vi.mock('../PendingQueueTree', () => ({
  PendingQueueTree: () => <div data-testid="mock-pending-tree" />,
}));

vi.mock('../SettingsView', () => ({
  SettingsView: () => <div data-testid="mock-settings" />,
}));

vi.mock('../GlobalSearchView', () => ({
  GlobalSearchView: () => <div data-testid="mock-global" />,
}));

vi.mock('@/hooks/useKgLiveEvents', () => ({
  useKgLiveEvents: () => ({
    connectionState: 'closed',
    unseenCommits: 0,
    lastEvent: null,
    markSeen: () => {},
  }),
}));

const health: KGHealth = {
  materialization_state: 'materialized',
  materialization_generation: 'generation-1',
  probe_reason_codes: {
    board_graph: 'board_graph_present',
    board_census: 'board_census_available',
    global_discovery: 'global_discovery_present',
  },
  queue_depth: 0,
  oldest_pending_age_s: 0,
  dead_letter_count: 0,
  global_outbox_dead_letter_count: 0,
  total_nodes: 140,
  default_score_count: 0,
  default_score_ratio: 0,
  avg_relevance: 0.0057,
  top_disconnected_nodes: [],
  schema_version: '1.0',
  health_schema_version: '1.1',
  graph_schema_version: '0.3.3',
  contradict_warn_count: 0,
  last_decay_tick_at: null,
  last_tick_status: 'failed',
  last_tick_error: 'tick handler failed',
  nodes_recomputed_in_last_tick: 0,
  tick_in_progress: false,
};

const metadata: GraphMetadata = {
  depth: 2,
  truncated: false,
  min_relevance: 0,
  edge_read_status: 'partial_failure',
  edge_tables_scanned: 5,
  edge_tables_failed: 1,
  edge_errors: [{ relationship: 'belongs_to', error: 'read failed' }],
  edges_returned: 0,
};

const completedHistorical: kgApi.HistoricalProgress = {
  enabled: true,
  status: 'completed',
  total: 1,
  progress: 1,
  pending: 0,
  claimed: 0,
  paused: 0,
  failed: 0,
};

beforeEach(() => {
  vi.restoreAllMocks();
  permissionHas.mockImplementation(() => true);
});

afterEach(() => cleanup());

describe('GraphVisibilityMismatchState', () => {
  it('renders source-aware diagnostics when health has nodes but graph is empty', () => {
    const onRefresh = vi.fn();

    render(
      <GraphVisibilityMismatchState
        boardId="board-123"
        health={health}
        metadata={metadata}
        onRefresh={onRefresh}
      />,
    );

    expect(screen.getByText('KG data exists, graph view is empty')).toBeInTheDocument();
    expect(screen.getByText(/Health reports 140 node\(s\)/)).toBeInTheDocument();
    expect(screen.getByText('Graph schema 0.3.3')).toBeInTheDocument();
    expect(screen.getByText('Health schema 1.1')).toBeInTheDocument();
    expect(screen.getByText('Last tick: failed')).toBeInTheDocument();
    expect(screen.getByText('Status partial_failure')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('kg-empty-mismatch-refresh'));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe('KnowledgeGraphPage — historical completion release', () => {
  it('renders the graph without waiting for the slower diagnostics', async () => {
    const stats = {
      schema_version: '1.0',
      node_counts_by_type: { Decision: 1 },
      edge_counts_by_type: {},
      avg_confidence: 0.9,
      pending_queue_count: 0,
    };
    let releaseStats!: () => void;
    vi.spyOn(kgApi, 'getSubgraph').mockResolvedValue({
      nodes: [{
        id: 'decision-1',
        title: 'Ready graph',
        content: '',
        source_confidence: 0.9,
        relevance_score: 0.8,
        node_type: 'Decision',
      }],
      edges: [],
      metadata: { edge_read_status: 'ok' },
      next_cursor: null,
    });
    vi.spyOn(kgApi, 'getStats').mockImplementation(() => new Promise((resolve) => {
      releaseStats = () => resolve(stats);
    }));
    vi.spyOn(kgApi, 'getHistoricalProgress').mockResolvedValue(completedHistorical);
    vi.spyOn(kgHealthApi, 'getKGHealth').mockResolvedValue({ ...health, total_nodes: 1 });

    render(<KnowledgeGraphPage boardId="board-123" />);

    expect(await screen.findByTestId('mock-graph-canvas')).toHaveTextContent('Ready graph');
    expect(screen.queryByTestId('kg-loading')).not.toBeInTheDocument();
    releaseStats();
    await waitFor(() => {
      expect(screen.getByTestId('mock-graph-controls')).toHaveTextContent('counts: 1');
    });
  });

  it('does not refetch the graph when diagnostic permissions become available', async () => {
    let healthAllowed = false;
    permissionHas.mockImplementation((flag: string) => (
      flag === 'kg.operations.health.read' ? healthAllowed : true
    ));
    const graph = vi.spyOn(kgApi, 'getSubgraph').mockResolvedValue({
      nodes: [],
      edges: [],
      metadata: { edge_read_status: 'ok' },
      next_cursor: null,
    });
    vi.spyOn(kgApi, 'getStats').mockResolvedValue({
      schema_version: '1.0',
      node_counts_by_type: {},
      edge_counts_by_type: {},
      avg_confidence: 0,
      pending_queue_count: 0,
    });
    vi.spyOn(kgApi, 'getHistoricalProgress').mockResolvedValue(completedHistorical);
    const healthRead = vi.spyOn(kgHealthApi, 'getKGHealth').mockResolvedValue(health);

    const { rerender } = render(<KnowledgeGraphPage boardId="board-123" />);
    await waitFor(() => expect(graph).toHaveBeenCalledTimes(1));

    healthAllowed = true;
    rerender(<KnowledgeGraphPage boardId="board-123" />);

    await waitFor(() => expect(healthRead).toHaveBeenCalledTimes(1));
    expect(graph).toHaveBeenCalledTimes(1);
  });

  it('renders the KG shell instead of the historical onboarding once backfill is terminal', async () => {
    vi.spyOn(kgApi, 'getSubgraph').mockResolvedValue({
      nodes: [],
      edges: [],
      metadata: { edge_read_status: 'ok' },
      next_cursor: null,
    });
    vi.spyOn(kgApi, 'getHistoricalProgress').mockResolvedValue({
      enabled: true,
      status: 'completed',
      total: 42,
      progress: 42,
      pending: 0,
      claimed: 0,
      paused: 0,
      failed: 0,
    });
    vi.spyOn(kgHealthApi, 'getKGHealth').mockResolvedValue({
      ...health,
      total_nodes: 0,
    });

    render(<KnowledgeGraphPage boardId="board-123" />);

    expect(await screen.findByTestId('mock-graph-canvas')).toHaveTextContent('canvas nodes: 0');
    expect(screen.getByTestId('mock-graph-controls')).toHaveTextContent('controls: graph');
    expect(screen.queryByTestId('kg-empty-yet')).not.toBeInTheDocument();
  });

  it('removes Code Traceability nodes, incident edges and counts after permission loss', async () => {
    const physicalNode: KGNode = {
      id: 'physical-1',
      title: 'Visible decision',
      content: 'Visible content',
      source_confidence: 0.9,
      relevance_score: 0.8,
      node_type: 'Decision',
    };
    const traceabilityNode: KGNode = {
      id: 'traceability-1',
      title: 'Secret implementation target',
      content: 'Agent-submitted target content',
      source_confidence: 0.9,
      relevance_score: 0.8,
      node_type: 'Entity',
      kind_of: 'implementation_target',
    };
    const semanticGuidelineNode: KGNode = {
      id: 'semantic-guideline-1',
      title: 'Visible semantic guideline',
      content: 'Governed guideline content',
      source_confidence: 0.9,
      relevance_score: 0.8,
      node_type: 'Entity',
      kind_of: 'SemanticGuidelineConstraint',
    };
    vi.spyOn(kgApi, 'getSubgraph').mockResolvedValue({
      nodes: [physicalNode, traceabilityNode, semanticGuidelineNode],
      edges: [
        {
          id: 'supports-secret',
          source: 'physical-1',
          target: 'traceability-1',
          edge_type: 'supports',
          confidence: 0.9,
        },
        {
          id: 'supports-guideline',
          source: 'physical-1',
          target: 'semantic-guideline-1',
          edge_type: 'supports',
          confidence: 0.9,
        },
      ],
      metadata: { edge_read_status: 'ok' },
      next_cursor: null,
    });
    vi.spyOn(kgApi, 'getHistoricalProgress').mockResolvedValue({
      enabled: true,
      status: 'completed',
      total: 2,
      progress: 2,
      pending: 0,
      claimed: 0,
      paused: 0,
      failed: 0,
    });
    vi.spyOn(kgHealthApi, 'getKGHealth').mockResolvedValue({ ...health, total_nodes: 2 });
    vi.spyOn(kgApi, 'getStats').mockResolvedValue({
      schema_version: '1.0',
      node_counts_by_type: { Decision: 1, Entity: 1 },
      edge_counts_by_type: { supports: 1 },
      avg_confidence: 0.9,
      pending_queue_count: 0,
    });

    const { rerender } = render(<KnowledgeGraphPage boardId="board-123" />);
    expect(await screen.findByTestId('mock-graph-canvas')).toHaveTextContent('canvas nodes: 3; edges: 2');
    expect(screen.getByTestId('mock-graph-canvas')).toHaveTextContent('Secret implementation target');

    permissionHas.mockImplementation(
      (flag: string) => !flag.startsWith('code_traceability.'),
    );
    rerender(<KnowledgeGraphPage boardId="board-123" />);

    await waitFor(() => {
      expect(screen.getByTestId('mock-graph-canvas')).toHaveTextContent('canvas nodes: 2; edges: 1');
    });
    expect(screen.getByTestId('mock-graph-canvas')).not.toHaveTextContent('Secret implementation target');
    expect(screen.getByTestId('mock-graph-canvas')).toHaveTextContent('Visible semantic guideline');
    expect(screen.getByTestId('mock-graph-controls')).toHaveTextContent('loaded: 2; visible: 2');
  });
});
