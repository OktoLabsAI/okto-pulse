import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState, type ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  ProjectStructureMutationResponse,
  ProjectStructureNode,
  ProjectStructureProjectionResponse,
  ProjectStructureSnapshot,
  Spec,
} from '@/types';
import {
  ProjectStructureProjectionPanel,
  ProjectStructureTab,
} from '../ProjectStructureTab';

const apiMock = vi.hoisted(() => ({
  getProjectStructure: vi.fn(),
  mutateProjectStructure: vi.fn(),
  getCodeTraceabilityProjection: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

function node(
  id: string,
  parentId: string | null,
  position: number,
  overrides: Partial<ProjectStructureNode> = {},
): ProjectStructureNode {
  return {
    id,
    parent_id: parentId,
    position,
    kind: 'folder',
    name: id === 'psn_root' ? 'src' : 'entry.tsx',
    note: id === 'psn_root' ? 'Application source' : 'Application entry point',
    classification: 'to_be',
    state: 'planned',
    interpretation_limit: null,
    status: 'active',
    task_references: [],
    test_references: [],
    evidence_ids: [],
    ...overrides,
  };
}

const nodes = [
  node('psn_root', null, 0),
  node('psn_entry', 'psn_root', 0, { kind: 'file' }),
];

function snapshot(
  version = 1,
  snapshotNodes: ProjectStructureNode[] = nodes,
): ProjectStructureSnapshot {
  return {
    contract_version: 'project-structure/v1',
    state: snapshotNodes.length > 0 ? 'authored' : 'authored_empty',
    spec_id: 'spec-1',
    spec_version: version,
    authored: true,
    structure_revision: version,
    digest: 'a'.repeat(64),
    nodes: snapshotNodes,
  };
}

function mutation(
  version: number,
  mutationNodes: ProjectStructureNode[],
): ProjectStructureMutationResponse {
  return {
    replayed: false,
    spec_version: version,
    structure_revision: version,
    affected_node_ids: ['psn_entry'],
    nodes: mutationNodes,
  };
}

const spec = {
  id: 'spec-1',
  board_id: 'board-1',
  title: 'Project structure Spec',
  status: 'draft',
  version: 1,
  edition: 1,
  cards: [],
  project_structure: nodes,
  project_structure_revision: 1,
} as unknown as Spec;

function renderTab(overrides: Partial<ComponentProps<typeof ProjectStructureTab>> = {}) {
  return render(
    <ProjectStructureTab
      boardId="board-1"
      spec={spec}
      canCreate
      canUpdate
      canRevoke
      canRestore
      canReorder
      canLinkTask
      canUnlinkTask
      canLinkTest
      canUnlinkTest
      canLinkEvidence
      canUnlinkEvidence
      onStructureChange={vi.fn()}
      {...overrides}
    />,
  );
}

describe('ProjectStructureTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getProjectStructure.mockResolvedValue(snapshot());
    apiMock.mutateProjectStructure.mockResolvedValue(mutation(2, nodes));
  });

  it('defaults to a clean single-column View with inline notes and no technical node IDs', async () => {
    renderTab();

    const tree = await screen.findByRole('tree', { name: 'Project structure tree' });
    expect(tree).toBeInTheDocument();
    expect(screen.getAllByRole('treeitem')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'View' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Edit' })).toHaveAttribute('aria-pressed', 'false');
    const entry = screen.getByRole('treeitem', { name: /entry\.tsx/i });
    expect(within(entry).getByText('Note / Description')).toBeInTheDocument();
    expect(within(entry).getByText('Application entry point')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add root' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit entry.tsx' })).not.toBeInTheDocument();
    expect(screen.queryByText('psn_entry')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Collapse src' }));
    expect(screen.getAllByRole('treeitem')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Expand src' }));
    expect(screen.getAllByRole('treeitem')).toHaveLength(2);
  });

  it('reveals mutation controls only in Edit and presents a reference boundary as the single Note / Description', async () => {
    const linkedNodes = nodes.map((item) => item.id === 'psn_entry'
      ? {
        ...item,
        classification: 'reference_scaffold' as const,
        interpretation_limit: 'Reference only; it does not prove implementation.',
        task_references: [{
          task_id: 'task-1',
          role: 'modify' as const,
          classification_at_link: 'to_be' as const,
        }],
      }
      : item);
    apiMock.getProjectStructure.mockResolvedValue(snapshot(1, linkedNodes));
    renderTab({
      spec: {
        ...spec,
        project_structure: linkedNodes,
        cards: [{ id: 'task-1', title: 'Implement entry', card_type: 'normal' }],
      } as Spec,
    });

    await screen.findByRole('tree', { name: 'Project structure tree' });
    const entry = screen.getByRole('treeitem', { name: /entry\.tsx/i });
    expect(within(entry).getByText('Note / Description')).toBeInTheDocument();
    expect(within(entry).getByText('Reference only; it does not prove implementation.')).toBeInTheDocument();
    expect(within(entry).queryByText('Application entry point')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add root' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit entry.tsx' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unlink Implement entry' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Reference boundary/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.getByRole('button', { name: 'Edit' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Add root' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit entry.tsx' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Move entry.tsx to another folder' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unlink Implement entry' })).toBeInTheDocument();
    expect(screen.queryByText(/Reference boundary/i)).not.toBeInTheDocument();
  });

  it('uses the single Note / Description as the governed reference-scaffold boundary on write', async () => {
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add root' }));

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'reference.openapi.yaml' } });
    fireEvent.change(screen.getByLabelText('Classification'), { target: { value: 'reference_scaffold' } });
    fireEvent.change(screen.getByLabelText(/Note \/ Description/), { target: { value: 'Reference transport shape only.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(apiMock.mutateProjectStructure).toHaveBeenCalledTimes(1));
    expect(apiMock.mutateProjectStructure.mock.calls[0][2].operations[0].payload).toMatchObject({
      note: 'Reference transport shape only.',
      interpretation_limit: 'Reference transport shape only.',
    });
  });

  it('does not reload when the parent recreates its change callback', async () => {
    function RerenderingParent() {
      const [, setRevision] = useState(0);
      return (
        <ProjectStructureTab
          boardId="board-1"
          spec={spec}
          canCreate
          canUpdate
          canRevoke
          canRestore
          canReorder
          canLinkTask
          canUnlinkTask
          canLinkTest
          canUnlinkTest
          canLinkEvidence
          canUnlinkEvidence
          onStructureChange={() => setRevision((current) => current + 1)}
        />
      );
    }

    render(<RerenderingParent />);

    expect(await screen.findByRole('tree', { name: 'Project structure tree' })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getProjectStructure).toHaveBeenCalledTimes(1));
  });

  it('sends canonical entity_id/top-level operations for keyboard reorder', async () => {
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const entry = await screen.findByRole('treeitem', { name: /entry\.tsx/i });
    fireEvent.keyDown(entry, { key: 'ArrowUp', altKey: true });

    // It is already first among its siblings, so no mutation occurs. Reorder
    // the root below a second root to exercise the canonical batch payload.
    apiMock.getProjectStructure.mockResolvedValueOnce(snapshot(1, [
      node('psn_root', null, 0),
      node('psn_other', null, 1, { name: 'tests' }),
    ]));
    renderTab({ spec: { ...spec, project_structure: [
      node('psn_root', null, 0),
      node('psn_other', null, 1, { name: 'tests' }),
    ] } as Spec });
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' }).at(-1)!);
    const source = (await screen.findAllByRole('treeitem', { name: /src/i })).at(-1)!;
    fireEvent.keyDown(source, { key: 'ArrowDown', altKey: true });

    await waitFor(() => expect(apiMock.mutateProjectStructure).toHaveBeenCalled());
    const request = apiMock.mutateProjectStructure.mock.calls.at(-1)![2];
    expect(request.expected_spec_version).toBe(1);
    expect(request.expected_structure_revision).toBe(1);
    expect(request.operations).toEqual([{
      operation: 'reorder',
      payload: { parent_id: null, ordered_ids: ['psn_other', 'psn_root'] },
    }]);
  });

  it('preserves the exact note draft across 409 refresh/review/retry', async () => {
    const serverChanged = nodes.map((item) => item.id === 'psn_entry'
      ? { ...item, note: 'Someone else changed this note' }
      : item);
    const retried = nodes.map((item) => item.id === 'psn_entry'
      ? { ...item, note: 'My exact unsaved note' }
      : item);
    apiMock.mutateProjectStructure
      .mockRejectedValueOnce(new AuthenticatedFetchError({
        message: 'version conflict',
        status: 409,
        code: 'version_conflict',
      }))
      .mockResolvedValueOnce(mutation(3, retried));
    apiMock.getProjectStructure
      .mockResolvedValueOnce(snapshot(1))
      .mockResolvedValueOnce(snapshot(2, serverChanged));

    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit entry.tsx' }));
    const note = await screen.findByLabelText(/Note \/ Description/);
    fireEvent.change(note, { target: { value: 'My exact unsaved note' } });
    fireEvent.keyDown(note, { key: 'Enter', ctrlKey: true });

    expect(await screen.findByText('Concurrent change')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Refresh & review/i }));
    await waitFor(() => expect(apiMock.getProjectStructure).toHaveBeenCalledTimes(2));
    expect(await screen.findByLabelText(/Note \/ Description/)).toHaveValue('My exact unsaved note');

    fireEvent.click(screen.getByRole('button', { name: 'Retry saved input' }));
    await waitFor(() => expect(apiMock.mutateProjectStructure).toHaveBeenCalledTimes(2));
    const retriedRequest = apiMock.mutateProjectStructure.mock.calls[1][2];
    expect(retriedRequest.expected_spec_version).toBe(2);
    expect(retriedRequest.expected_structure_revision).toBe(2);
    expect(retriedRequest.operations[0]).toMatchObject({
      operation: 'update',
      entity_id: 'psn_entry',
      payload: { note: 'My exact unsaved note' },
    });
  });
});

