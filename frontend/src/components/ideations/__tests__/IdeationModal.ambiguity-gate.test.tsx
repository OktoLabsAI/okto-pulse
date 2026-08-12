import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { IdeationModal } from '../IdeationModal';
import type {
  CurrentQualityAssessment,
  Ideation,
  QualityAssessmentReceipt,
} from '@/types';

const apiMock = vi.hoisted(() => ({
  getIdeation: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listIdeationSnapshots: vi.fn(),
  listIdeationKnowledge: vi.fn(),
  listIdeationHistory: vi.fn(),
  listIdeationQA: vi.fn(),
  getAllowedTransitions: vi.fn(),
  moveIdeation: vi.fn(),
  deleteIdeation: vi.fn(),
  updateIdeation: vi.fn(),
  setIdeationAmbiguityGateSkip: vi.fn(),
  getValidationCycle: vi.fn(),
  getValidationTechnicalAudit: vi.fn(),
  getCurrentQualityAssessment: vi.fn(),
  listQualityAssessments: vi.fn(),
  listQualityFindings: vi.fn(),
  recordAmbiguityAssessment: vi.fn(),
}));

const boardState = vi.hoisted(() => ({
  currentBoard: { id: 'board-1', owner_id: 'owner-1', agents: [], settings: {} } as any,
}));
const permissionState = vi.hoisted(() => ({
  canReadQuality: true,
  canAssessQuality: true,
  canReadPolicyCompliance: true,
}));
const policyRejectionState = vi.hoisted(() => ({
  value: null as null | { code: string },
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: null,
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => {
      if (flag === 'ideation.quality.read') return permissionState.canReadQuality;
      if (flag === 'ideation.quality.assess') return permissionState.canAssessQuality;
      if (flag === 'guidelines.assessments.read') {
        return permissionState.canReadPolicyCompliance;
      }
      return false;
    },
  }),
}));
vi.mock('@/store/dashboard', () => ({ useCurrentBoard: () => boardState.currentBoard }));
vi.mock('@/lib/exportMarkdown', () => ({
  exportIdeation: vi.fn(() => '# x'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((s: string) => s),
}));
vi.mock('@/components/traceability', () => ({ openLineageGraph: vi.fn() }));
vi.mock('@/components/architecture', () => ({ ArchitectureTab: () => <div /> }));
vi.mock('@/components/resources/ResourceGateDisclosure', () => ({ ResourceGateDisclosure: () => <div /> }));
vi.mock('@/components/specs/MockupsTab', () => ({ MockupsTab: () => <div /> }));
vi.mock('@/components/shared/MentionInput', () => ({ MentionInput: () => <div /> }));
vi.mock('@/components/shared/MarkdownContent', () => ({ MarkdownContent: ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildIdeationItems: vi.fn(() => []),
  compileSelectedContext: vi.fn(() => ''),
}));
vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => <div>{value ? renderView(value) : placeholder}</div>,
}));
vi.mock('@/components/policy-compliance', () => ({
  requirePolicyTransitionEnvelope: (response: {
    allowed_transitions: unknown[];
  }) => response.allowed_transitions,
  readPolicyTransitionRejection: () => policyRejectionState.value,
  policyTransitionRejectionMessage: (rejection: { code: string }) =>
    `Authoritative rejection: ${rejection.code}`,
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
    subjectEdition,
    presentationMode,
  }: {
    boardId: string;
    entityType: string;
    subjectId: string;
    subjectEdition?: number;
    presentationMode?: string;
  }) => (
    <div
      data-testid="policy-compliance-panel"
      data-board-id={boardId}
      data-entity-type={entityType}
      data-subject-id={subjectId}
      data-subject-edition={subjectEdition}
      data-presentation-mode={presentationMode}
    />
  ),
  PolicyComplianceTransitionPreview: ({
    rejection,
    presentationMode,
  }: {
    rejection?: { code: string } | null;
    presentationMode?: string;
  }) => (
    <div
      data-testid="policy-transition-preview"
      data-presentation-mode={presentationMode}
    >
      {rejection?.code}
    </div>
  ),
}));

const toastMock = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: toastMock }));

function ideationWith(overrides: Partial<Ideation>): Ideation {
  return {
    id: 'ideation-1',
    board_id: 'board-1',
    title: 'My Ideation',
    description: 'An idea',
    problem_statement: 'A problem',
    proposed_approach: 'An approach',
    scope_assessment: { domains: 1, ambiguity: 4, dependencies: 1 },
    complexity: 'medium',
    screen_mockups: [],
    architecture_designs: [],
    status: 'evaluating',
    version: 2,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-05-06T10:00:00Z',
    updated_at: '2026-05-06T10:00:00Z',
    labels: [],
    skip_ambiguity_gate: false,
    refinements: [],
    stories: [],
    specs: [],
    knowledge_bases: [],
    qa_items: [],
    ...overrides,
  };
}

