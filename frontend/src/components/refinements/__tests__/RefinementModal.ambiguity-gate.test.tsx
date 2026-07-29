import type { ReactNode } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { BoardSettings, Refinement } from '@/types';
import { RefinementModal } from '../RefinementModal';

const apiMock = vi.hoisted(() => ({
  getRefinement: vi.fn(),
  getIdeation: vi.fn(),
  getAllowedTransitions: vi.fn(),
  setRefinementAmbiguityGateSkip: vi.fn(),
  updateRefinement: vi.fn(),
  deleteRefinement: vi.fn(),
  moveRefinement: vi.fn(),
  getRefinementKnowledge: vi.fn(),
  getArchitectureDesign: vi.fn(),
  getEffectiveResources: vi.fn(),
  listRefinementSnapshots: vi.fn(),
  listRefinementHistory: vi.fn(),
  listRefinementQA: vi.fn(),
  getCurrentQualityAssessment: vi.fn(),
}));

type TestBoardState = {
  currentBoard: {
    id: string;
    owner_id: string;
    agents: [];
    settings: Partial<BoardSettings>;
  };
};

const boardState = vi.hoisted((): TestBoardState => ({
  currentBoard: {
    id: 'board-1',
    owner_id: 'owner-1',
    agents: [],
    settings: {
      require_refinement_ambiguity_gate: true,
      max_refinement_ambiguity: 3,
    },
  },
}));

const toastMock = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: null,
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => (
      flag === 'refinement.quality.read'
      || flag === 'refinement.quality.assess'
      || flag === 'refinement.research_decisions.read'
    ),
  }),
}));
vi.mock('@/store/dashboard', () => ({ useCurrentBoard: () => boardState.currentBoard }));
vi.mock('react-hot-toast', () => ({ default: toastMock }));
vi.mock('@/lib/exportMarkdown', () => ({
  exportRefinement: vi.fn(() => '# refinement'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((value: string) => value),
}));
vi.mock('@/components/traceability', () => ({ openLineageGraph: vi.fn() }));
vi.mock('@/components/architecture', () => ({ ArchitectureTab: () => <div /> }));
vi.mock('@/components/resources/ResourceGateSummary', () => ({ ResourceGateSummary: () => <div /> }));
vi.mock('@/components/specs/MockupsTab', () => ({ MockupsTab: () => <div /> }));
vi.mock('@/components/ideations/IdeationModal', () => ({ IdeationModal: () => <div /> }));
vi.mock('@/components/shared/MentionInput', () => ({ MentionInput: () => <div /> }));
vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildRefinementItems: vi.fn(() => []),
}));
vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({
    value,
    renderView,
    placeholder,
  }: {
    value: string;
    renderView: (value: string) => ReactNode;
    placeholder: string;
  }) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));
vi.mock('../ResearchDecisionPanel', () => ({
  ResearchDecisionTab: () => (
    <input aria-label="Research decision draft" defaultValue="" />
  ),
}));

function refinementWith(overrides: Partial<Refinement> = {}): Refinement {
  return {
    id: 'refinement-1',
    ideation_id: 'ideation-1',
    board_id: 'board-1',
    title: 'My Refinement',
    description: 'A refinement',
    in_scope: ['in'],
    out_of_scope: ['out'],
    analysis: 'analysis',
    decisions: [],
    screen_mockups: [],
    architecture_designs: [],
    status: 'approved',
    version: 7,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-07-27T10:00:00Z',
    updated_at: '2026-07-27T10:00:00Z',
    labels: [],
    skip_ambiguity_gate: false,
    specs: [],
    qa_items: [],
    knowledge_bases: [],
    ...overrides,
  };
}

function currentAssessment(
  reasonCode:
    | 'ambiguity_gate_ready'
    | 'ambiguity_gate_skipped'
    | 'ambiguity_assessment_stale' = 'ambiguity_gate_ready',
) {
  const skipped = reasonCode === 'ambiguity_gate_skipped';
  const stale = reasonCode === 'ambiguity_assessment_stale';
  return {
    receipt: {
      id: 'receipt-ref-1',
      score: 2,
    },
    head_revision: 4,
    currentness: stale ? 'stale' : 'current',
    stale_reasons: stale ? ['content_changed'] : [],
    gate_preview: {
      applicable: true,
      enabled: true,
      allowed: !stale,
      reason_code: reasonCode,
      threshold: 3,
      score: 2,
      skipped,
    },
  };
}

