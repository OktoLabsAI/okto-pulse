import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Spec } from '@/types';

import { SpecModal } from '../SpecModal';

type QualityPanelProps = {
  subjectType: string;
  subjectId: string;
  subjectVersion: number;
  subjectEdition: number;
  subjectStatus: string;
  subjectArchived: boolean;
  canRead: boolean;
  canAssess: boolean;
  canProposeQuestions: boolean;
  onAssessmentRecorded?: () => void;
};

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  getCurrentSpecValidation: vi.fn(),
  getValidationCycle: vi.fn(),
  getValidationTechnicalAudit: vi.fn(),
  listSprints: vi.fn(),
}));
const permissionMock = vi.hoisted(() => ({
  allowed: new Set<string>(),
}));
const qualityPanelSpy = vi.hoisted(() => vi.fn());

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({
    id: 'board-1',
    owner_id: null,
    agents: [],
    settings: { require_spec_validation: true },
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
      has: (permission: string) =>
        permissionMock.allowed.has(permission),
    }),
  };
});

vi.mock('@/components/quality', () => ({
  QualityPanel: (props: QualityPanelProps) => {
    qualityPanelSpy(props);
    return (
      <button
        type="button"
        data-testid="spec-quality-panel"
        onClick={props.onAssessmentRecorded}
      >
        Spec quality panel
      </button>
    );
  },
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

vi.mock('@/components/shared/ValidationGateOverride', () => ({
  ValidationGateOverride: () => <div />,
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: () => <div />,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseSpec: Spec = {
  id: 'spec-quality-tab',
  board_id: 'board-1',
  ideation_id: null,
  refinement_id: null,
  title: 'Spec quality integration',
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
  status: 'review',
  edition: 1,
  version: 9,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-07-28T10:00:00Z',
  updated_at: '2026-07-28T10:00:00Z',
  labels: [],
  cards: [],
  knowledge_bases: [],
  qa_items: [],
};

function renderSpec(
  overrides: Partial<Spec> = {},
  onChanged = vi.fn(),
) {
  const spec = { ...baseSpec, ...overrides };
  apiMock.getSpec.mockResolvedValue(spec);

  return {
    ...render(
      <SpecModal
        specId={spec.id}
        boardId={spec.board_id}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    ),
    onChanged,
    spec,
  };
}

describe('SpecModal Requirement lint in Validation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionMock.allowed = new Set();
    apiMock.getAllowedTransitions.mockResolvedValue({
      allowed_transitions: [],
    });
    apiMock.getValidationCycle.mockResolvedValue({
      subject_type: 'spec',
      subject_id: baseSpec.id,
      edition: 1,
      subject_status: 'review',
      visible_sections: ['spec_validation', 'requirement_lint'],
      cycle_state: 'pending',
      current_result: null,
      previous_result_count: 0,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 1,
        expected_subject_version: 9,
        expected_head_revision: 0,
      },
      checks: [
        { result_type: 'requirement_lint', status: 'not_started', summary: 'Not started' },
      ],
      remaining_actions: [],
    });
    apiMock.getValidationTechnicalAudit.mockResolvedValue(null);
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: baseSpec.id,
      edition: 1,
      result: null,
    });
    apiMock.listSprints.mockResolvedValue([]);
  });

  it.each([
    {
      permissions: [] as string[],
      validationVisible: false,
      lintVisible: false,
      caseName: 'without quality read permission',
    },
    {
      permissions: ['spec.validation.read'],
      validationVisible: true,
      lintVisible: false,
      caseName: 'with only the neighboring validation permission',
    },
    {
      permissions: ['spec.quality.read'],
      validationVisible: true,
      lintVisible: true,
      caseName: 'with quality read permission',
    },
  ])(
    'controls visibility $caseName',
    async ({ permissions, validationVisible, lintVisible }) => {
      permissionMock.allowed = new Set(permissions);
      renderSpec();

      await screen.findByText(baseSpec.title);
      expect(screen.queryByRole('tab', {
        name: 'Quality',
      })).not.toBeInTheDocument();
      const validationTab = screen.queryByRole('tab', {
        name: 'Validation',
      });

      if (!validationVisible) {
        expect(validationTab).not.toBeInTheDocument();
        expect(
          screen.queryByTestId('spec-quality-panel'),
        ).not.toBeInTheDocument();
        return;
      }

      expect(validationTab).toBeInTheDocument();
      fireEvent.click(validationTab!);
      if (!lintVisible) {
        expect(
          screen.queryByRole('tab', { name: /Requirement lint/ }),
        ).not.toBeInTheDocument();
        expect(
          screen.queryByTestId('spec-quality-panel'),
        ).not.toBeInTheDocument();
        return;
      }
      fireEvent.click(
        screen.getByRole('tab', { name: /Requirement lint/ }),
      );
      expect(
        screen.getByTestId('spec-quality-panel'),
      ).toBeInTheDocument();
    },
  );

  it.each([
    {
      status: 'review' as const,
      version: 9,
      archived: undefined,
      expectedArchived: false,
    },
    {
      status: 'done' as const,
      version: 12,
      archived: true,
      expectedArchived: true,
    },
  ])(
    'forwards the spec lifecycle in $status',
    async ({ status, version, archived, expectedArchived }) => {
      permissionMock.allowed = new Set(['spec.quality.read']);
      const onChanged = vi.fn();
      const { spec } = renderSpec(
        { status, version, archived },
        onChanged,
      );

      await screen.findByText(spec.title);
      fireEvent.click(
        screen.getByRole('tab', { name: 'Validation' }),
      );
      fireEvent.click(
        screen.getByRole('tab', { name: /Requirement lint/ }),
      );

      const props = qualityPanelSpy.mock.calls.at(-1)?.[0] as
        | QualityPanelProps
        | undefined;
      expect(props).toMatchObject({
        subjectType: 'spec',
        subjectId: spec.id,
        subjectVersion: version,
        subjectEdition: 1,
        subjectStatus: status,
        subjectArchived: expectedArchived,
        canRead: true,
        canAssess: false,
        canProposeQuestions: false,
      });

      fireEvent.click(screen.getByTestId('spec-quality-panel'));
      expect(onChanged).toHaveBeenCalledTimes(1);
    },
  );
});
