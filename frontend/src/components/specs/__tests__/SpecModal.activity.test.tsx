import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import { SpecModal } from '../SpecModal';

const scenarioWatch = vi.hoisted(() => ({ options: null as null | Parameters<typeof import('@/hooks/useWorkspaceRevisions').useWorkspaceRevisions>[0] }));
vi.mock('@/hooks/useWorkspaceRevisions', () => ({ useWorkspaceRevisions: (options: NonNullable<typeof scenarioWatch.options>) => { scenarioWatch.options = options; return 'current'; } }));
import { persistTestScenariosWithWriteGuard } from '../scenarioWriteGuard';
import type { Spec, SpecHistoryEntry, TestScenario } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

type ValidationGateOverrideProps = {
  title?: string;
  description?: string;
};

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  getEffectiveResources: vi.fn(),
  listSprints: vi.fn(),
  getSprint: vi.fn(),
  listSpecQA: vi.fn(),
  createSpecQuestion: vi.fn(),
  answerSpecQuestion: vi.fn(),
  deleteSpecQuestion: vi.fn(),
  listSpecHistory: vi.fn(),
  listSpecKnowledge: vi.fn(),
  updateSpec: vi.fn(),
  updateScenarioVerificationMethod: vi.fn(),
  linkTaskToScenario: vi.fn(),
  unlinkTaskFromScenario: vi.fn(),
}));
const methodDeniedPermissions = vi.hoisted(() => new Set<string>());
const validationGateOverrideSpy = vi.hoisted(() => vi.fn());
const evidenceMatrixPropsSpy = vi.hoisted(() => vi.fn());
const currentBoardState = vi.hoisted(() => ({
  skipCodeEvidenceCoverageGlobal: false,
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({
    id: 'board-1',
    owner_id: null,
    agents: [],
    settings: {
      skip_code_evidence_coverage_global:
        currentBoardState.skipCodeEvidenceCoverageGlobal,
    },
  }),
}));

vi.mock('@/hooks/usePermissions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePermissions')>();
  return {
    ...actual,
    usePermissions: () => ({
      preset: null,
      isLoading: false,
      error: null,
      has: (permission: string) => !methodDeniedPermissions.has(permission),
    }),
  };
});

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/code-traceability', () => ({
  useCodeTraceabilityAuthority: () => ({ canReadProjection: true }),
  EvidenceMatrixPanel: (props: {
    boardSkipCoverage?: boolean;
    skipCoverage?: boolean;
    canEditCoverageFlags?: boolean;
    obligationTitles?: Readonly<Record<string, string>>;
    onSkipCoverageChange?: (skip: boolean) => Promise<void> | void;
  }) => {
    evidenceMatrixPropsSpy(props);
    return props.canEditCoverageFlags ? (
      <button
        type="button"
        role="switch"
        aria-label="Skip Code Evidence coverage"
        aria-checked={props.skipCoverage ?? false}
        onClick={() => void props.onSkipCoverageChange?.(!props.skipCoverage)}
      >
        Toggle Code Evidence coverage
      </button>
    ) : <div data-testid="read-only-code-evidence-matrix" />;
  },
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/policy-compliance', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/components/policy-compliance')>(),
  PolicyCompliancePanel: () => <div />,
}));

vi.mock('@/components/resources/ResourceGateSummary', () => ({
  ResourceGateSummary: () => <div />,
}));

vi.mock('@/components/shared/ValidationGateOverride', () => ({
  ValidationGateOverride: (props: ValidationGateOverrideProps) => {
    validationGateOverrideSpy(props);
    return (
      <div data-testid="validation-gate-override">
        <span>{props.title}</span>
        <span>{props.description}</span>
      </div>
    );
  },
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: () => <div />,
}));

vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
    />
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const spec: Spec = {
  execution_contract: { contract_version: 'spec-execution-contract/v1', board_id: 'board-1',
    spec_id: 'spec-activity-1', adopted_in_edition: 1, actor_id: 'author', origin: 'new_spec' },
  id: 'spec-activity-1',
  board_id: 'board-1',
  ideation_id: null,
  refinement_id: null,
  title: 'Activity integration spec',
  description: null,
  context: null,
  functional_requirements: [],
  technical_requirements: [],
  acceptance_criteria: [],
  test_scenarios: [],
  scenario_snapshot: 'a'.repeat(64),
  business_rules: [],
  api_contracts: [],
  integration_requirements: [],
  observability_requirements: [],
  decisions: [],
  screen_mockups: [],
  architecture_designs: [],
  skip_test_coverage: false,
  skip_code_evidence_coverage: false,
  status: 'draft',
  edition: 2,
  version: 3,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-05-29T10:00:00Z',
  updated_at: '2026-05-29T10:00:00Z',
  labels: [],
  cards: [],
  knowledge_bases: [],
  qa_items: [],
};

