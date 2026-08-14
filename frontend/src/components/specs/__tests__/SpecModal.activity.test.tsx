import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecModal } from '../SpecModal';
import { persistTestScenariosWithWriteGuard } from '../scenarioWriteGuard';
import type { Spec, SpecHistoryEntry, TestScenario } from '@/types';

type ValidationGateOverrideProps = {
  title?: string;
  description?: string;
};

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  getEffectiveResources: vi.fn(),
  listSprints: vi.fn(),
  listSpecHistory: vi.fn(),
  listSpecKnowledge: vi.fn(),
  updateSpec: vi.fn(),
}));
const validationGateOverrideSpy = vi.hoisted(() => vi.fn());
const evidenceMatrixPropsSpy = vi.hoisted(() => vi.fn());

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: null, agents: [] }),
}));

vi.mock('@/hooks/usePermissions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePermissions')>();
  return {
    ...actual,
    usePermissions: () => ({
      preset: null,
      isLoading: false,
      error: null,
      has: () => true,
    }),
  };
});

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/code-traceability', () => ({
  useCodeTraceabilityAuthority: () => ({ canReadProjection: true }),
  EvidenceMatrixPanel: (props: {
    skipCoverage?: boolean;
    canEditCoverageFlags?: boolean;
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

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const spec: Spec = {
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
    apiMock.getSpec.mockResolvedValue(spec);
    apiMock.getAllowedTransitions.mockResolvedValue({ allowed_transitions: [] });
    apiMock.listSprints.mockResolvedValue([]);
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

  it('identifies the Details override as the Task Validation Gate for descendant cards', async () => {
    render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText(spec.title);
    const gate = screen.getByTestId('validation-gate-override');
    expect(gate).toHaveTextContent('Task Validation Gate');
    expect(gate).toHaveTextContent('cards derived from this spec');
    expect(gate).toHaveTextContent('do not change the Spec Validation Gate');
    expect(validationGateOverrideSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Task Validation Gate',
        description: expect.stringContaining('cards derived from this spec'),
      }),
    );
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
      'Code Evidence Matrix',
      'Tests',
      'Rules',
      'Dependencies',
      'Contracts',
      'IRs',
      'ORs',
      'TRs',
      'Decisions',
      'Resources',
      'Q&A',
      'References',
      'Sprints',
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

  it('persists the Draft-only Code Evidence coverage skip from its own tab', async () => {
    const updatedSpec = {
      ...spec,
      skip_code_evidence_coverage: true,
      version: spec.version + 1,
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
        skipCoverage: true,
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

  it('omits an unsupported legacy scenario type from the whole-list request', async () => {
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

    await waitFor(() => expect(apiMock.updateSpec).toHaveBeenCalledTimes(1));
    const request = apiMock.updateSpec.mock.calls[0][1];
    expect(request.test_scenarios).toHaveLength(1);
    const legacyRequest = request.test_scenarios.find(
      (scenario: { id: string }) => scenario.id === 'ts-legacy',
    );
    expect(legacyRequest).toMatchObject({
      id: 'ts-legacy',
      title: 'Historical regression type',
      status: 'draft',
    });
    expect(legacyRequest).not.toHaveProperty('scenario_type');
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
          [],
          [tampered],
        ),
      ).rejects.toThrow(/Invalid scenario_type undefined for new scenario/);
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
  });
});
