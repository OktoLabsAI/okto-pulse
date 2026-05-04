import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import * as kgApi from '@/services/kg-api';
import * as kgHealthApi from '@/services/kg-health-api';
import { GraphVisibilityMismatchState, KnowledgeGraphPage } from '../KnowledgeGraphPage';
import type { GraphMetadata } from '@/services/kg-api';
import type { KGHealth } from '@/services/kg-health-api';

vi.mock('../GraphCanvas', () => ({
  GraphCanvas: ({ nodes }: { nodes: unknown[] }) => (
    <div data-testid="mock-graph-canvas">canvas nodes: {nodes.length}</div>
  ),
}));

vi.mock('../GraphControlsPanel', () => ({
  GraphControlsPanel: ({ subView }: { subView: string }) => (
    <div data-testid="mock-graph-controls">controls: {subView}</div>
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
  queue_depth: 0,
  oldest_pending_age_s: 0,
  dead_letter_count: 0,
  total_nodes: 140,
  default_score_count: 0,
  default_score_ratio: 0,
  avg_relevance: 0.0057,
  top_disconnected_nodes: [],
  schema_version: '1.0',
  health_schema_version: '1.0',
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

beforeEach(() => {
  vi.restoreAllMocks();
});

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
    expect(screen.getByText('Health schema 1.0')).toBeInTheDocument();
    expect(screen.getByText('Last tick: failed')).toBeInTheDocument();
    expect(screen.getByText('Status partial_failure')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('kg-empty-mismatch-refresh'));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe('KnowledgeGraphPage — historical completion release', () => {
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
});