function receipt(): QualityAssessmentReceipt {
  return {
    id: 'receipt-1',
    board_id: 'board-1',
    subject_type: 'ideation',
    subject_id: 'ideation-1',
    subject_version: 2,
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
    score: 4,
    justification: 'Unresolved scope uncertainty',
    digests: {
      content_digest: 'content',
      clarification_digest: 'clarification',
      ruleset_digest: 'ruleset',
      taxonomy_digest: 'taxonomy',
      policy_digest: 'policy',
      input_digest: 'input',
      canonicalization_version: 'v1',
    },
    versions: {
      ruleset_version: 'v1',
      taxonomy_version: 'v1',
      analyzer_version: 'v1',
      policy_version: 'v1',
    },
    run_identity_digest: 'run',
    authority_digest: 'authority',
    idempotency_key: 'idem',
    request_digest: 'request',
    created_by: 'agent-1',
    created_at: '2026-07-28T12:00:00Z',
    predecessor_receipt_id: null,
    contract_version: 'quality-assessment/v1',
  };
}

function currentAssessment(
  reasonCode:
    | 'ambiguity_score_exceeds_threshold'
    | 'ambiguity_gate_skipped'
    | 'ambiguity_gate_ready' = 'ambiguity_score_exceeds_threshold',
): CurrentQualityAssessment {
  const skipped = reasonCode === 'ambiguity_gate_skipped';
  const allowed = skipped || reasonCode === 'ambiguity_gate_ready';
  return {
    receipt: receipt(),
    head_revision: 3,
    currentness: 'current',
    stale_reasons: [],
    gate_preview: {
      applicable: true,
      enabled: true,
      allowed,
      reason_code: reasonCode,
      threshold: 3,
      score: 4,
      skipped,
    },
  };
}

function page<T>(items: T[]) {
  return {
    items,
    total_filtered: items.length,
    total_overall: items.length,
    offset: 0,
    limit: 25,
  };
}

function blockedPolicyDecision() {
  return {
    state: 'policy_compliance_blocked',
    allowed: false,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_blocked'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_id: 'policy-receipt-1',
    currentness: 'current',
    currentness_reasons: [],
    applicable_rule_count: 2,
    applicable_blocking_rule_count: 1,
    blocking_rule_count: 1,
    waived_rule_count: 0,
    advisory_issue_count: 0,
  };
}

