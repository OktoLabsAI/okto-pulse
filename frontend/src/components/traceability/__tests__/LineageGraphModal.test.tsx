import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  ReactFlow: ({
    children,
    nodes = [],
    edges = [],
  }: {
    children: ReactNode;
    nodes?: Array<{ id: string; position: { x: number; y: number } }>;
    edges?: Array<{ id: string; source: string; target: string; label?: string }>;
  }) => (
    <div data-testid="lineage-flow">
      {nodes.map((node) => (
        <div
          key={node.id}
          data-testid={`flow-node-${node.id}`}
          data-x={node.position.x}
          data-y={node.position.y}
        />
      ))}
      {edges.map((edge) => (
        <div
          key={edge.id}
          data-testid={`flow-edge-${edge.id}`}
          data-source={edge.source}
          data-target={edge.target}
          data-label={edge.label}
        />
      ))}
      {children}
    </div>
  ),
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

const storyGraph: LineageGraphResponse = {
  board_id: 'board-1',
  selected: { entity_type: 'story', entity_id: 'story-1' },
  root_ideation: { id: 'ideation-1', title: 'Root Ideation', status: 'done' },
  resolution_path: [
    { type: 'story', id: 'story-1' },
    { type: 'ideation', id: 'ideation-1' },
  ],
  nodes: [
    {
      id: 'node-story-1',
      entity_type: 'story',
      entity_id: 'story-1',
      title: 'User can request audit',
      label: 'User can request audit',
      status: 'converted',
      stage: -1,
    },
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
  edges: [
    {
      id: 'edge-story-ideation',
      source: 'node-story-1',
      target: 'node-ideation-1',
      relationship: 'feeds_ideation',
    },
  ],
  summary: { stories: 1, ideations: 1 },
  warnings: [],
};

const bugGraph: LineageGraphResponse = {
  board_id: 'board-1',
  selected: { entity_type: 'bug', entity_id: 'bug-1' },
  root_ideation: { id: 'ideation-1', title: 'Root Ideation', status: 'done' },
  resolution_path: [
    { type: 'bug', id: 'bug-1' },
    { type: 'ideation', id: 'ideation-1' },
  ],
  nodes: [
    {
      id: 'task:task-1',
      entity_type: 'task',
      entity_id: 'task-1',
      title: 'Implement feature',
      label: 'Implement feature',
      status: 'done',
      stage: 4,
    },
    {
      id: 'test:test-1',
      entity_type: 'test',
      entity_id: 'test-1',
      title: 'Regression test',
      label: 'Regression test',
      status: 'done',
      stage: 4,
    },
    {
      id: 'bug:bug-1',
      entity_type: 'bug',
      entity_id: 'bug-1',
      title: 'Fix bug',
      label: 'Fix bug',
      status: 'validation',
      stage: 5,
    },
  ],
  edges: [
    {
      id: 'task:task-1->originates_bug->bug:bug-1',
      source: 'task:task-1',
      target: 'bug:bug-1',
      relationship: 'originates_bug',
    },
    {
      id: 'test:test-1->regression_test->bug:bug-1',
      source: 'test:test-1',
      target: 'bug:bug-1',
      relationship: 'regression_test',
    },
  ],
  summary: { tasks: 1, tests: 1, bugs: 1 },
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

  it('shows details for a selected Story node', async () => {
    apiMock.getLineageGraph.mockResolvedValue(storyGraph);

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    await waitFor(() => expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByText('Show details'));

    expect(pushMock).toHaveBeenCalledWith({ type: 'story', id: 'story-1' });
    expect(screen.getByText('SDLC Lineage')).toBeInTheDocument();
  });

  it('orders the stage bar with Stories before Ideation without horizontal overflow', async () => {
    apiMock.getLineageGraph.mockResolvedValue(storyGraph);

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    const stageBar = await screen.findByTestId('lineage-stage-bar');
    expect(stageBar).toHaveClass('flex-wrap');
    expect(stageBar).not.toHaveClass('overflow-x-auto');
    expect(
      within(stageBar).getAllByText(
        /^(Stories|Ideation|Refinement|Spec|Sprint|Tasks \/ Tests|Bugs)$/,
      ).map((item) => item.textContent),
    ).toEqual([
      'Stories',
      'Ideation',
      'Refinement',
      'Spec',
      'Sprint',
      'Tasks / Tests',
      'Bugs',
    ]);
  });

  it('doubles horizontal spacing between lineage stages', async () => {
    apiMock.getLineageGraph.mockResolvedValue(storyGraph);

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    const storyNode = await screen.findByTestId('flow-node-node-story-1');
    const ideationNode = await screen.findByTestId('flow-node-node-ideation-1');

    expect(Number(ideationNode.dataset.x) - Number(storyNode.dataset.x)).toBe(580);
  });

  it('renders bug regression test links in the lineage graph', async () => {
    apiMock.getLineageGraph.mockResolvedValue(bugGraph);

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('bug', 'bug-1');
    });

    const regressionEdge = await screen.findByTestId('flow-edge-test:test-1->regression_test->bug:bug-1');

    expect(regressionEdge).toHaveAttribute('data-source', 'test:test-1');
    expect(regressionEdge).toHaveAttribute('data-target', 'bug:bug-1');
    expect(regressionEdge).toHaveAttribute('data-label', 'test');
  });
});