describe('ProjectStructureProjectionPanel', () => {
  it('renders an accessible read-only ancestor projection and deep-links by opaque node internally', () => {
    const onOpenFull = vi.fn();
    const projection: ProjectStructureProjectionResponse = {
      contract_version: 'project-structure/v1',
      state: 'projected',
      spec_id: 'spec-1',
      spec_version: 4,
      authored: true,
      structure_revision: 3,
      digest: 'b'.repeat(64),
      reference_type: 'task',
      reference_id: 'task-1',
      nodes: [
        { node: nodes[0], depth: 1, direct: false, context_only: true, reference_role: null },
        { node: nodes[1], depth: 2, direct: true, context_only: false, reference_role: 'modify' },
      ],
      affected_references: [],
    };

    render(
      <ProjectStructureProjectionPanel
        projection={projection}
        loading={false}
        error={null}
        onRetry={vi.fn()}
        onOpenFull={onOpenFull}
      />,
    );

    expect(screen.getByRole('tree', { name: 'Read-only project structure projection' })).toBeInTheDocument();
    expect(screen.getAllByRole('treeitem')).toHaveLength(2);
    expect(screen.getByText('Context only')).toBeInTheDocument();
    expect(screen.getByText('Direct')).toBeInTheDocument();
    expect(screen.queryByText('psn_entry')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open entry.tsx in the full project structure' }));
    expect(onOpenFull).toHaveBeenCalledWith('psn_entry');
  });
});
