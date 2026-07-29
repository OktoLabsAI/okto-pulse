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

function openAmbiguityAssessment() {
  fireEvent.click(screen.getByRole('tab', { name: 'Evaluation' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Ambiguity Assessment' }));
}

describe('IdeationModal Max ambiguity gate panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.canReadQuality = true;
    permissionState.canAssessQuality = true;
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
      source: 'programmatic_backend_transition_authority',
      allowed_transitions: [
        { to_status: 'done', label: 'Done', gate: 'ambiguity_resource_cognitive', blocked_reason: null },
        { to_status: 'approved', label: 'Approved', gate: 'none', blocked_reason: null },
        { to_status: 'cancelled', label: 'Cancelled', gate: 'none', blocked_reason: null },
      ],
    });
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment());
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

  it('shows the server-projected score, threshold and gate result when the board gate is enabled', async () => {
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    expect(screen.queryByTestId('ambiguity-gate-panel')).not.toBeInTheDocument();
    openAmbiguityAssessment();
    const panel = await screen.findByTestId('ambiguity-gate-panel');
    await screen.findByTestId('quality-gate-preview');
    expect(apiMock.getCurrentQualityAssessment).toHaveBeenCalledWith(
      'ideation',
      'ideation-1',
      'ambiguity',
      expect.any(AbortSignal),
    );
    expect(panel).toHaveTextContent('Score: 4');
    expect(panel).toHaveTextContent('Threshold: 3');
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Blocked — score exceeds threshold',
    );
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('role', 'switch');
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toHaveAttribute('aria-checked', 'false');
  });

  it('persists skip through the dedicated endpoint and refreshes state', async () => {
    apiMock.getCurrentQualityAssessment
      .mockResolvedValueOnce(currentAssessment())
      .mockResolvedValue(currentAssessment('ambiguity_gate_skipped'));
    apiMock.setIdeationAmbiguityGateSkip.mockResolvedValue(ideationWith({ skip_ambiguity_gate: true }));
    const onChanged = vi.fn();
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={onChanged} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    await screen.findByTestId('quality-gate-preview');
    fireEvent.click(screen.getByTestId('toggle-skip-ambiguity-gate'));

    await waitFor(() => expect(apiMock.setIdeationAmbiguityGateSkip).toHaveBeenCalledWith('ideation-1', true));
    expect(onChanged).toHaveBeenCalled();
    // Entity skip state refreshes the server preview; the client never infers it.
    await waitFor(() => expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'Skipped by recorded override',
    ));
    expect(apiMock.getCurrentQualityAssessment).toHaveBeenCalledTimes(2);
    expect(apiMock.updateIdeation).not.toHaveBeenCalled();
  });

  it('surfaces the backend error through the toast path without a generic message', async () => {
    apiMock.setIdeationAmbiguityGateSkip.mockRejectedValue(new Error('Cannot update ambiguity gate skip for archived ideation.'));
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    openAmbiguityAssessment();
    await screen.findByTestId('quality-gate-preview');
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
      'The assessment and server gate preview are omitted because Quality read permission is not available.',
    );
    expect(screen.getByTestId('toggle-skip-ambiguity-gate')).toBeInTheDocument();
    expect(apiMock.getCurrentQualityAssessment).not.toHaveBeenCalled();
  });
});
