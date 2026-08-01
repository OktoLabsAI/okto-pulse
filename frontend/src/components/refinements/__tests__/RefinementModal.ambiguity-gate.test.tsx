import type { ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BoardSettings,
  CurrentQualityAssessment,
  QualityAssessmentReceipt,
  Refinement,
} from '@/types';
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
  listQualityAssessments: vi.fn(),
  listQualityFindings: vi.fn(),
  recordAmbiguityAssessment: vi.fn(),
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
const permissionState = vi.hoisted(() => ({
  flags: new Set([
    'refinement.quality.read',
    'refinement.quality.assess',
    'refinement.research_decisions.read',
  ]),
}));

const toastMock = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: null,
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => permissionState.flags.has(flag),
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
vi.mock('@/components/policy-compliance', () => ({
  requirePolicyTransitionEnvelope: (response: {
    allowed_transitions: unknown[];
  }) => response.allowed_transitions,
  readPolicyTransitionRejection: () => null,
  policyTransitionRejectionMessage: () => 'Policy Compliance rejected',
  isAllowedTransitionActionable: (transition: {
    policy_compliance?: boolean;
    policy_compliance_decision?: { allowed?: boolean } | null;
  }) => (
    transition.policy_compliance === false
    || (
      transition.policy_compliance === true
      && transition.policy_compliance_decision?.allowed === true
    )
  ),
  PolicyCompliancePanel: ({
    boardId,
    entityType,
    subjectId,
  }: {
    boardId: string;
    entityType: string;
    subjectId: string;
  }) => (
    <div
      data-testid="policy-compliance-panel"
      data-board-id={boardId}
      data-entity-type={entityType}
      data-subject-id={subjectId}
    />
  ),
  PolicyComplianceTransitionPreview: () => (
    <div data-testid="policy-transition-preview" />
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

function qualityReceipt(): QualityAssessmentReceipt {
  return {
    id: 'receipt-ref-1',
    board_id: 'board-1',
    subject_type: 'refinement',
    subject_id: 'refinement-1',
    subject_version: 7,
    assessment_kind: 'ambiguity',
    origin: 'human_or_agent',
    source: 'native',
    channel: 'rest',
    outcome: 'recorded',
    scale: {
      kind: 'ambiguity_score',
      minimum: 1,
      maximum: 5,
      direction: 'lower_better',
    },
    score: 2,
    justification: 'The remaining ambiguity is acceptable.',
    digests: {
      content_digest: 'a',
      clarification_digest: 'b',
      ruleset_digest: 'c',
      taxonomy_digest: 'd',
      policy_digest: 'e',
      input_digest: 'f',
      canonicalization_version: 'v1',
    },
    versions: {
      ruleset_version: 'v1',
      taxonomy_version: 'v1',
      analyzer_version: 'v1',
      policy_version: 'v1',
    },
    run_identity_digest: 'g',
    authority_digest: 'h',
    idempotency_key: 'idem-ref-1',
    request_digest: 'i',
    created_by: 'agent-1',
    created_at: '2026-07-27T10:00:00Z',
    predecessor_receipt_id: null,
    contract_version: 'quality-assessment/v1',
  };
}

function currentAssessment(
  reasonCode:
    | 'ambiguity_gate_ready'
    | 'ambiguity_gate_skipped'
    | 'ambiguity_assessment_stale' = 'ambiguity_gate_ready',
): CurrentQualityAssessment {
  const skipped = reasonCode === 'ambiguity_gate_skipped';
  const stale = reasonCode === 'ambiguity_assessment_stale';
  return {
    receipt: qualityReceipt(),
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

function blockedPolicyDecision() {
  return {
    state: 'policy_compliance_receipt_missing',
    allowed: false,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_receipt_missing'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_id: null,
    currentness: null,
    currentness_reasons: [],
    applicable_rule_count: 1,
    applicable_blocking_rule_count: 1,
    blocking_rule_count: 0,
    waived_rule_count: 0,
    advisory_issue_count: 0,
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
    permissionState.flags = new Set([
      'refinement.quality.read',
      'refinement.quality.assess',
      'refinement.research_decisions.read',
    ]);
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
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [],
    });
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment());
    apiMock.listQualityAssessments.mockResolvedValue({
      items: [],
      total_filtered: 0,
      total_overall: 0,
      offset: 0,
      limit: 25,
    });
    apiMock.listQualityFindings.mockResolvedValue({
      items: [],
      total_filtered: 0,
      total_overall: 0,
      offset: 0,
      limit: 25,
    });
  });

  it('exposes the agreed top-level information architecture in order', async () => {
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    const detailsTab = screen.getByRole('tab', { name: 'Details' });
    const tabBar = detailsTab.parentElement;
    expect(tabBar).not.toBeNull();
    expect(
      within(tabBar as HTMLElement)
        .getAllByRole('tab')
        .map((button) => button.textContent?.trim()),
    ).toEqual([
      'Details',
      'Research decisions',
      'Resources',
      'Q&A',
      'References',
      'Validation',
      'Versions',
      'Activity',
    ]);
    expect(
      within(tabBar as HTMLElement).queryByRole('tab', { name: 'Quality' }),
    ).not.toBeInTheDocument();
    expect(detailsTab).toHaveAttribute(
      'aria-controls',
      'refinement-refinement-1-details-panel',
    );
    expect(
      screen.getByRole('tab', { name: 'Research decisions' }),
    ).toHaveAttribute(
      'id',
      'refinement-refinement-1-research-decisions-tab',
    );
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute(
      'id',
      'refinement-refinement-1-activity-tab',
    );
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
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    const panel = screen.getByTestId('refinement-ambiguity-gate-panel');
    const preview = await screen.findByTestId('quality-gate-preview');
    expect(apiMock.getCurrentQualityAssessment).toHaveBeenCalledWith(
      'refinement',
      'refinement-1',
      'ambiguity',
      expect.any(AbortSignal),
    );
    expect(preview).toHaveTextContent('Score: 2');
    expect(preview).toHaveTextContent('Threshold: 3');
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Ready',
    );
    expect(panel).not.toHaveTextContent('Current ambiguity:');
    expect(screen.getByTestId('ambiguity-gate-skip-control')).toHaveTextContent(
      'Skip Max ambiguity gate',
    );
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute(
      'role',
      'switch',
    );
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('applies skip through the same switch pattern used by ideation', async () => {
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
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() => {
      expect(apiMock.setRefinementAmbiguityGateSkip).toHaveBeenCalledWith(
        'refinement-1',
        {
          skip_ambiguity_gate: true,
          reason: 'Max ambiguity gate skipped from the refinement UI.',
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
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.queryByTestId('refinement-ambiguity-skip-reason')).not.toBeInTheDocument();
  });

  it('removes an existing skip through the same switch', async () => {
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
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    const toggle = screen.getByTestId('toggle-skip-ambiguity-gate');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(apiMock.setRefinementAmbiguityGateSkip).toHaveBeenCalledWith(
        'refinement-1',
        {
          skip_ambiguity_gate: false,
          reason: 'Max ambiguity gate re-enabled from the refinement UI.',
          expected_refinement_version: 7,
        },
      );
    });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
  });

  it('surfaces a version conflict without changing local gate state', async () => {
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
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    const toggle = screen.getByTestId('toggle-skip-ambiguity-gate');
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'The refinement changed; refresh before applying this override.',
      );
    });
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Ready',
    );
    expect(toggle).toHaveAttribute('aria-checked', 'false');
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
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    expect(screen.queryByTestId('refinement-ambiguity-gate-panel')).not.toBeInTheDocument();
    expect(await screen.findByTestId('quality-panel')).toBeInTheDocument();
  });

  it('keeps Validation available for a required board gate without Quality read permission', async () => {
    permissionState.flags.delete('refinement.quality.read');
    permissionState.flags.delete('refinement.quality.assess');
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    expect(
      screen.getByTestId('refinement-ambiguity-gate-panel'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('refinement-ambiguity-currentness-note'),
    ).toHaveTextContent('Quality read permission is not available');
    expect(screen.queryByTestId('quality-panel')).not.toBeInTheDocument();
  });

  it('hides Validation only when neither Quality read nor the board gate applies', async () => {
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_refinement_ambiguity_gate: false },
    };
    permissionState.flags.delete('refinement.quality.read');
    permissionState.flags.delete('refinement.quality.assess');

    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    expect(
      screen.queryByRole('tab', { name: 'Validation' }),
    ).not.toBeInTheDocument();
  });

  it('keeps Validation available and selects Policy Compliance for a policy-only actor', async () => {
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_refinement_ambiguity_gate: false },
    };
    permissionState.flags = new Set([
      'guidelines.assessments.read',
    ]);

    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));

    expect(
      screen.getByRole('tab', { name: 'Policy Compliance' }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(
      screen.queryByRole('tab', { name: 'Ambiguity Assessment' }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-board-id',
      'board-1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-entity-type',
      'refinement',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-id',
      'refinement-1',
    );
    expect(
      screen.getByTestId('policy-transition-preview'),
    ).toBeInTheDocument();
  });

  it('fails closed on approved-to-done while keeping cancellation available', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'approved',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        {
          to_status: 'done',
          label: 'Done',
          gate: 'refinement_completion',
          policy_compliance: true,
          policy_compliance_decision: blockedPolicyDecision(),
        },
        {
          to_status: 'cancelled',
          label: 'Cancelled',
          gate: 'cancel',
          policy_compliance: false,
          policy_compliance_decision: null,
        },
      ],
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
    expect(
      screen.queryByRole('button', { name: 'Done' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Cancelled' }),
    ).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('tab', { name: 'Research decisions' }));
    const draft = screen.getByLabelText('Research decision draft');
    fireEvent.change(draft, { target: { value: 'Preserve this draft' } });

    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    expect(screen.getByTestId('research-decisions-tab-state')).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Research decisions' }));
    expect(screen.getByLabelText('Research decision draft')).toHaveValue(
      'Preserve this draft',
    );
  });
});
