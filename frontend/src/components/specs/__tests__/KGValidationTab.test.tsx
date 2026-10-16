import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KGValidationTab } from '../KGValidationTab';

const api = vi.hoisted(() => ({ getSubgraph: vi.fn() }));
vi.mock('@/services/kg-api', () => api);
vi.mock('@/components/knowledge/NodeDetailModal', () => ({ NodeDetailModal: () => null }));

describe('KG Graph loading', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the shared animated pulse while loading and removes it after completion', async () => {
    let finish!: (value: unknown) => void;
    api.getSubgraph.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    render(<KGValidationTab boardId="board" specId="spec" />);
    const loading = screen.getByTestId('kg-validation-loading');
    expect(loading).toHaveTextContent('Loading Knowledge Graph…');
    expect(loading.querySelector('svg .pulse-loader__trace')).not.toBeNull();
    await act(async () => { finish({ nodes: [], edges: [], next_cursor: null }); });
    expect(screen.queryByTestId('kg-validation-loading')).not.toBeInTheDocument();
    expect(screen.getByTestId('kg-validation-empty')).toBeInTheDocument();
  });

  it('replaces the loader with the existing error state on failure', async () => {
    api.getSubgraph.mockRejectedValue(new Error('Graph unavailable'));
    render(<KGValidationTab boardId="board" specId="spec" />);
    expect(await screen.findByTestId('kg-validation-error')).toHaveTextContent('Graph unavailable');
    expect(screen.queryByTestId('kg-validation-loading')).not.toBeInTheDocument();
  });

  it('includes working derived references from later pages, excluding other Specs and superseded history', async () => {
    const node = (id: string, ref: string, extra = {}) => ({ id, title: id, source_artifact_ref: ref, node_type: 'Requirement', source_confidence: 90, relevance_score: 0.8, ...extra });
    api.getSubgraph.mockResolvedValueOnce({ nodes: [node('other', 'spec:spec-other:fr:a')], edges: [], next_cursor: 'page2' });
    api.getSubgraph.mockResolvedValueOnce({ nodes: [node('current', 'spec:spec:fr:a', { graph_layer: 'working' }), node('old', 'spec:spec:fr:a', { superseded_by: 'current' })], edges: [], next_cursor: null });
    render(<KGValidationTab boardId="board" specId="spec" />);
    expect(await screen.findByTestId('kg-validation-tab')).toHaveTextContent('current');
    expect(screen.queryByText('other')).not.toBeInTheDocument();
    expect(screen.queryByText('old')).not.toBeInTheDocument();
    expect(api.getSubgraph).toHaveBeenNthCalledWith(1, 'board', { limit: 500, graph_layer: 'all' });
    expect(api.getSubgraph).toHaveBeenNthCalledWith(2, 'board', { limit: 500, graph_layer: 'all', cursor: 'page2' });
  });

  it('does not claim an empty graph when a later page fails', async () => {
    api.getSubgraph.mockResolvedValueOnce({ nodes: [], edges: [], next_cursor: 'page2' }).mockRejectedValueOnce(new Error('Page unavailable'));
    render(<KGValidationTab boardId="board" specId="spec" />);
    expect(await screen.findByTestId('kg-validation-error')).toHaveTextContent('Page unavailable');
    expect(screen.queryByTestId('kg-validation-empty')).not.toBeInTheDocument();
  });

  it('reports repeated cursors instead of presenting complete data', async () => {
    api.getSubgraph.mockResolvedValue({ nodes: [], edges: [], next_cursor: 'same' });
    render(<KGValidationTab boardId="board" specId="spec" />);
    expect(await screen.findByTestId('kg-validation-error')).toHaveTextContent('pagination did not advance');
  });

  it('reports partial edge failures instead of claiming an empty graph', async () => {
    api.getSubgraph.mockResolvedValue({ nodes: [], edges: [], next_cursor: null, metadata: { edge_read_status: 'partial_failure' } });
    render(<KGValidationTab boardId="board" specId="spec" />);
    expect(await screen.findByTestId('kg-validation-error')).toHaveTextContent('relations could not be fully loaded');
  });
});