const historyEntry: SpecHistoryEntry = {
  id: 'history-activity-1',
  spec_id: spec.id,
  action: 'updated',
  actor_type: 'agent',
  actor_id: 'agent-1',
  actor_name: 'Specification Agent',
  summary: 'Specification title updated',
  version: 12,
  created_at: '2026-05-29T10:15:00Z',
  changes: [
    {
      field: 'title',
      old: 'Previous specification title',
      new: 'Current specification title',
    },
  ],
};


describe('SpecModal Activity tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    methodDeniedPermissions.clear();
    currentBoardState.skipCodeEvidenceCoverageGlobal = false;
    apiMock.getSpec.mockResolvedValue(spec);
    apiMock.getAllowedTransitions.mockResolvedValue({ allowed_transitions: [] });
    apiMock.listSprints.mockResolvedValue([]);
    apiMock.getSprint.mockResolvedValue({ cards: [] });
    apiMock.listSpecQA.mockResolvedValue([]);
    apiMock.listSpecHistory.mockResolvedValue([historyEntry]);
    apiMock.updateSpec.mockResolvedValue(spec);
    apiMock.getEffectiveResources.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'spec',
      entity_id: spec.id,
      profile: 'summary',
      items: [],
      next_cursor: null,
      resources: { architecture: [], mockup: [], knowledge_base: [] },
    });
  });

  it('keeps native Activity without offering imported historical context', async () => {
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    expect(screen.queryByRole('tab', { name: 'Historical context' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
    expect(apiMock.listSpecHistory).toHaveBeenCalled();
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });

  it('edits the scenario method through the scoped writer without replacing the scenario list', async () => {
    const scenario: TestScenario = { id: 'ts-method', title: 'Observe the condition', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', status: 'ready', notes: null, linked_criteria: [], linked_task_ids: [] };
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, status: 'draft', test_scenarios: [scenario] }).mockResolvedValue({ ...spec, status: 'draft', version: spec.version + 1, test_scenarios: [{ ...scenario, verification_method: 'automated_test' }] });
    apiMock.updateScenarioVerificationMethod.mockResolvedValue({ scenario_id: scenario.id, evidence_invalidated: false, updated_fields: ['verification_method'] });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title); fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByText(scenario.title));
    fireEvent.change(screen.getByLabelText('Verification method ts-method'), { target: { value: 'automated_test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save verification method' }));
    await waitFor(() => expect(apiMock.updateScenarioVerificationMethod).toHaveBeenCalledExactlyOnceWith(spec.board_id, spec.id, scenario.id, 'automated_test', spec.version));
    await waitFor(() => expect(apiMock.getSpec).toHaveBeenCalledTimes(2));
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });

  it.each(['review', 'validated', 'in_progress', 'done', 'archived', 'spec.tests.edit', 'spec.interact_in.draft'] as const)('keeps method authorship read-only for %s', async reason => {
    if (reason.startsWith('spec.')) methodDeniedPermissions.add(reason);
    const scenario: TestScenario = { id: 'ts-method', title: 'Observe the condition', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', status: 'ready', notes: null, linked_criteria: [], linked_task_ids: [], verification_method: 'inspection' };
    const status = reason === 'review' || reason === 'validated' || reason === 'in_progress' || reason === 'done' ? reason : 'draft';
    apiMock.getSpec.mockResolvedValue({ ...spec, status, archived: reason === 'archived', test_scenarios: [scenario] });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title); fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByText(scenario.title));
    expect(screen.queryByLabelText('Verification method ts-method')).not.toBeInTheDocument();
    expect(screen.getByText('Verification method: inspection')).toBeInTheDocument();
  });

  it('shows Details and its Task Validation Gate as read-only information', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    const gate = screen.getByRole('region', { name: 'Task Validation Gate' });
    expect(gate).toHaveTextContent('Task Validation Gate');
    expect(gate).toHaveTextContent('cards derived from this spec');
    expect(gate).toHaveTextContent('do not change the Spec Validation Gate');
    expect(validationGateOverrideSpy).not.toHaveBeenCalled();
    const details = screen.getByRole('tabpanel', { name: 'Details' });
    expect(within(details).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(details).queryByRole('button')).not.toBeInTheDocument();
    expect(within(details).queryByRole('combobox')).not.toBeInTheDocument();
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });

  it('loads and expands the shared Before/After history renderer', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );

    await screen.findByText(spec.title);
    expect(
      screen.getByLabelText('Edition 2'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));

    await waitFor(() => expect(apiMock.listSpecHistory).toHaveBeenCalledWith(spec.id));
    const actionBadge = await screen.findByText('Updated');
    expect(screen.getByText(historyEntry.summary!)).toBeInTheDocument();
    expect(screen.getByText(historyEntry.actor_name)).toBeInTheDocument();
    expect(screen.getByText('r12')).toHaveAttribute(
      'title',
      'Technical revision r12',
    );

    const entryToggle = actionBadge.closest('button');
    expect(entryToggle).not.toBeNull();
    expect(entryToggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(entryToggle!);

    expect(entryToggle).toHaveAttribute('aria-expanded', 'true');
    const before = screen.getByRole('region', { name: 'title before value' });
    const after = screen.getByRole('region', { name: 'title after value' });
    expect(within(before).getByText('Before')).toBeInTheDocument();
    expect(within(before).getByText('Previous specification title')).toBeInTheDocument();
    expect(within(after).getByText('After')).toBeInTheDocument();
    expect(within(after).getByText('Current specification title')).toBeInTheDocument();
  });

  it('opens Knowledge through the bounded Workspace without eager listing', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Resources' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Knowledge' }));

    await waitFor(() => {
      expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
        'board-1',
        'spec',
        spec.id,
        { profile: 'summary', limit: 25 },
      );
    });
    expect(apiMock.listSpecKnowledge).not.toHaveBeenCalled();
  });

  it('uses the consolidated top-level order without Spec Versions', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    const tabList = screen.getByRole('tablist', { name: 'Spec sections' });
    expect(
      within(tabList).getAllByRole('tab').map((tab) => tab.textContent),
    ).toEqual([
      'Details',
      'Coverage',
      'Project Structure',
      'Code Evidence Matrix',
      'Resources',
      'Requirements & Decisions',
      'Tests & Verifications',
      'Q&A',
      'Dependencies',
      'References',
      'KG Graph',
      'Validation',
      'Activity',
    ]);
    expect(
      within(tabList).queryByRole('tab', { name: 'Versions' }),
    ).not.toBeInTheDocument();
    expect(
      within(tabList).queryByRole('tab', { name: 'Quality' }),
    ).not.toBeInTheDocument();
    expect(
      within(tabList).queryByRole('tab', { name: 'Cards' }),
    ).not.toBeInTheDocument();
  });

  it('does not expose or request Sprints even with old pagination preferences', async () => {
    window.localStorage.setItem('okto.pagination.sprints.scope.board-1', JSON.stringify({ page: 2, pageSize: 25 }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    expect(screen.queryByRole('tab', { name: /Sprints/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Suggest Sprints/ })).not.toBeInTheDocument();
    expect(apiMock.listSprints).not.toHaveBeenCalled();
    expect(apiMock.getSprint).not.toHaveBeenCalled();
  });

  it('notifies the board list exactly once after asking and answering Q&A', async () => {
    const openQuestion = {
      id: 'qa-open',
      spec_id: spec.id,
      question: 'Which rollout window?',
      question_type: 'text',
      choices: null,
      allow_free_text: false,
      answer: null,
      selected: null,
      asked_by: 'agent-1',
      answered_by: null,
      created_at: '2026-08-14T10:00:00Z',
      answered_at: null,
    };
    const answeredQuestion = {
      ...openQuestion,
      answer: 'Tonight',
      answered_by: 'user-1',
      answered_at: '2026-08-14T11:00:00Z',
    };
    apiMock.listSpecQA
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([openQuestion])
      .mockResolvedValueOnce([answeredQuestion]);
    apiMock.createSpecQuestion.mockResolvedValue(openQuestion);
    apiMock.answerSpecQuestion.mockResolvedValue(answeredQuestion);
    const onChanged = vi.fn();

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Q&A' }));
    onChanged.mockClear();

    fireEvent.change(
      await screen.findByPlaceholderText('Ask a question... (type @ to mention)'),
      { target: { value: openQuestion.question } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByRole('button', { name: 'Answer this question' }));
    fireEvent.change(
      screen.getByPlaceholderText('Type your answer... (@ to mention)'),
      { target: { value: 'Tonight' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
    expect(apiMock.createSpecQuestion).toHaveBeenCalledTimes(1);
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(1);
  });

  it('persists the same-version Draft-only Code Evidence coverage skip from its own tab', async () => {
    const updatedSpec = {
      ...spec,
      skip_code_evidence_coverage: true,
      version: spec.version,
    };
    apiMock.updateSpec.mockResolvedValueOnce(updatedSpec);

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Code Evidence Matrix' }));
    const toggle = screen.getByRole('switch', { name: 'Skip Code Evidence coverage' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(apiMock.updateSpec).toHaveBeenCalledWith(spec.id, {
        skip_code_evidence_coverage: true,
      });
    });
    await waitFor(() => {
      expect(evidenceMatrixPropsSpy).toHaveBeenLastCalledWith(expect.objectContaining({
        boardSkipCoverage: false,
        skipCoverage: true,
        canEditCoverageFlags: true,
      }));
    });
  });

  it('opens directly on the Code Evidence Matrix and supplies human obligation titles', async () => {
    apiMock.getSpec.mockResolvedValueOnce({
      ...spec,
      integration_requirements: [{
        id: 'ir-checkout',
        title: 'Payment provider contract',
        integration_type: 'api',
        description: 'Keep the provider exchange compatible.',
        provider: null,
        consumer: null,
        contract_ref: null,
        endpoint: null,
        method: null,
        data_contract: null,
        linked_requirements: null,
        linked_api_contracts: null,
        linked_task_ids: null,
        status: 'active',
        notes: null,
      }],
    });

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        initialTab="evidence-matrix"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    expect(screen.getByRole('tab', { name: 'Code Evidence Matrix' }))
      .toHaveAttribute('aria-selected', 'true');
    await waitFor(() => {
      expect(evidenceMatrixPropsSpy).toHaveBeenLastCalledWith(expect.objectContaining({
        obligationTitles: expect.objectContaining({
          [spec.id]: `Spec: ${spec.title}`,
          'ir-checkout': 'IR-1: Payment provider contract: Keep the provider exchange compatible.',
        }),
      }));
    });
  });

  it('passes the effective Board-wide Code Evidence coverage skip to the matrix', async () => {
    currentBoardState.skipCodeEvidenceCoverageGlobal = true;

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Code Evidence Matrix' }));

    await waitFor(() => {
      expect(evidenceMatrixPropsSpy).toHaveBeenLastCalledWith(expect.objectContaining({
        boardSkipCoverage: true,
        skipCoverage: false,
        canEditCoverageFlags: true,
      }));
    });
  });

  it('does not expose Code Evidence coverage editing outside Draft', async () => {
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, status: 'validated' });

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Code Evidence Matrix' }));
    expect(screen.getByTestId('read-only-code-evidence-matrix')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Skip Code Evidence coverage' }))
      .not.toBeInTheDocument();
    expect(evidenceMatrixPropsSpy).toHaveBeenLastCalledWith(expect.objectContaining({
      canEditCoverageFlags: false,
    }));
  });

  it('does not expose Code Evidence coverage editing on an archived Draft Spec', async () => {
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, archived: true });

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Code Evidence Matrix' }));
    expect(screen.getByTestId('read-only-code-evidence-matrix')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Skip Code Evidence coverage' }))
      .not.toBeInTheDocument();
    expect(evidenceMatrixPropsSpy).toHaveBeenLastCalledWith(expect.objectContaining({
      canEditCoverageFlags: false,
    }));
  });

  it('shows cancellation audit in Details without a Cancellation tab', async () => {
    apiMock.getSpec.mockResolvedValueOnce({
      ...spec,
      status: 'cancelled',
      cancellation_reason: 'The capability is no longer required.',
      cancelled_by: 'user-2',
      cancelled_at: '2026-07-28T18:00:00Z',
    });

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    expect(screen.getByTestId('cancellation-details')).toHaveTextContent(
      'The capability is no longer required.',
    );
    expect(
      screen.queryByRole('tab', { name: 'Cancellation' }),
    ).not.toBeInTheDocument();
  });

  it('groups origin and derived cards under References', async () => {
    apiMock.getSpec.mockResolvedValueOnce({
      ...spec,
      cards: [
        {
          id: 'card-1',
          title: 'Implement deterministic lint view',
          status: 'validation',
        },
        {
          id: 'card-2',
          title: 'Repair rejected traceability',
          status: 'rejected',
        },
      ],
    });

    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^References/ }));
    expect(
      screen.getByText('No origin is registered for this spec.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /^Derived cards/ }));
    expect(
      screen.getByText('Implement deterministic lint view'),
    ).toBeInTheDocument();
    expect(screen.getByText('validation')).toHaveClass('bg-violet-100');
    expect(screen.getByText('rejected')).toHaveClass('bg-rose-100');
  });

  it('refuses an unsupported stored scenario type without rewriting or submitting it', async () => {
    const legacySpec: Spec = {
      ...spec,
      test_scenarios: [
        {
          id: 'ts-legacy',
          title: 'Historical regression type',
          linked_criteria: null,
          scenario_type: 'regression',
          given: 'legacy data exists',
          when: 'the scenario status changes',
          then: 'the original persisted type remains untouched',
          notes: null,
          status: 'draft',
          linked_task_ids: null,
        },
        {
          id: 'ts-negative',
          title: 'Supported negative type',
          linked_criteria: null,
          scenario_type: 'negative',
          given: 'invalid input',
          when: 'it is submitted',
          then: 'it is rejected',
          notes: null,
          status: 'draft',
          linked_task_ids: null,
        },
      ],
    };
    apiMock.getSpec.mockResolvedValue(legacySpec);
    apiMock.updateSpec.mockResolvedValue({
      ...legacySpec,
      test_scenarios: legacySpec.test_scenarios!.filter(
        (scenario) => scenario.id !== 'ts-negative',
      ),
    });

    render(
      <SpecModal
        specId={legacySpec.id}
        boardId={legacySpec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(legacySpec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    expect(
      await screen.findByText('regression (unsupported)'),
    ).toBeInTheDocument();

    expect(screen.getAllByTestId('test-scenario-status-badge')).toHaveLength(2);
    expect(screen.queryByDisplayValue('draft')).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Delete test scenario Supported negative type',
      }),
    );

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('Invalid scenario_type regression for scenario ts-legacy')));
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
    expect(legacySpec.test_scenarios?.[0].scenario_type).toBe('regression');
  });

  it('blocks a tampered new scenario with an absent type before the request', async () => {
    const updateSpec = vi.fn();
    const scenarioWithoutType = {
      id: 'ts-tampered',
      title: 'Missing type',
      linked_criteria: null,
      given: 'invalid state',
      when: 'the UI submits it',
      then: 'no request is sent',
      notes: null,
      status: 'draft',
      linked_task_ids: null,
    };
    const tamperedScenarios = [
      scenarioWithoutType,
      { ...scenarioWithoutType, scenario_type: undefined },
    ] as unknown as TestScenario[];

    for (const tampered of tamperedScenarios) {
      await expect(
        persistTestScenariosWithWriteGuard(
          updateSpec,
          spec.id,
          [tampered],
          spec.version,
          spec.scenario_snapshot!,
        ),
      ).rejects.toThrow(/Invalid scenario_type undefined for scenario/);
    }
    expect(updateSpec).not.toHaveBeenCalled();
  });

  it('materializes integration explicitly for the normal create flow', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByRole('button', { name: /Add Test Scenario/i }));
    fireEvent.change(screen.getByPlaceholderText('Scenario title'), {
      target: { value: 'Defaulted scenario' },
    });
    fireEvent.change(screen.getByPlaceholderText('Given: precondition...'), {
      target: { value: 'a valid precondition' },
    });
    fireEvent.change(screen.getByPlaceholderText('When: action...'), {
      target: { value: 'the action occurs' },
    });
    fireEvent.change(screen.getByPlaceholderText('Then: expected result...'), {
      target: { value: 'the result is observed' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));

    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(1));
    expect(
      apiMock.updateSpec.mock.calls[0][1].test_scenarios[0],
    ).toHaveProperty('scenario_type', 'integration');
    expect(apiMock.updateSpec.mock.calls[0][1].expected_spec_version).toBe(spec.version);
  });

  async function openScenarioDraft() {
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByRole('button', { name: /Add Test Scenario/i }));
    for (const [placeholder, value] of [
      ['Scenario title', 'Preserve this authored scenario'],
      ['Given: precondition...', 'A local draft exists'],
      ['When: action...', 'The native write is submitted'],
      ['Then: expected result...', 'Clear the draft only after confirmation'],
    ]) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
  }

  it('requires review of the authored scenario base after a method edit refreshes the Spec', async () => {
    const scenario: TestScenario = { id: 'ts-method', title: 'Observe the condition', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', status: 'ready', notes: null, linked_criteria: [], linked_task_ids: [] };
    const initial = { ...spec, test_scenarios: [scenario] };
    const refreshed = { ...initial, version: spec.version + 1, test_scenarios: [{ ...scenario, verification_method: 'inspection' as const }] };
    apiMock.getSpec.mockResolvedValueOnce(initial).mockResolvedValue(refreshed);
    apiMock.updateScenarioVerificationMethod.mockResolvedValue({ scenario_id: scenario.id });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByText(scenario.title));
    fireEvent.change(screen.getByLabelText('Verification method'), { target: { value: 'inspection' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save verification method' }));
    await screen.findByRole('button', { name: 'Review scenario base' });
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review scenario base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(1));
    expect(apiMock.updateSpec.mock.calls[0][1]).toEqual(expect.objectContaining({
      expected_spec_version: refreshed.version,
      test_scenarios: expect.arrayContaining([expect.objectContaining({ id: scenario.id, verification_method: 'inspection' })]),
    }));
  });

  it('preserves a scenario draft until the native write confirms and prevents duplicate submission', async () => {
    let confirm!: (value: Spec) => void;
    apiMock.updateSpec.mockReturnValueOnce(new Promise<Spec>(resolve => { confirm = resolve; }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: /Saving scenario/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Cancel$/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /Saving scenario/ }));
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
    confirm({ ...spec, version: 4, test_scenarios: apiMock.updateSpec.mock.calls[0][1].test_scenarios });
    await waitFor(() => expect(screen.queryByPlaceholderText('Scenario title')).not.toBeInTheDocument());
    expect(screen.getByText('Preserve this authored scenario')).toBeInTheDocument();
  });

  it('preserves a scenario draft and reports a definite native rejection inline', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 422, message: 'Scenario condition requires correction' }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Scenario condition requires correction');
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeEnabled();
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
  });

  it('reloads after a native version conflict without losing the draft or silently adopting the new base', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'spec_version_conflict' }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Reload scenarios' });
    expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeDisabled();
    apiMock.getSpec.mockRejectedValueOnce(new Error('Current read unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Current read unavailable'));
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, version: spec.version + 1 });
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review scenario base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(2));
    expect(apiMock.updateSpec.mock.calls[0][1].expected_spec_version).toBe(spec.version);
    expect(apiMock.updateSpec.mock.calls[1][1].expected_spec_version).toBe(spec.version + 1);
  });

  it('removes the scenario draft when its conflict reload is denied', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'spec_version_conflict' }));
    const onClose = vi.fn();
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={onClose} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Reload scenarios' });
    apiMock.getSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 403, message: 'Access denied' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.queryByPlaceholderText('Scenario title')).not.toBeInTheDocument();
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
  });

  it('requires explicit review after a same-version scenario snapshot conflict', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'scenario_snapshot_conflict' }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Reload scenarios' });
    expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeDisabled();
    expect(apiMock.updateSpec.mock.calls[0][1].expected_scenario_snapshot).toBe(spec.scenario_snapshot);
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, scenario_snapshot: 'b'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeEnabled());
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review scenario base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(2));
    expect(apiMock.updateSpec.mock.calls[1][1]).toEqual(expect.objectContaining({
      expected_spec_version: spec.version, expected_scenario_snapshot: 'b'.repeat(64),
    }));
  });

  it('does not author a scenario from a projection without its snapshot', async () => {
    apiMock.getSpec.mockResolvedValue({ ...spec, scenario_snapshot: undefined });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    expect(screen.getByRole('button', { name: /Add Test Scenario/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reload scenarios' })).toBeEnabled();
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });

  it('does not replace another Spec with a late scenario write response', async () => {
    let confirm!: (value: Spec) => void;
    apiMock.updateSpec.mockReturnValueOnce(new Promise<Spec>(resolve => { confirm = resolve; }));
    const view = render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    apiMock.getSpec.mockResolvedValue({ ...spec, id: 'other-spec', title: 'Other current Spec' });
    view.rerender(<SpecModal specId="other-spec" boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText('Other current Spec');
    confirm({ ...spec, version: 4, test_scenarios: apiMock.updateSpec.mock.calls[0][1].test_scenarios });
    await waitFor(() => expect(screen.getByText('Other current Spec')).toBeInTheDocument());
    // Flush the completed callback before asserting identity, not just the old DOM.
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByText(spec.title)).not.toBeInTheDocument();
    expect(screen.queryByText('Preserve this authored scenario')).not.toBeInTheDocument();
  });

  it('reconciles a committed create after a lost response without submitting twice', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Check scenario write' });
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    const request = apiMock.updateSpec.mock.calls[0][1];
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, version: spec.version + 1,
      scenario_snapshot: 'b'.repeat(64), test_scenarios: request.test_scenarios });
    fireEvent.click(screen.getByRole('button', { name: 'Check scenario write' }));
    await waitFor(() => expect(screen.queryByPlaceholderText('Scenario title')).not.toBeInTheDocument());
    expect(screen.getByText('Preserve this authored scenario')).toBeInTheDocument();
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
  });

  it('retries only the exact original scenario request after an unchanged authoritative read', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new TypeError('Response lost'));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Check scenario write' });
    const request = structuredClone(apiMock.updateSpec.mock.calls[0][1]);
    expect(screen.queryByRole('button', { name: 'Retry original write' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Check scenario write' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Retry original write' }));
    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(2));
    expect(apiMock.updateSpec.mock.calls[1]).toEqual([spec.id, request]);
  });

  it('retains an uncertain operation across read failure and refuses retry over a changed base', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new TypeError('Response lost'));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Check scenario write' });
    apiMock.getSpec.mockRejectedValueOnce(new Error('Read unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Check scenario write' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Read unavailable'));
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, version: spec.version + 1, scenario_snapshot: 'b'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Check scenario write' }));
    await screen.findByText(/The collection changed and the requested result could not be confirmed/);
    expect(screen.queryByRole('button', { name: 'Retry original write' })).not.toBeInTheDocument();
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
  });

  it('retains a method draft when another scenario refresh removes its subject', async () => {
    const a: TestScenario = { id: 'ts-a', title: 'Scenario A', given: 'G', when: 'W', then: 'Original observation', scenario_type: 'manual', status: 'draft', notes: null, linked_criteria: [], linked_task_ids: [] };
    const b: TestScenario = { ...a, id: 'ts-b', title: 'Scenario B' };
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, test_scenarios: [a, b] }).mockResolvedValue({ ...spec,
      version: spec.version + 1, scenario_snapshot: 'b'.repeat(64), test_scenarios: [{ ...b, verification_method: 'demonstration' }] });
    apiMock.updateScenarioVerificationMethod.mockResolvedValue({ scenario_id: b.id });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByText(a.title));
    fireEvent.change(screen.getByLabelText('Verification method ts-a'), { target: { value: 'inspection' } });
    fireEvent.click(screen.getByText(b.title));
    fireEvent.change(screen.getByLabelText('Verification method ts-b'), { target: { value: 'demonstration' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save verification method' }));
    await screen.findByText(/This scenario was removed. Your selected method is preserved/);
    expect(screen.getByLabelText('Verification method ts-a')).toHaveValue('inspection');
    expect(screen.getByLabelText('Verification method ts-a')).toBeDisabled();
    expect(apiMock.updateScenarioVerificationMethod).toHaveBeenCalledExactlyOnceWith(spec.board_id, spec.id, b.id, 'demonstration', spec.version);
    fireEvent.click(screen.getByRole('button', { name: 'Discard removed scenario draft' }));
    expect(screen.queryByText(a.title)).not.toBeInTheDocument();
  });

  it('reconciles a removal after a lost response without another write', async () => {
    const scenario: TestScenario = { id: 'ts-remove', title: 'Remove this scenario', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', status: 'draft', notes: null, linked_criteria: [], linked_task_ids: [] };
    apiMock.getSpec.mockResolvedValue({ ...spec, test_scenarios: [scenario] });
    apiMock.updateSpec.mockRejectedValueOnce(new TypeError('Response lost'));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete test scenario Remove this scenario' }));
    await screen.findByRole('button', { name: 'Check scenario write' });
    expect(screen.getByRole('button', { name: /Add Test Scenario/ })).toBeDisabled();
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, version: spec.version + 1, scenario_snapshot: 'b'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Check scenario write' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Check scenario write' })).not.toBeInTheDocument());
    expect(screen.queryByText(scenario.title)).not.toBeInTheDocument();
    expect(apiMock.updateSpec).toHaveBeenCalledTimes(1);
  });

  it('clears the scenario draft when the writer loses authority', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 403, message: 'Access denied' }));
    const onClose = vi.fn();
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={onClose} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.queryByPlaceholderText('Scenario title')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Check scenario write' })).not.toBeInTheDocument();
  });

  it.each(['link', 'unlink'] as const)('does not repeat a confirmed %s when its protected read fails', async kind => {
    const scenario: TestScenario = { id: 'ts-link', title: 'Link recovery scenario', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', status: 'draft', notes: null, linked_criteria: [], linked_task_ids: kind === 'unlink' ? ['card-link'] : [] };
    const initial = { ...spec, test_scenarios: [scenario], cards: [{ id: 'card-link', title: 'Selectable mock task', status: 'not_started', priority: 'none', assignee_id: null }] };
    apiMock.getSpec.mockResolvedValueOnce(initial).mockRejectedValueOnce(new Error('read offline')).mockResolvedValue(initial);
    apiMock.linkTaskToScenario.mockResolvedValue(undefined);
    apiMock.unlinkTaskFromScenario.mockResolvedValue(undefined);
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByText(scenario.title));
    if (kind === 'link') {
      fireEvent.click(screen.getByRole('button', { name: /Link task/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Selectable mock task' }));
    } else fireEvent.click(screen.getByTitle('Unlink task'));
    await screen.findByText(/change was saved, but its current state/);
    expect(screen.getByRole('button', { name: /Link task/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenario state' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reload scenario state' })).not.toBeInTheDocument());
    expect(kind === 'link' ? apiMock.linkTaskToScenario : apiMock.unlinkTaskFromScenario).toHaveBeenCalledExactlyOnceWith(spec.id, scenario.id, 'card-link');
  });

  it('surfaces a rejected coverage change without claiming it was saved', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'Coverage override denied' }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Skip test coverage requirement' }));
    await screen.findByText('Coverage override denied');
    expect(toast.success).not.toHaveBeenCalled();
    expect(apiMock.getSpec).toHaveBeenCalledTimes(1);
  });

  it('rejects a foreign scenario reload before updating the modal', async () => {
    apiMock.updateSpec.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'spec_version_conflict' }));
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    fireEvent.click(screen.getByRole('button', { name: 'Add Scenario' }));
    await screen.findByRole('button', { name: 'Reload scenarios' });
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, id: 'foreign', title: 'Foreign Spec content' });
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await screen.findByText('The Spec response does not match the requested identity.');
    expect(screen.queryByText('Foreign Spec content')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
  });

  it('subscribes Tests and preserves its dirty form on a same-version external snapshot change', async () => {
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    expect(scenarioWatch.options?.boardId).toBe(spec.board_id);
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, scenario_snapshot: 'b'.repeat(64) });
    await act(async () => scenarioWatch.options!.onChange({ subjects: [], authorityChanged: false, initial: false }));
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeEnabled();
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });

  it('preserves an external report draft when the watcher removes its scenario', async () => {
    const scenario: TestScenario = { id: 'ts-report', title: 'Report subject', given: 'G', when: 'W', then: 'T', scenario_type: 'manual', verification_method: 'inspection', status: 'ready', notes: null, linked_criteria: [], linked_task_ids: [] };
    apiMock.getAllowedTransitions.mockImplementation(async (boardId, query) => ({
      board_id: boardId, entity_type: query.entity_type, entity_id: query.entity_id,
      current_status: query.current_status ?? spec.status, source: 'core_sdlc_registry_v1', allowed_transitions: [],
    }));
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, test_scenarios: [scenario] });
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /^Tests/ }));
    fireEvent.click(screen.getByText(scenario.title));
    fireEvent.change(await screen.findByLabelText('Verification report ts-report'), { target: { value: '{"draft":"retain this observation"}' } });
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, version: spec.version + 1, scenario_snapshot: 'b'.repeat(64), test_scenarios: [] });
    await act(async () => scenarioWatch.options!.onChange({ subjects: [], authorityChanged: false, initial: false }));
    expect(screen.getByLabelText('Verification report ts-report')).toHaveValue('{"draft":"retain this observation"}');
    expect(screen.getByLabelText('Verification report ts-report')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Discard removed report draft' }));
    await waitFor(() => expect(screen.queryByText(scenario.title)).not.toBeInTheDocument());
  });

  it('removes Tests drafts when its revision subscription loses access', async () => {
    const onClose = vi.fn();
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={onClose} onChanged={vi.fn()} />);
    await openScenarioDraft();
    act(() => scenarioWatch.options!.onAccessLost());
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByPlaceholderText('Scenario title')).not.toBeInTheDocument();
  });

  it('recovers a failed watcher read without remounting the scenario draft', async () => {
    render(<SpecModal specId={spec.id} boardId={spec.board_id} onClose={vi.fn()} onChanged={vi.fn()} />);
    await openScenarioDraft();
    apiMock.getSpec.mockRejectedValueOnce(new Error('offline'));
    await act(async () => scenarioWatch.options!.onChange({ subjects: [], authorityChanged: false, initial: false }));
    expect(screen.getByRole('button', { name: 'Add Scenario' })).toBeDisabled();
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    apiMock.getSpec.mockResolvedValueOnce({ ...spec, scenario_snapshot: 'b'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Reload scenarios' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review scenario base' })).toBeEnabled());
    expect(screen.getByPlaceholderText('Scenario title')).toHaveValue('Preserve this authored scenario');
    expect(apiMock.updateSpec).not.toHaveBeenCalled();
  });
});
