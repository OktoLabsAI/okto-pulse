import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { MouseEvent, ReactNode } from 'react';
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
    onNodeClick,
    onPaneClick,
  }: {
    children: ReactNode;
    nodes?: Array<{
      id: string;
      position: { x: number; y: number };
      data?: { selected?: boolean };
    }>;
    edges?: Array<{
      id: string;
      source: string;
      target: string;
      label?: string;
      animated?: boolean;
      markerEnd?: { type?: string };
      style?: { opacity?: number; strokeDasharray?: string };
    }>;
    onNodeClick?: (
      event: MouseEvent,
      node: { id: string; position: { x: number; y: number } },
    ) => void;
    onPaneClick?: () => void;
  }) => (
    <div data-testid="lineage-flow">
      <button type="button" aria-label="Clear graph selection" onClick={onPaneClick} />
      {nodes.map((node) => (
        <button
          type="button"
          key={node.id}
          data-testid={`flow-node-${node.id}`}
          data-x={node.position.x}
          data-y={node.position.y}
          data-selected={String(Boolean(node.data?.selected))}
          aria-label={`Select node ${node.id}`}
          onClick={(event) => onNodeClick?.(event, node)}
        />
      ))}
      {edges.map((edge) => (
        <div
          key={edge.id}
          data-testid={`flow-edge-${edge.id}`}
          data-source={edge.source}
          data-target={edge.target}
          data-label={edge.label}
          data-animated={String(Boolean(edge.animated))}
          data-marker={edge.markerEnd?.type}
          data-opacity={edge.style?.opacity}
          data-stroke-dasharray={edge.style?.strokeDasharray}
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
      resource_counts: {
        unique_effective_count: 3,
        raw_attachment_count: 8,
        workspace_item_count: 5,
        unique_root_version_count: 4,
      },
    },
  ],
  edges: [],
  summary: { ideations: 1 },
  resource_counts: {
    unique_effective_count: 3,
    raw_attachment_count: 8,
    workspace_item_count: 5,
    unique_root_version_count: 4,
  },
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

const specLineageGraph: LineageGraphResponse = {
  board_id: 'board-1',
  view: 'lineage',
  selected: { entity_type: 'spec', entity_id: 'spec-a' },
  root_ideation: { id: 'ideation-1', title: 'Spec A lineage', status: 'done' },
  resolution_path: [{ type: 'spec', id: 'spec-a' }],
  nodes: [
    {
      id: 'origin:spec-a',
      entity_type: 'spec',
      entity_id: 'spec-a',
      title: 'Spec A',
      label: 'Spec A',
      status: 'in_progress',
      stage: 2,
    },
  ],
  edges: [],
  summary: { specs: 1, nodes: 1, edges: 0 },
  warnings: [],
};

const specDependencyGraph: LineageGraphResponse = {
  board_id: 'board-1',
  view: 'dependency',
  selected: { entity_type: 'spec', entity_id: 'spec-a' },
  root_ideation: { id: 'spec-a', title: 'Spec A', status: 'in_progress' },
  resolution_path: [{ type: 'spec', id: 'spec-a' }],
  nodes: [
    {
      id: 'spec:spec-c',
      entity_type: 'spec',
      entity_id: 'spec-c',
      title: 'Spec C',
      label: 'Spec C',
      status: 'done',
      stage: -2,
      dependency_role: 'prerequisite',
    },
    {
      id: 'spec:spec-b',
      entity_type: 'spec',
      entity_id: 'spec-b',
      title: 'Spec B',
      label: 'Spec B',
      status: 'done',
      stage: -1,
      dependency_role: 'prerequisite',
    },
    {
      id: 'spec:spec-a',
      entity_type: 'spec',
      entity_id: 'spec-a',
      title: 'Spec A',
      label: 'Spec A',
      status: 'in_progress',
      stage: 0,
      dependency_role: 'selected',
    },
    {
      id: 'spec:spec-d',
      entity_type: 'spec',
      entity_id: 'spec-d',
      title: 'Spec D',
      label: 'Spec D',
      status: 'draft',
      stage: 1,
      dependency_role: 'dependent',
    },
  ],
  edges: [
    {
      id: 'precedes:c-b',
      source: 'spec:spec-c',
      target: 'spec:spec-b',
      relationship: 'precedes',
    },
    {
      id: 'precedes:b-a',
      source: 'spec:spec-b',
      target: 'spec:spec-a',
      relationship: 'precedes',
    },
    {
      id: 'precedes:a-d',
      source: 'spec:spec-a',
      target: 'spec:spec-d',
      relationship: 'precedes',
    },
  ],
  summary: { specs: 4, nodes: 4, edges: 3 },
  warnings: [],
};

const taskLineageGraph: LineageGraphResponse = {
  ...bugGraph,
  selected: { entity_type: 'task', entity_id: 'task-a' },
  nodes: [
    {
      id: 'origin:task-a',
      entity_type: 'task',
      entity_id: 'task-a',
      title: 'Task A',
      label: 'Task A',
      status: 'started',
      stage: 4,
    },
  ],
  edges: [],
};

const taskDependencyGraph: LineageGraphResponse = {
  ...taskLineageGraph,
  view: 'dependency',
  root_ideation: { id: 'task-a', title: 'Task A', status: 'started' },
  nodes: [
    {
      id: 'test:test-prerequisite',
      entity_type: 'test',
      entity_id: 'test-prerequisite',
      title: 'Regression prerequisite',
      label: 'Regression prerequisite',
      status: 'done',
      stage: -1,
      dependency_role: 'prerequisite',
    },
    {
      id: 'task:task-a',
      entity_type: 'task',
      entity_id: 'task-a',
      title: 'Task A',
      label: 'Task A',
      status: 'started',
      stage: 0,
      dependency_role: 'selected',
    },
    {
      id: 'bug:bug-dependent',
      entity_type: 'bug',
      entity_id: 'bug-dependent',
      title: 'Dependent bug',
      label: 'Dependent bug',
      status: 'not_started',
      stage: 1,
      dependency_role: 'dependent',
    },
  ],
  edges: [
    {
      id: 'precedes:test-task',
      source: 'test:test-prerequisite',
      target: 'task:task-a',
      relationship: 'precedes',
    },
    {
      id: 'precedes:task-bug',
      source: 'task:task-a',
      target: 'bug:bug-dependent',
      relationship: 'precedes',
    },
  ],
  summary: { cards: 3, nodes: 3, edges: 2 },
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
    const counts = screen.getByTestId('lineage-resource-counts');
    expect(counts).toHaveTextContent('Roots3');
    expect(counts).toHaveTextContent('Physical8');
    expect(counts).toHaveTextContent('Versions4');
  });

  it('does not attribute requested-entity counts to another selected node', async () => {
    apiMock.getLineageGraph.mockResolvedValue({
      ...graph,
      nodes: [
        ...graph.nodes,
        {
          id: 'node-spec-2',
          entity_type: 'spec',
          entity_id: 'spec-2',
          title: 'Another spec',
          label: 'Another spec',
          status: 'approved',
          stage: 2,
        },
      ],
    });

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('ideation', 'ideation-1');
    });

    expect(await screen.findByTestId('lineage-resource-counts')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Select node node-spec-2' }));

    expect(screen.queryByTestId('lineage-resource-counts')).not.toBeInTheDocument();
    expect(screen.getByText('Another spec')).toBeInTheDocument();
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

  it('uses two-thirds horizontal spacing between lineage stages', async () => {
    apiMock.getLineageGraph.mockResolvedValue(storyGraph);

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    const storyNode = await screen.findByTestId('flow-node-node-story-1');
    const ideationNode = await screen.findByTestId('flow-node-node-ideation-1');

    expect(Number(ideationNode.dataset.x) - Number(storyNode.dataset.x)).toBeCloseTo((580 * 2) / 3);
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

  it('loads the governed Spec dependency closure lazily and restores lineage from cache', async () => {
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? specDependencyGraph : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('spec', 'spec-a');
    });

    const dependencyToggle = await screen.findByRole('button', { name: 'Dependencies' });
    const lineageToggle = screen.getByRole('button', { name: 'Origin / derivation' });
    expect(lineageToggle).toHaveAttribute('aria-pressed', 'true');
    expect(dependencyToggle).toHaveAttribute('aria-pressed', 'false');
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(1);
    expect(apiMock.getLineageGraph).toHaveBeenNthCalledWith(
      1,
      'board-1',
      'spec',
      'spec-a',
      false,
    );

    fireEvent.click(dependencyToggle);

    const selected = await screen.findByTestId('flow-node-spec:spec-a');
    const prerequisite = screen.getByTestId('flow-node-spec:spec-b');
    const transitivePrerequisite = screen.getByTestId('flow-node-spec:spec-c');
    const dependent = screen.getByTestId('flow-node-spec:spec-d');

    expect(apiMock.getLineageGraph).toHaveBeenNthCalledWith(
      2,
      'board-1',
      'spec',
      'spec-a',
      false,
      'dependency',
    );
    expect(dependencyToggle).toHaveAttribute('aria-pressed', 'true');
    expect(Number(transitivePrerequisite.dataset.x)).toBeLessThan(Number(prerequisite.dataset.x));
    expect(Number(prerequisite.dataset.x)).toBeLessThan(Number(selected.dataset.x));
    expect(Number(selected.dataset.x)).toBeLessThan(Number(dependent.dataset.x));

    for (const edge of specDependencyGraph.edges) {
      const rendered = screen.getByTestId(`flow-edge-${edge.id}`);
      const source = screen.getByTestId(`flow-node-${edge.source}`);
      const target = screen.getByTestId(`flow-node-${edge.target}`);
      expect(Number(source.dataset.x)).toBeLessThan(Number(target.dataset.x));
      expect(rendered).toHaveAttribute('data-label', 'precedes');
      expect(rendered).toHaveAttribute('data-marker', 'arrowclosed');
      expect(rendered).toHaveAttribute('data-stroke-dasharray', '8 5');
    }
    expect(screen.getByText('Spec C precedes Spec B')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-precedes:c-b')).toHaveAttribute(
      'data-opacity',
      '0.9',
    );
    expect(screen.getByTestId('lineage-stage-bar')).toHaveAttribute('data-view', 'dependencies');

    fireEvent.click(screen.getByRole('button', { name: 'Clear graph selection' }));
    for (const edge of specDependencyGraph.edges) {
      expect(screen.getByTestId(`flow-edge-${edge.id}`)).toHaveAttribute(
        'data-animated',
        'false',
      );
    }

    fireEvent.click(lineageToggle);

    expect(await screen.findByTestId('flow-node-origin:spec-a')).toBeInTheDocument();
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('lineage-stage-bar')).toHaveAttribute('data-view', 'lineage');
  });

  it('renders Task dependency flow with the real related card types', async () => {
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? taskDependencyGraph : taskLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('task', 'task-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    const testNode = await screen.findByTestId('flow-node-test:test-prerequisite');
    const taskNode = screen.getByTestId('flow-node-task:task-a');
    const bugNode = screen.getByTestId('flow-node-bug:bug-dependent');
    expect(Number(testNode.dataset.x)).toBeLessThan(Number(taskNode.dataset.x));
    expect(Number(taskNode.dataset.x)).toBeLessThan(Number(bugNode.dataset.x));
    expect(screen.getByText('Selected Task')).toBeInTheDocument();

    fireEvent.click(bugNode);
    fireEvent.click(screen.getByText('Show details'));
    expect(openCardModalMock).toHaveBeenCalledWith('bug-dependent');
    expect(pushMock).toHaveBeenCalledWith({ type: 'card', id: 'bug-dependent' });
  });

  it('disables edge animation for dense dependency graphs', async () => {
    const denseDependencyGraph: LineageGraphResponse = {
      ...specDependencyGraph,
      edges: Array.from({ length: 81 }, (_, index) => ({
        id: `precedes:dense-${index}`,
        source: 'spec:spec-b',
        target: 'spec:spec-a',
        relationship: 'precedes',
      })),
      summary: { specs: 4, nodes: 4, edges: 81 },
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? denseDependencyGraph : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByTestId('flow-edge-precedes:dense-0')).toHaveAttribute(
      'data-animated',
      'false',
    );
    expect(screen.getByTestId('flow-edge-precedes:dense-80')).toHaveAttribute(
      'data-animated',
      'false',
    );
  });

  it('shows an explicit empty dependency state while keeping the selected node', async () => {
    const isolatedDependencyGraph: LineageGraphResponse = {
      ...specDependencyGraph,
      nodes: [specDependencyGraph.nodes.find((node) => node.entity_id === 'spec-a')!],
      edges: [],
      summary: { specs: 1, nodes: 1, edges: 0 },
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? isolatedDependencyGraph : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByTestId('flow-node-spec:spec-a')).toBeInTheDocument();
    expect(screen.getByText('No active prerequisites or dependents.')).toBeInTheDocument();
  });

  it('does not offer dependency view for unsupported lineage entities', async () => {
    apiMock.getLineageGraph.mockResolvedValue(storyGraph);
    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    await screen.findByTestId('flow-node-node-story-1');
    expect(screen.queryByRole('button', { name: 'Dependencies' })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Graph view' })).not.toBeInTheDocument();
  });

  it('rejects an old-server lineage payload instead of relabeling it as dependencies', async () => {
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? specLineageGraph : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The server returned an incompatible dependency graph');
    expect(screen.queryByText('precedes')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('loads Task dependencies independently when origin lineage is unavailable', async () => {
    const dependencyGraphWithCounts: LineageGraphResponse = {
      ...taskDependencyGraph,
      nodes: taskDependencyGraph.nodes.map((node) => (
        node.entity_id === 'task-a'
          ? {
              ...node,
              resource_counts: {
                unique_effective_count: 2,
                raw_attachment_count: 3,
                workspace_item_count: 4,
              },
            }
          : node
      )),
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => view === 'dependency'
      ? Promise.resolve(dependencyGraphWithCounts)
      : Promise.reject(new Error('Origin lineage is unavailable')));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('task', 'task-a');
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('Origin lineage is unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByTestId('flow-node-task:task-a')).toBeInTheDocument();
    expect(screen.queryByText('Origin lineage is unavailable')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Dependencies graph for Task A' })).toBeInTheDocument();
    expect(screen.getByTestId('lineage-resource-counts')).toHaveTextContent('2');
  });

  it('keeps dependency selection when a slower lineage response arrives later', async () => {
    let resolveLineage!: (value: LineageGraphResponse) => void;
    const pendingLineage = new Promise<LineageGraphResponse>((resolve) => {
      resolveLineage = resolve;
    });
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => view === 'dependency'
      ? Promise.resolve(specDependencyGraph)
      : pendingLineage);

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByTestId('flow-node-spec:spec-a')).toHaveAttribute(
      'data-selected',
      'true',
    );

    await act(async () => {
      resolveLineage(specLineageGraph);
      await pendingLineage;
    });

    expect(screen.getByTestId('flow-node-spec:spec-a')).toHaveAttribute(
      'data-selected',
      'true',
    );
    expect(screen.getByText('Show details')).toBeInTheDocument();
  });

  it('exposes modal, toggle, region, and live-state semantics', async () => {
    apiMock.getLineageGraph.mockResolvedValue(specLineageGraph);
    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('spec', 'spec-a');
    });

    const dialog = await screen.findByRole('dialog', { name: 'SDLC Lineage' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('group', { name: 'Graph view' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Origin and derivation graph for Spec A lineage' })).toBeInTheDocument();
  });
});
