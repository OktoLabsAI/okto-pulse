import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { MouseEvent, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LineageGraphModal } from '../LineageGraphModal';
import { openLineageGraph } from '../lineageGraphEvents';
import type { LineageGraphResponse } from '@/types';

type DependencyOverlay = LineageGraphResponse & {
  dependency_scope: 'lineage';
  lineage_node_ids: string[];
  lineage_entities: Array<{
    entity_type: 'spec' | 'card';
    entity_id: string;
  }>;
};

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
      id: 'origin:ideation-1',
      entity_type: 'ideation',
      entity_id: 'ideation-1',
      title: 'Spec A lineage',
      label: 'Spec A lineage',
      status: 'done',
      stage: 0,
    },
    {
      id: 'origin:spec-a',
      entity_type: 'spec',
      entity_id: 'spec-a',
      title: 'Spec A',
      label: 'Spec A',
      status: 'in_progress',
      stage: 2,
    },
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
  edges: [
    {
      id: 'lineage:ideation-spec',
      source: 'origin:ideation-1',
      target: 'origin:spec-a',
      relationship: 'direct_spec',
    },
    {
      id: 'lineage:spec-task',
      source: 'origin:spec-a',
      target: 'origin:task-a',
      relationship: 'contains_card',
    },
  ],
  summary: { ideations: 1, specs: 1, tasks: 1, nodes: 3, edges: 2 },
  warnings: [],
};

