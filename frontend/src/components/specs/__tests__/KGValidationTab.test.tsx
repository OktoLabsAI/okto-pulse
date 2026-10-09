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
});
