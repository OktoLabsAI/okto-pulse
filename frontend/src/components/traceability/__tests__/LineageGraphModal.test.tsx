import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LineageGraphModal } from '../LineageGraphModal';
import { openLineageGraph } from '../lineageGraphEvents';
import type { LineageGraphResponse } from '@/types';

const apiMock = vi.hoisted(() => ({
  getLineageGraph: vi.fn(),
}));

const pushMock = vi.hoisted(() => vi.fn());
const openCardModalMock = vi.hoisted(() => vi.fn());

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/contexts/ModalStackContext', () => ({
  useModalStack: () => ({ push: pushMock }),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: (selector: (state: { openCardModal: typeof openCardModalMock }) => unknown) => selector({
    openCardModal: openCardModalMock,
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
  },
}));

vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ children }: { children: ReactNode }) => <div data-testid="lineage-flow">{children}</div>,
  Background: () => null,
  Controls: () => null,
  Handle: () => null,
  MiniMap: () => null,
  MarkerType: { ArrowClosed: 'arrowclosed' },
  Position: { Left: 'left', Right: 'right' },
}));

const graph: LineageGraphResponse = {
  board_id: 'board-1',
  selected: { entity_type: 'ideation', entity_id: 'ideation-1' },
  root_ideation: { id: 'ideation-1', title: 'Root Ideation', status: 'done' },
  resolution_path: [{ type: 'ideation', id: 'ideation-1' }],
  nodes: [
    {
      id: 'node-ideation-1',
      entity_type: 'ideation',
      entity_id: 'ideation-1',
      title: 'Root Ideation',
      label: 'Root Ideation',
      status: 'done',
      stage: 0,
    },
  ],
  edges: [],
  summary: { ideations: 1 },
  warnings: [],
};

describe('LineageGraphModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getLineageGraph.mockResolvedValue(graph);
  });

  it('keeps the lineage graph open when Show details opens an entity modal', async () => {
    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('ideation', 'ideation-1');
    });

    await waitFor(() => expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByText('Show details'));

    expect(pushMock).toHaveBeenCalledWith({ type: 'ideation', id: 'ideation-1' });
    expect(screen.getByText('SDLC Lineage')).toBeInTheDocument();
    expect(screen.getAllByText('Root Ideation').length).toBeGreaterThan(0);
  });
});