const specDependencyGraph: DependencyOverlay = {
  board_id: 'board-1',
  view: 'dependency',
  dependency_scope: 'lineage',
  lineage_node_ids: ['spec:spec-a', 'task:task-a'],
  lineage_entities: [
    { entity_type: 'card', entity_id: 'task-a' },
    { entity_type: 'spec', entity_id: 'spec-a' },
  ],
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
    {
      id: 'test:test-prerequisite',
      entity_type: 'test',
      entity_id: 'test-prerequisite',
      title: 'Regression prerequisite',
      label: 'Regression prerequisite',
      status: 'done',
      stage: 3,
    },
    {
      id: 'task:task-a',
      entity_type: 'task',
      entity_id: 'task-a',
      title: 'Task A overlay title',
      label: 'Task A overlay title',
      status: 'blocked',
      stage: 4,
    },
    {
      id: 'bug:bug-dependent',
      entity_type: 'bug',
      entity_id: 'bug-dependent',
      title: 'Dependent bug',
      label: 'Dependent bug',
      status: 'not_started',
      stage: 5,
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
  summary: { specs: 4, cards: 3, nodes: 7, edges: 5 },
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

const taskDependencyGraph: DependencyOverlay = {
  ...taskLineageGraph,
  view: 'dependency',
  dependency_scope: 'lineage',
  lineage_node_ids: ['task:task-a'],
  lineage_entities: [{ entity_type: 'card', entity_id: 'task-a' }],
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

  it('overlays all lineage dependencies without replacing origin nodes or edges', async () => {
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
    expect(await screen.findByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:spec-a')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:task-a')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toHaveAttribute(
      'data-label',
      'spec',
    );
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(1);

    fireEvent.click(dependencyToggle);

    const selected = await screen.findByTestId('flow-node-origin:spec-a');
    const prerequisite = screen.getByTestId('flow-node-spec:spec-b');
    const transitivePrerequisite = screen.getByTestId('flow-node-spec:spec-c');
    const dependent = screen.getByTestId('flow-node-spec:spec-d');
    const taskSeed = screen.getByTestId('flow-node-origin:task-a');
    const taskPrerequisite = screen.getByTestId('flow-node-test:test-prerequisite');
    const taskDependent = screen.getByTestId('flow-node-bug:bug-dependent');

    expect(apiMock.getLineageGraph).toHaveBeenNthCalledWith(
      2,
      'board-1',
      'spec',
      'spec-a',
      false,
      'dependency',
      'lineage',
    );
    expect(screen.queryByTestId('flow-node-spec:spec-a')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flow-node-task:task-a')).not.toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toHaveAttribute(
      'data-label',
      'spec',
    );
    expect(screen.getByTestId('flow-edge-lineage:spec-task')).toHaveAttribute(
      'data-label',
      'card',
    );
    expect(Number(transitivePrerequisite.dataset.x)).toBeLessThan(Number(prerequisite.dataset.x));
    expect(Number(prerequisite.dataset.x)).toBeLessThan(Number(selected.dataset.x));
    expect(Number(selected.dataset.x)).toBeLessThan(Number(dependent.dataset.x));
    expect(Number(taskPrerequisite.dataset.x)).toBeLessThan(Number(taskSeed.dataset.x));
    expect(Number(taskSeed.dataset.x)).toBeLessThan(Number(taskDependent.dataset.x));

    const mergedEndpoints = new Map([
      ['spec:spec-a', 'origin:spec-a'],
      ['task:task-a', 'origin:task-a'],
    ]);
    for (const edge of specDependencyGraph.edges) {
      const rendered = screen.getByTestId(`flow-edge-${edge.id}`);
      const sourceId = mergedEndpoints.get(edge.source) || edge.source;
      const targetId = mergedEndpoints.get(edge.target) || edge.target;
      const source = screen.getByTestId(`flow-node-${sourceId}`);
      const target = screen.getByTestId(`flow-node-${targetId}`);
      expect(Number(source.dataset.x)).toBeLessThan(Number(target.dataset.x));
      expect(rendered).toHaveAttribute('data-source', sourceId);
      expect(rendered).toHaveAttribute('data-target', targetId);
      expect(rendered).toHaveAttribute('data-label', 'precedes');
      expect(rendered).toHaveAttribute('data-marker', 'arrowclosed');
      expect(rendered).toHaveAttribute('data-stroke-dasharray', '8 5');
    }
    expect(screen.getByText('Spec C precedes Spec B')).toBeInTheDocument();
    expect(screen.getByText('Regression prerequisite precedes Task A')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-precedes:c-b')).toHaveAttribute(
      'data-opacity',
      '0.9',
    );
    const stageBar = screen.getByTestId('lineage-stage-bar');
    expect(stageBar).toHaveAttribute('data-view', 'dependencies');
    expect(within(stageBar).getByText('Spec / Task dependencies')).toBeInTheDocument();
    expect(within(stageBar).getByText('Ideation')).toBeInTheDocument();
    expect(within(stageBar).getByText('Bugs')).toBeInTheDocument();

    fireEvent.click(taskSeed);
    expect(screen.getByText('Task A')).toBeInTheDocument();
    expect(screen.queryByText('Task A overlay title')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear graph selection' }));
    for (const edge of specDependencyGraph.edges) {
      expect(screen.getByTestId(`flow-edge-${edge.id}`)).toHaveAttribute(
        'data-animated',
        'false',
      );
    }

    fireEvent.click(lineageToggle);
    expect(await screen.findByTestId('flow-node-origin:spec-a')).toBeInTheDocument();
    expect(screen.queryByTestId('flow-node-spec:spec-b')).not.toBeInTheDocument();
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('lineage-stage-bar')).toHaveAttribute('data-view', 'lineage');
  });

  it('deduplicates card variants and renders their real related types', async () => {
    const normalizedSelectedOverlay: DependencyOverlay = {
      ...taskDependencyGraph,
      selected: { entity_type: 'card', entity_id: 'task-a' },
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? normalizedSelectedOverlay : taskLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('task', 'task-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    const testNode = await screen.findByTestId('flow-node-test:test-prerequisite');
    const taskNode = screen.getByTestId('flow-node-origin:task-a');
    const bugNode = screen.getByTestId('flow-node-bug:bug-dependent');
    expect(screen.queryByTestId('flow-node-task:task-a')).not.toBeInTheDocument();
    expect(Number(testNode.dataset.x)).toBeLessThan(Number(taskNode.dataset.x));
    expect(Number(taskNode.dataset.x)).toBeLessThan(Number(bugNode.dataset.x));

    fireEvent.click(bugNode);
    fireEvent.click(screen.getByText('Show details'));
    expect(openCardModalMock).toHaveBeenCalledWith('bug-dependent');
    expect(pushMock).toHaveBeenCalledWith({ type: 'card', id: 'bug-dependent' });
  });

  it('preserves lineage nodes and edges when dependency IDs collide', async () => {
    const collidingOverlay: DependencyOverlay = {
      ...specDependencyGraph,
      nodes: specDependencyGraph.nodes.map((node) => (
        node.id === 'spec:spec-b'
          ? { ...node, id: 'origin:ideation-1' }
          : node
      )),
      edges: specDependencyGraph.edges.map((edge, index) => ({
        ...edge,
        id: index === 0 ? 'lineage:ideation-spec' : edge.id,
        source: edge.source === 'spec:spec-b' ? 'origin:ideation-1' : edge.source,
        target: edge.target === 'spec:spec-b' ? 'origin:ideation-1' : edge.target,
      })),
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? collidingOverlay : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    const dependencyNode = await screen.findByTestId(
      'flow-node-dependency-overlay:1:origin:ideation-1',
    );
    const lineageNode = screen.getByTestId('flow-node-origin:ideation-1');
    expect(lineageNode).toBeInTheDocument();
    expect(dependencyNode).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toHaveAttribute(
      'data-label',
      'spec',
    );
    expect(screen.getByTestId(
      'flow-edge-dependency-overlay:1:lineage:ideation-spec',
    )).toHaveAttribute('data-label', 'precedes');
  });

  it('keeps integer dependency columns from overlapping adjacent SDLC nodes', async () => {
    const sameStageLineage: LineageGraphResponse = {
      board_id: 'board-1',
      view: 'lineage',
      selected: { entity_type: 'spec', entity_id: 'spec-a' },
      root_ideation: { id: 'ideation-1', title: 'Integer column lineage' },
      resolution_path: [{ type: 'spec', id: 'spec-a' }],
      nodes: [
        {
          id: 'origin:ideation-1',
          entity_type: 'ideation',
          entity_id: 'ideation-1',
          title: 'Ideation',
          label: 'Ideation',
          stage: 0,
        },
        {
          id: 'origin:refinement-1',
          entity_type: 'refinement',
          entity_id: 'refinement-1',
          title: 'Refinement',
          label: 'Refinement',
          stage: 1,
        },
        {
          id: 'origin:spec-a',
          entity_type: 'spec',
          entity_id: 'spec-a',
          title: 'Spec A',
          label: 'Spec A',
          stage: 2,
        },
        {
          id: 'origin:spec-b',
          entity_type: 'spec',
          entity_id: 'spec-b',
          title: 'Spec B',
          label: 'Spec B',
          stage: 2,
        },
      ],
      edges: [
        {
          id: 'lineage:ideation-refinement',
          source: 'origin:ideation-1',
          target: 'origin:refinement-1',
          relationship: 'has_refinement',
        },
        {
          id: 'lineage:refinement-spec-a',
          source: 'origin:refinement-1',
          target: 'origin:spec-a',
          relationship: 'derived_spec',
        },
        {
          id: 'lineage:refinement-spec-b',
          source: 'origin:refinement-1',
          target: 'origin:spec-b',
          relationship: 'derived_spec',
        },
      ],
      summary: { ideations: 1, refinements: 1, specs: 2, nodes: 4, edges: 3 },
      warnings: [],
    };
    const sameStageOverlay: DependencyOverlay = {
      board_id: 'board-1',
      view: 'dependency',
      dependency_scope: 'lineage',
      lineage_node_ids: ['spec:spec-a', 'spec:spec-b'],
      lineage_entities: [
        { entity_type: 'spec', entity_id: 'spec-a' },
        { entity_type: 'spec', entity_id: 'spec-b' },
      ],
      selected: { entity_type: 'spec', entity_id: 'spec-a' },
      root_ideation: { id: 'ideation-1', title: 'Integer column lineage' },
      resolution_path: [{ type: 'spec', id: 'spec-a' }],
      nodes: [
        {
          id: 'spec:spec-a',
          entity_type: 'spec',
          entity_id: 'spec-a',
          title: 'Spec A overlay',
          label: 'Spec A overlay',
          stage: 0,
        },
        {
          id: 'spec:spec-b',
          entity_type: 'spec',
          entity_id: 'spec-b',
          title: 'Spec B overlay',
          label: 'Spec B overlay',
          stage: 1,
        },
      ],
      edges: [
        {
          id: 'precedes:a-b',
          source: 'spec:spec-a',
          target: 'spec:spec-b',
          relationship: 'precedes',
        },
      ],
      summary: { specs: 2, nodes: 2, edges: 1 },
      warnings: [],
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? sameStageOverlay : sameStageLineage));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));
    await screen.findByTestId('flow-edge-precedes:a-b');

    const nodeIds = [
      'origin:ideation-1',
      'origin:refinement-1',
      'origin:spec-a',
      'origin:spec-b',
    ];
    const rectangles = nodeIds.map((nodeId) => {
      const node = screen.getByTestId(`flow-node-${nodeId}`);
      return {
        nodeId,
        x: Number(node.dataset.x),
        y: Number(node.dataset.y),
        width: 236,
        height: 136,
      };
    });
    const overlaps: string[] = [];
    rectangles.forEach((left, leftIndex) => {
      rectangles.slice(leftIndex + 1).forEach((right) => {
        const horizontalOverlap = Math.abs(left.x - right.x) < left.width;
        const verticalOverlap = Math.abs(left.y - right.y) < left.height;
        if (horizontalOverlap && verticalOverlap) {
          overlaps.push(`${left.nodeId} / ${right.nodeId}`);
        }
      });
    });

    expect(overlaps).toEqual([]);
    expect(Number(screen.getByTestId('flow-node-origin:spec-a').dataset.x)).toBeLessThan(
      Number(screen.getByTestId('flow-node-origin:spec-b').dataset.x),
    );
  });

  it('disables edge animation for dense dependency overlays', async () => {
    const denseDependencyGraph: DependencyOverlay = {
      ...specDependencyGraph,
      edges: Array.from({ length: 81 }, (_, index) => ({
        id: `precedes:dense-${index}`,
        source: 'spec:spec-b',
        target: 'spec:spec-a',
        relationship: 'precedes',
      })),
      summary: { specs: 4, nodes: 7, edges: 81 },
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
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();
  });

  it('shows an empty overlay state while preserving the complete lineage', async () => {
    const isolatedDependencyGraph: DependencyOverlay = {
      ...specDependencyGraph,
      nodes: specDependencyGraph.nodes.filter((node) => (
        node.entity_id === 'spec-a' || node.entity_id === 'task-a'
      )),
      edges: [],
      summary: { specs: 1, cards: 1, nodes: 2, edges: 0 },
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

    expect(await screen.findByText(
      'No active Spec or Task dependencies in this lineage.',
    )).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:spec-a')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:task-a')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();
  });

  it('offers dependency view for any entity with a resolved lineage', async () => {
    const storyOverlay: DependencyOverlay = {
      ...storyGraph,
      view: 'dependency',
      dependency_scope: 'lineage',
      lineage_node_ids: [],
      lineage_entities: [],
      nodes: [],
      edges: [],
      summary: { nodes: 0, edges: 0 },
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? storyOverlay : storyGraph));
    render(<LineageGraphModal boardId="board-1" />);

    act(() => {
      openLineageGraph('story', 'story-1');
    });

    await screen.findByTestId('flow-node-node-story-1');
    expect(screen.getByRole('button', { name: 'Dependencies' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Graph view' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Dependencies' }));
    expect(await screen.findByText(
      'No active Spec or Task dependencies in this lineage.',
    )).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-node-story-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-node-ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-edge-story-ideation')).toBeInTheDocument();
  });

  it('keeps lineage visible while the dependency overlay is loading', async () => {
    let resolveOverlay!: (value: DependencyOverlay) => void;
    const pendingOverlay = new Promise<DependencyOverlay>((resolve) => {
      resolveOverlay = resolve;
    });
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => view === 'dependency' ? pendingOverlay : Promise.resolve(specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Loading dependency overlay...');
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();

    await act(async () => {
      resolveOverlay(specDependencyGraph);
      await pendingOverlay;
    });
    expect(await screen.findByTestId('flow-node-spec:spec-b')).toBeInTheDocument();
  });

  it('keeps lineage visible and retryable when the dependency overlay fails', async () => {
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => view === 'dependency'
      ? Promise.reject(new Error('Dependency overlay is unavailable'))
      : Promise.resolve(specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Dependency overlay is unavailable');
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:task-a')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry overlay' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh lineage' })).toBeInTheDocument();
  });

  it('never composes a dependency snapshot requested during a lineage refresh', async () => {
    const refreshedLineage: LineageGraphResponse = {
      ...specLineageGraph,
      root_ideation: {
        ...specLineageGraph.root_ideation,
        title: 'Refreshed Spec A lineage',
      },
      nodes: [
        ...specLineageGraph.nodes,
        {
          id: 'origin:spec-e',
          entity_type: 'spec',
          entity_id: 'spec-e',
          title: 'Spec E',
          label: 'Spec E',
          status: 'approved',
          stage: 2,
        },
      ],
      edges: [
        ...specLineageGraph.edges,
        {
          id: 'lineage:ideation-spec-e',
          source: 'origin:ideation-1',
          target: 'origin:spec-e',
          relationship: 'direct_spec',
        },
      ],
    };
    const refreshedOverlay: DependencyOverlay = {
      ...specDependencyGraph,
      lineage_node_ids: ['spec:spec-a', 'spec:spec-e', 'task:task-a'],
      lineage_entities: [
        { entity_type: 'card', entity_id: 'task-a' },
        { entity_type: 'spec', entity_id: 'spec-a' },
        { entity_type: 'spec', entity_id: 'spec-e' },
      ],
      nodes: [
        specDependencyGraph.nodes.find((node) => node.id === 'spec:spec-a')!,
        specDependencyGraph.nodes.find((node) => node.id === 'task:task-a')!,
        {
          id: 'spec:spec-e',
          entity_type: 'spec',
          entity_id: 'spec-e',
          title: 'Spec E overlay',
          label: 'Spec E overlay',
          status: 'approved',
          stage: 0,
        },
        {
          id: 'spec:spec-new-dependent',
          entity_type: 'spec',
          entity_id: 'spec-new-dependent',
          title: 'New dependent',
          label: 'New dependent',
          status: 'draft',
          stage: 1,
        },
      ],
      edges: [
        {
          id: 'precedes:e-new',
          source: 'spec:spec-e',
          target: 'spec:spec-new-dependent',
          relationship: 'precedes',
        },
      ],
    };
    let resolveRefreshedLineage!: (value: LineageGraphResponse) => void;
    const pendingRefreshedLineage = new Promise<LineageGraphResponse>((resolve) => {
      resolveRefreshedLineage = resolve;
    });
    let lineageRequestCount = 0;
    let refreshedLineagePublished = false;
    let dependencyRequestCount = 0;
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => {
      if (view === 'dependency') {
        dependencyRequestCount += 1;
        if (dependencyRequestCount === 1) {
          return Promise.reject(new Error('Dependency snapshot expired'));
        }
        return Promise.resolve(
          refreshedLineagePublished ? refreshedOverlay : specDependencyGraph,
        );
      }
      lineageRequestCount += 1;
      return lineageRequestCount === 1
        ? Promise.resolve(specLineageGraph)
        : pendingRefreshedLineage;
    });

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Dependency snapshot expired');

    fireEvent.click(screen.getByRole('button', { name: 'Refresh lineage' }));
    fireEvent.click(screen.getByRole('button', { name: 'Dependencies' }));
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();

    refreshedLineagePublished = true;
    await act(async () => {
      resolveRefreshedLineage(refreshedLineage);
      await pendingRefreshedLineage;
    });

    expect(await screen.findByTestId('flow-node-spec:spec-new-dependent')).toBeInTheDocument();
    expect(screen.getByTestId('flow-node-origin:spec-e')).toBeInTheDocument();
    expect(screen.queryByTestId('flow-node-spec:spec-b')).not.toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-precedes:e-new')).toBeInTheDocument();
    expect(apiMock.getLineageGraph).toHaveBeenCalledTimes(4);
  });

  it('fails closed on a lineage membership drift without hiding the base graph', async () => {
    const driftedOverlay: DependencyOverlay = {
      ...specDependencyGraph,
      lineage_entities: [{ entity_type: 'spec', entity_id: 'spec-a' }],
    };
    apiMock.getLineageGraph.mockImplementation((
      _boardId: string,
      _entityType: string,
      _entityId: string,
      _includeArtifacts: boolean,
      view?: string,
    ) => Promise.resolve(view === 'dependency' ? driftedOverlay : specLineageGraph));

    render(<LineageGraphModal boardId="board-1" />);
    act(() => {
      openLineageGraph('spec', 'spec-a');
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Dependencies' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The server returned an incompatible dependency graph',
    );
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();
    expect(screen.queryByTestId('flow-edge-precedes:c-b')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry overlay' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh lineage' })).toBeInTheDocument();
  });

  it('rejects an old-server lineage payload without replacing the base lineage', async () => {
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
    expect(screen.getByTestId('flow-node-origin:ideation-1')).toBeInTheDocument();
    expect(screen.getByTestId('flow-edge-lineage:ideation-spec')).toBeInTheDocument();
    expect(screen.queryByTestId('flow-edge-precedes:c-b')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry overlay' })).toBeInTheDocument();
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
    expect(screen.getByRole('group', { name: 'Graph legend: SDLC stages' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Origin and derivation graph for Spec A lineage' })).toBeInTheDocument();
  });
});