describe('RefinementModal ambiguity gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: {
        require_refinement_ambiguity_gate: true,
        max_refinement_ambiguity: 3,
      },
    };
    apiMock.getRefinement.mockResolvedValue(refinementWith());
    apiMock.getIdeation.mockResolvedValue({
      id: 'ideation-1',
      title: 'Parent Ideation',
      version: 2,
    });
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'approved',
      source: 'programmatic_backend_transition_authority',
      allowed_transitions: [],
    });
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment());
  });

  it('shows the current server receipt and gate result without inventing a legacy score', async () => {
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    const panel = screen.getByTestId('refinement-ambiguity-gate-panel');
    await screen.findByTestId('quality-gate-preview');
    expect(apiMock.getCurrentQualityAssessment).toHaveBeenCalledWith(
      'refinement',
      'refinement-1',
      'ambiguity',
      expect.any(AbortSignal),
    );
    expect(panel).toHaveTextContent('Score: 2');
    expect(panel).toHaveTextContent('Threshold: 3');
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Ready',
    );
    expect(panel).not.toHaveTextContent('Current ambiguity:');
    expect(screen.getByTestId('refinement-ambiguity-skip-submit')).toBeDisabled();
  });

  it('applies a reasoned skip without a semantic version bump and renders the receipt', async () => {
    apiMock.getCurrentQualityAssessment
      .mockResolvedValueOnce(currentAssessment())
      .mockResolvedValue(currentAssessment('ambiguity_gate_skipped'));
    apiMock.setRefinementAmbiguityGateSkip.mockResolvedValue({
      skipped: true,
      activity_id: 'activity-42',
      version: 7,
    });
    const onChanged = vi.fn();
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.change(screen.getByTestId('refinement-ambiguity-skip-reason'), {
      target: { value: 'Accepted risk for this delivery.' },
    });
    fireEvent.click(screen.getByTestId('refinement-ambiguity-skip-submit'));

    await waitFor(() => {
      expect(apiMock.setRefinementAmbiguityGateSkip).toHaveBeenCalledWith(
        'refinement-1',
        {
          skip_ambiguity_gate: true,
          reason: 'Accepted risk for this delivery.',
          expected_refinement_version: 7,
        },
      );
    });
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByText('v7')).toBeInTheDocument();
    expect(screen.getByText('Approved')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Skipped by recorded override',
    ));
    expect(screen.getByTestId('refinement-ambiguity-skip-receipt')).toHaveTextContent(
      'activity-42',
    );
    expect(screen.getByTestId('refinement-ambiguity-skip-submit')).toHaveTextContent(
      'Remove skip',
    );
  });

  it('requires a new explicit reason when removing an existing skip', async () => {
    apiMock.getRefinement.mockResolvedValue(refinementWith({ skip_ambiguity_gate: true }));
    apiMock.setRefinementAmbiguityGateSkip.mockResolvedValue({
      skipped: false,
      activity_id: 'activity-43',
      version: 7,
    });
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    const submit = screen.getByTestId('refinement-ambiguity-skip-submit');
    expect(submit).toHaveTextContent('Remove skip');
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId('refinement-ambiguity-skip-reason'), {
      target: { value: 'The current assessment is now acceptable.' },
    });
    fireEvent.click(submit);

    await waitFor(() => {
      expect(apiMock.setRefinementAmbiguityGateSkip).toHaveBeenCalledWith(
        'refinement-1',
        {
          skip_ambiguity_gate: false,
          reason: 'The current assessment is now acceptable.',
          expected_refinement_version: 7,
        },
      );
    });
  });

  it('surfaces a version conflict without changing local gate state or losing the reason', async () => {
    apiMock.setRefinementAmbiguityGateSkip.mockRejectedValue(
      new Error('The refinement changed; refresh before applying this override.'),
    );
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    const reason = screen.getByTestId('refinement-ambiguity-skip-reason');
    fireEvent.change(reason, {
      target: { value: 'Accepted risk for this delivery.' },
    });
    fireEvent.click(screen.getByTestId('refinement-ambiguity-skip-submit'));

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'The refinement changed; refresh before applying this override.',
      );
    });
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Ready',
    );
    expect(reason).toHaveValue('Accepted risk for this delivery.');
    expect(screen.queryByTestId('refinement-ambiguity-skip-receipt')).not.toBeInTheDocument();
  });

  it('hides the panel when the board policy is disabled', async () => {
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_refinement_ambiguity_gate: false },
    };
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    expect(screen.queryByTestId('refinement-ambiguity-gate-panel')).not.toBeInTheDocument();
  });

  it('keeps the Research Decision tab mounted while another tab is active', async () => {
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByRole('button', { name: 'Research decisions' }));
    const draft = screen.getByLabelText('Research decision draft');
    fireEvent.change(draft, { target: { value: 'Preserve this draft' } });

    fireEvent.click(screen.getByRole('button', { name: 'Details' }));
    expect(screen.getByTestId('research-decisions-tab-state')).not.toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Research decisions' }));
    expect(screen.getByLabelText('Research decision draft')).toHaveValue(
      'Preserve this draft',
    );
  });
});