function openAmbiguityAssessment() {
  fireEvent.click(screen.getByRole('tab', { name: 'Evaluation' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Ambiguity Assessment' }));
}

describe('IdeationModal Max ambiguity gate panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.canReadQuality = true;
    permissionState.canAssessQuality = true;
    permissionState.canReadPolicyCompliance = true;
    policyRejectionState.value = null;
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_ideation_ambiguity_gate: true, max_ideation_ambiguity: 3 },
    };
    apiMock.getIdeation.mockResolvedValue(ideationWith({}));
    apiMock.listIdeationSnapshots.mockResolvedValue([]);
    apiMock.listIdeationKnowledge.mockResolvedValue([]);
    apiMock.listIdeationHistory.mockResolvedValue([]);
    apiMock.listIdeationQA.mockResolvedValue([]);
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'ideation',
      entity_id: 'ideation-1',
      current_status: 'evaluating',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        { to_status: 'done', label: 'Done', gate: 'ambiguity_resource_cognitive', blocked_reason: null, policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'approved', label: 'Approved', gate: 'none', blocked_reason: null, policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'cancelled', label: 'Cancelled', gate: 'none', blocked_reason: null, policy_compliance: false, policy_compliance_decision: null },
      ],
    });
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment());
    apiMock.getValidationCycle.mockResolvedValue({
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      edition: 1,
      subject_status: 'evaluating',
      visible_sections: ['ambiguity_assessment'],
      cycle_state: 'completed',
      current_result: {
        result_id: 'receipt-1',
        result_type: 'ambiguity_assessment',
        subject_edition: 1,
        status: 'failed',
        summary: { score: 4, threshold: 3 },
      },
      previous_result_count: 0,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 1,
        expected_subject_version: 2,
        expected_head_revision: 3,
      },
    });
    apiMock.getValidationTechnicalAudit.mockResolvedValue(null);
    apiMock.listQualityAssessments.mockResolvedValue(page([]));
    apiMock.listQualityFindings.mockResolvedValue(page([]));
    apiMock.recordAmbiguityAssessment.mockResolvedValue({
      outcome: 'success',
      replayed: false,
      receipt_id: 'receipt-2',
      head_revision: 4,
      qa_id_map: {},
    });
  });

  it('shows the current-edition assessment without receipt or stale gate noise', async () => {
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    expect(screen.queryByTestId('ambiguity-gate-panel')).not.toBeInTheDocument();
    openAmbiguityAssessment();
    const panel = await screen.findByTestId('ambiguity-gate-panel');
    await screen.findByTestId('quality-current-result');
    expect(apiMock.getValidationCycle).toHaveBeenCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({
        includePrevious: false,
        signal: expect.any(AbortSignal),
      }),
    );
    expect(panel).toHaveTextContent('Ambiguity exceeds the allowed limit');
    expect(screen.getByRole('img', {
      name: 'Ambiguity score 4 out of 5',
    })).toBeInTheDocument();
    expect(panel).toHaveTextContent('Maximum accepted score 3');
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/receipt-1/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('quality-gate-preview')).not.toBeInTheDocument();
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('role', 'switch');
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('aria-checked', 'false');
  });

  it('composes Policy Compliance inside Evaluation with exact subject identity', async () => {
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByRole('tab', { name: 'Evaluation' }));
    fireEvent.click(
      screen.getByRole('tab', { name: 'Policy Compliance' }),
    );

    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-board-id',
      'board-1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-entity-type',
      'ideation',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-id',
      'ideation-1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-edition',
      '1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-presentation-mode',
      'lifecycle-edition',
    );
    expect(
      screen.getByTestId('policy-transition-preview'),
    ).toHaveAttribute('data-presentation-mode', 'lifecycle-edition');
  });

  it('does not expose Policy Compliance without its exact read capability', async () => {
    permissionState.canReadPolicyCompliance = false;
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByRole('tab', { name: 'Evaluation' }));

    expect(
      screen.queryByRole('tab', { name: 'Policy Compliance' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('policy-compliance-panel'),
    ).not.toBeInTheDocument();
  });

  it('fails closed on the governed forward action and preserves cancellation', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'ideation',
      entity_id: 'ideation-1',
      current_status: 'evaluating',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        {
          to_status: 'done',
          label: 'Done',
          gate: 'ideation_completion',
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

    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    expect(
      screen.queryByRole('button', { name: 'Done' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Cancelled' }),
    ).toBeInTheDocument();
  });

  it('surfaces a structured mutation rejection and refreshes authority', async () => {
    policyRejectionState.value = {
      code: 'policy_compliance_blocked',
    };
    apiMock.moveIdeation.mockRejectedValue(new Error('409 conflict'));

    render(
      <IdeationModal
        ideationId="ideation-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Authoritative rejection: policy_compliance_blocked',
      );
    });
    await waitFor(() => {
      expect(apiMock.getAllowedTransitions).toHaveBeenCalledTimes(2);
    });

    fireEvent.click(screen.getByRole('tab', { name: 'Evaluation' }));
    fireEvent.click(
      screen.getByRole('tab', { name: 'Policy Compliance' }),
    );
    expect(screen.getByTestId('policy-transition-preview'))
      .toHaveTextContent('policy_compliance_blocked');
  });

  it('persists skip through the dedicated endpoint and refreshes state', async () => {
    apiMock.setIdeationAmbiguityGateSkip.mockResolvedValue(ideationWith({ skip_ambiguity_gate: true }));
    const onChanged = vi.fn();
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={onChanged} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    await screen.findByTestId('quality-current-result');
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() => expect(apiMock.setIdeationAmbiguityGateSkip).toHaveBeenCalledWith(
      'ideation-1',
      {
        skip_ambiguity_gate: true,
        reason: 'Max ambiguity gate skipped from the ideation UI.',
        expected_ideation_version: 2,
        expected_ideation_edition: 1,
      },
    ));
    expect(onChanged).toHaveBeenCalled();
    await waitFor(() => expect(
      screen.getByTestId('toggle-skip-ambiguity-gate'),
    ).toHaveAttribute('aria-checked', 'true'));
    expect(apiMock.updateIdeation).not.toHaveBeenCalled();
  });

  it('surfaces the backend error through the toast path without a generic message', async () => {
    apiMock.setIdeationAmbiguityGateSkip.mockRejectedValue(new Error('Cannot update ambiguity gate skip for archived ideation.'));
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    await screen.findByTestId('quality-current-result');
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() =>
      expect(toastMock.error).toHaveBeenCalledWith('Cannot update ambiguity gate skip for archived ideation.'),
    );
  });

  it('keeps quality readable but removes the gate wrapper and skip control when the board gate is disabled', async () => {
    boardState.currentBoard = {
      id: 'board-1',
      owner_id: 'owner-1',
      agents: [],
      settings: { require_ideation_ambiguity_gate: false },
    };
    apiMock.getCurrentQualityAssessment.mockResolvedValue({
      ...currentAssessment('ambiguity_gate_ready'),
      gate_preview: {
        applicable: false,
        enabled: false,
        allowed: true,
        reason_code: 'not_applicable',
        threshold: null,
        score: 4,
        skipped: false,
      },
    });
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    expect(await screen.findByTestId('quality-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('ambiguity-gate-panel')).not.toBeInTheDocument();
    expect(screen.queryByTestId('toggle-skip-ambiguity-gate')).not.toBeInTheDocument();
  });

  it('keeps the governed gate and human skip visible without quality.read', async () => {
    permissionState.canReadQuality = false;
    permissionState.canAssessQuality = false;

    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    const panel = screen.getByTestId('ambiguity-gate-panel');
    expect(panel).toHaveTextContent(
      'The current assessment is omitted because Quality read permission is not available.',
    );
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toBeInTheDocument();
    expect(apiMock.getCurrentQualityAssessment).not.toHaveBeenCalled();
  });
});
