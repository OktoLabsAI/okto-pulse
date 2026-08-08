// SK-B2-S1 — TS-16 on the SECOND human conclusion surface: the drag-and-drop
// "Execution Report Required" modal. Independent review of I4 found this
// surface had zero automated coverage (its conformance rested on code
// inspection alone), and that a rejected move could DISCARD the whole typed
// report. Both are covered here.
import type { ReactNode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CardStatus, CardSummary } from '@/types';

const mocks = vi.hoisted(() => {
  const moveCard = vi.fn();
  const emptyColumns = {
    not_started: [],
    started: [],
    in_progress: [],
    validation: [],
    on_hold: [],
    done: [],
    cancelled: [],
  };
  const card = {
    id: 'card-ie-1',
    board_id: 'board-1',
    spec_id: 'spec-1',
    title: 'Execution card',
    description: null,
    status: 'in_progress' as CardStatus,
    priority: 'high',
    position: 0,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
    due_date: null,
    labels: [],
    test_scenario_ids: null,
    conclusions: null,
    card_type: 'normal',
    origin_task_id: null,
    severity: null,
    linked_test_task_ids: null,
    archived: false,
    open_qa_count: 0,
  } as unknown as CardSummary;
  const dashboardState = {
    columns: { ...emptyColumns, in_progress: [card] },
    columnsMeta: {},
    columnsGeneration: 1,
    currentBoard: { id: 'board-1', owner_id: 'owner-1', agents: [] },
    openCardModal: vi.fn(),
    optimisticMoveCard: vi.fn(),
    beginColumnsGeneration: vi.fn(),
    applyColumnsBatch: vi.fn(),
    beginColumnPage: vi.fn(),
    applyColumnPage: vi.fn(),
    failColumnPage: vi.fn(),
  };
  const dashboardHook = Object.assign(
    (selector?: (state: typeof dashboardState) => unknown) =>
      (selector ? selector(dashboardState) : dashboardState),
    { getState: () => dashboardState },
  );
  const dragHandlers: {
    onDragStart?: (event: unknown) => void;
    onDragEnd?: (event: unknown) => void;
  } = {};
  return { moveCard, emptyColumns, card, dashboardState, dashboardHook, dragHandlers };
});

const permissionState = vi.hoisted(() => ({
  denied: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => ({
    getBoardColumns: vi.fn().mockResolvedValue({ columns: mocks.dashboardState.columns }),
    getBoardColumnPage: vi.fn(),
    lookupSpecs: vi.fn().mockResolvedValue([]),
    moveCard: mocks.moveCard,
  }),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: mocks.dashboardHook,
  useColumns: () => mocks.dashboardState.columns,
  useColumnsMeta: () => mocks.dashboardState.columnsMeta,
  useCurrentBoard: () => mocks.dashboardState.currentBoard,
}));

vi.mock('@/hooks/useCognitivePendingBadges', () => ({
  useCognitivePendingBadges: () => ({ badges: new Map() }),
}));

vi.mock('@/hooks/usePermissions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePermissions')>();
  return {
    ...actual,
    usePermissions: () => ({
      preset: 'full_control',
      isLoading: false,
      error: null,
      ownerReviewRequired: false,
      has: (flag: string) => !permissionState.denied.has(flag),
    }),
  };
});

// Capture onDragEnd so the drop can be driven deterministically in jsdom.
vi.mock('@dnd-kit/core', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@dnd-kit/core');
  return {
    ...actual,
    DndContext: ({
      children,
      onDragStart,
      onDragEnd,
    }: {
      children: ReactNode;
      onDragStart?: (event: unknown) => void;
      onDragEnd?: (event: unknown) => void;
    }) => {
      mocks.dragHandlers.onDragStart = onDragStart;
      mocks.dragHandlers.onDragEnd = onDragEnd;
      return <div data-testid="dnd-context">{children}</div>;
    },
    DragOverlay: ({ children }: { children?: ReactNode }) => <>{children}</>,
    useSensor: () => ({}),
    useSensors: () => [],
    useDroppable: () => ({ setNodeRef: () => {}, isOver: false }),
    useDraggable: () => ({
      setNodeRef: () => {},
      attributes: {},
      listeners: {},
      transform: null,
      isDragging: false,
    }),
  };
});

vi.mock('../CardModal', () => ({ CardModal: () => null }));
vi.mock('../CreateCardModal', () => ({ CreateCardModal: () => null }));
vi.mock('@/components/shared/CancellationReasonDialog', () => ({
  CancellationReasonDialog: () => null,
}));

import { KanbanBoard } from '../KanbanBoard';

function dropOnValidation() {
  // Drive the real handlers: the origin column is remembered on drag start
  // and the destination resolves from the rendered columns.
  act(() => {
    mocks.dragHandlers.onDragStart?.({ active: { id: mocks.card.id } });
  });
  act(() => {
    mocks.dragHandlers.onDragEnd?.({
      active: { id: mocks.card.id },
      over: { id: 'validation' },
    });
  });
}

