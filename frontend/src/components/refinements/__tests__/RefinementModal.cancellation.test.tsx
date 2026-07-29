/**
 * ITEM 17 — cancellation justification in the RefinementModal.
 *
 * Covers: (1) cancelling requires a reason (dialog intercepts the move and the
 * API is only called after confirm, with cancellation_reason), (2) the
 * cancellation details appear at the top of Details with the recorded
 * reason/actor/timestamp while the refinement is cancelled, (3) those
 * details do not exist for any other status (reopening therefore hides them).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RefinementModal } from '../RefinementModal';
import type { Refinement } from '@/types';

const apiMock = vi.hoisted(() => ({
  getRefinement: vi.fn(),
  getRefinementKnowledge: vi.fn(),
  listRefinementKnowledge: vi.fn(),
  getEffectiveResources: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listRefinementSnapshots: vi.fn(),
  listRefinementHistory: vi.fn(),
  listRefinementQA: vi.fn(),
  getAllowedTransitions: vi.fn(),
  moveRefinement: vi.fn(),
  deleteRefinement: vi.fn(),
  updateRefinement: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: 'owner-1', agents: [] }),
}));

vi.mock('@/lib/exportMarkdown', () => ({
  exportRefinement: vi.fn(() => '# refinement export'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((s: string) => s.toLowerCase().replace(/\s+/g, '-')),
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/resources/ResourceGateSummary', () => ({
  ResourceGateSummary: () => <div />,
}));

vi.mock('@/components/specs/MockupsTab', () => ({
  MockupsTab: () => <div />,
}));

vi.mock('@/components/ideations/IdeationModal', () => ({
  IdeationModal: () => <div />,
}));

vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: () => <div />,
}));

vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildRefinementItems: vi.fn(() => []),
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseRefinement: Refinement = {
  id: 'refinement-1',
  ideation_id: 'ideation-1',
  board_id: 'board-1',
  title: 'My Refinement',
  description: 'A refinement',
  in_scope: ['in'],
  out_of_scope: ['out'],
  analysis: 'analysis',
  decisions: ['decision'],
  screen_mockups: [],
  architecture_designs: [],
  status: 'review',
  version: 3,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-05-06T10:00:00Z',
  updated_at: '2026-05-06T10:00:00Z',
  labels: [],
  specs: [],
  qa_items: [],
  knowledge_bases: [],
};

const transitionsResponse = (allowed: string[]) => ({
  board_id: 'board-1',
  entity_type: 'refinement',
  entity_id: 'refinement-1',
  current_status: 'review',
  source: 'programmatic_backend_transition_authority',
  allowed_transitions: allowed.map((to_status) => ({
    to_status,
    label: to_status,
    gate: 'none',
    blocked_reason: null,
  })),
});

function mockCommonApis() {
  apiMock.listRefinementSnapshots.mockResolvedValue([]);
  apiMock.listRefinementHistory.mockResolvedValue([]);
  apiMock.listRefinementQA.mockResolvedValue([]);
  apiMock.getArchitectureDesign.mockResolvedValue(null);
  apiMock.listRefinementKnowledge.mockResolvedValue([]);
  apiMock.getEffectiveResources.mockResolvedValue({ resources: { knowledge_base: [] } });
}

describe('RefinementModal cancellation flow (ITEM 17)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCommonApis();
    apiMock.getRefinement.mockResolvedValue(baseRefinement);
    apiMock.getAllowedTransitions.mockResolvedValue(
      transitionsResponse(['approved', 'draft', 'cancelled']),
    );
  });

  it('cancelling requires a reason: dialog intercepts, then move sends cancellation_reason', async () => {
    const cancelledRefinement: Refinement = {
      ...baseRefinement,
      status: 'cancelled',
      cancellation_reason: 'Duplicated scope',
      cancelled_by: 'owner-1',
      cancelled_at: '2026-07-10T09:00:00Z',
    };
    apiMock.moveRefinement.mockResolvedValue(cancelledRefinement);

    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    await screen.findByText('My Refinement');

    // Clicking the Cancelled move button opens the dialog WITHOUT moving.
    fireEvent.click(screen.getByRole('button', { name: /cancelled/i }));
    expect(await screen.findByTestId('cancellation-reason-input')).toBeInTheDocument();
    expect(apiMock.moveRefinement).not.toHaveBeenCalled();

    // Confirm is disabled until a non-empty reason is provided.
    expect(screen.getByTestId('cancellation-dialog-confirm')).toBeDisabled();

    fireEvent.change(screen.getByTestId('cancellation-reason-input'), {
      target: { value: 'Duplicated scope' },
    });
    fireEvent.click(screen.getByTestId('cancellation-dialog-confirm'));

    await waitFor(() =>
      expect(apiMock.moveRefinement).toHaveBeenCalledWith('refinement-1', {
        status: 'cancelled',
        cancellation_reason: 'Duplicated scope',
      }),
    );
  });

  it('shows cancellation details first in Details while cancelled', async () => {
    apiMock.getRefinement.mockResolvedValue({
      ...baseRefinement,
      status: 'cancelled',
      cancellation_reason: '## Motivo\n\nEscopo **duplicado**',
      cancelled_by: 'agent-9',
      cancelled_at: '2026-07-10T09:00:00Z',
    });
    apiMock.getAllowedTransitions.mockResolvedValue(transitionsResponse([]));

    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    await screen.findByText('My Refinement');

    const details = await screen.findByTestId('cancellation-details');
    expect(details).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Cancellation' }),
    ).not.toBeInTheDocument();
    // Reason renders as markdown (heading + strong), not raw text.
    expect(screen.getByRole('heading', { name: 'Motivo' })).toBeInTheDocument();
    expect(screen.getByText('duplicado').tagName).toBe('STRONG');
    // Who + when
    expect(screen.getByText('agent-9')).toBeInTheDocument();
    expect(details.textContent).toContain(new Date('2026-07-10T09:00:00Z').getFullYear().toString());
  });

  it('hides cancellation details when the item is not cancelled (reopen)', async () => {
    apiMock.getRefinement.mockResolvedValue(baseRefinement); // status: review
    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    await screen.findByText('My Refinement');

    expect(
      screen.queryByRole('button', { name: 'Cancellation' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId('cancellation-details')).toBeNull();
  });
});