async function fillReport() {
  fireEvent.change(
    await screen.findByPlaceholderText(/## Implementation Summary/),
    { target: { value: 'Executor claim from the DnD surface' } },
  );
  fireEvent.change(
    screen.getByPlaceholderText('Justify the completeness score...'),
    { target: { value: 'complete' } },
  );
  fireEvent.change(
    screen.getByPlaceholderText('Justify the drift score...'),
    { target: { value: 'no drift' } },
  );
}

describe('KanbanBoard DnD execution report — impact evidence (TS-16)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.denied = new Set();
    mocks.dragHandlers.onDragEnd = undefined;
  });

  it.each([
    'card.move.in_progress_to_validation',
    'card.interact_in.in_progress',
  ])('does not start a predictable move when %s is false', async (denied) => {
    permissionState.denied = new Set([denied]);
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());

    dropOnValidation();

    expect(screen.queryByText('Execution Report Required')).not.toBeInTheDocument();
    expect(mocks.moveCard).not.toHaveBeenCalled();
  });

  it('mounts the shared editor inside the max-w-lg modal', async () => {
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();

    expect(
      await screen.findByText('Execution Report Required'),
    ).toBeInTheDocument();
    const editor = screen.getByTestId('impact-evidence-editor');
    expect(editor).toBeInTheDocument();
    // Collapsed by default with its own internal scroll, so the modal keeps
    // its narrow width (AC-16).
    expect(editor).not.toHaveAttribute('open');
    expect(screen.getByTestId('impact-evidence-sections').className).toContain(
      'max-h-64',
    );
    expect(document.querySelector('.max-w-lg')).not.toBeNull();
  });

  it('keeps the modal open with the typed report when the require gate rejects', async () => {
    mocks.moveCard.mockRejectedValueOnce(
      new Error(
        'impact_evidence_required: This board requires declared impact evidence on the execution report',
      ),
    );
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();
    await fillReport();

    fireEvent.click(screen.getByRole('button', { name: /Complete & Move to/ }));

    expect(
      await screen.findByTestId('impact-evidence-gate-error'),
    ).toHaveTextContent('impact_evidence_required');
    expect(screen.getByText('Execution Report Required')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/## Implementation Summary/),
    ).toHaveValue('Executor claim from the DnD surface');
    expect(mocks.dashboardState.optimisticMoveCard).not.toHaveBeenCalled();
  });

  it('shows the structured remediation the gate returned, not just the refusal', async () => {
    const rejection = Object.assign(
      new Error('impact_evidence_required: declared impact evidence missing'),
      {
        details: {
          code: 'impact_evidence_required',
          remediation:
            'Re-enumerate what the execution touched and resubmit the move '
            + 'with impact_evidence (schema_version=1).',
        },
      },
    );
    mocks.moveCard.mockRejectedValueOnce(rejection);
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();
    await fillReport();

    fireEvent.click(screen.getByRole('button', { name: /Complete & Move to/ }));

    expect(
      await screen.findByTestId('impact-evidence-gate-remediation'),
    ).toHaveTextContent('Re-enumerate what the execution touched');
  });

  it('omits the remediation line when the rejection carries none', async () => {
    mocks.moveCard.mockRejectedValueOnce(new Error('plain failure'));
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();
    await fillReport();

    fireEvent.click(screen.getByRole('button', { name: /Complete & Move to/ }));

    expect(
      await screen.findByTestId('impact-evidence-gate-error'),
    ).toHaveTextContent('plain failure');
    expect(
      screen.queryByTestId('impact-evidence-gate-remediation'),
    ).not.toBeInTheDocument();
  });

  it('never discards the report on a shape rejection (422) either', async () => {
    // Independent review of I4 caught this: FR-2 violations are reachable
    // ONLY through the UI, and the old else-branch closed the modal and wiped
    // every typed row.
    mocks.moveCard.mockRejectedValueOnce(
      new Error('[{"loc":["body","impact_evidence","files",0,"previous_path"]}]'),
    );
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();
    await fillReport();
    fireEvent.click(screen.getByTestId('impact-add-file'));

    fireEvent.click(screen.getByRole('button', { name: /Complete & Move to/ }));

    await waitFor(() =>
      expect(screen.getByTestId('impact-evidence-gate-error')).toBeInTheDocument(),
    );
    expect(screen.getByText('Execution Report Required')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/## Implementation Summary/),
    ).toHaveValue('Executor claim from the DnD surface');
    expect(screen.getByTestId('impact-file-row-0')).toBeInTheDocument();
  });

  it('sends the declared block and closes only on success', async () => {
    mocks.moveCard.mockResolvedValueOnce({});
    render(<KanbanBoard boardId="board-1" refreshKey={0} />);
    await waitFor(() => expect(mocks.dragHandlers.onDragEnd).toBeDefined());
    dropOnValidation();
    await fillReport();

    fireEvent.click(screen.getByTestId('impact-add-file'));
    fireEvent.change(screen.getByLabelText('file 0 path'), {
      target: { value: 'frontend/src/components/kanban/KanbanBoard.tsx' },
    });

    fireEvent.click(screen.getByRole('button', { name: /Complete & Move to/ }));

    await waitFor(() => expect(mocks.moveCard).toHaveBeenCalledTimes(1));
    expect(mocks.moveCard.mock.calls[0][1].impact_evidence).toEqual(
      expect.objectContaining({
        schema_version: 1,
        files: [
          {
            repo: 'core',
            path: 'frontend/src/components/kanban/KanbanBoard.tsx',
            change_kind: 'modified',
          },
        ],
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByText('Execution Report Required'),
      ).not.toBeInTheDocument(),
    );
  });
});
