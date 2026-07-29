import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Spec } from '@/types';

import { SpecModal } from '../SpecModal';

type QualityPanelProps = {
  subjectType: string;
  subjectId: string;
  subjectVersion: number;
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

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: null,
    isLoading: false,
    error: null,
    has: (permission: string) =>
      permissionMock.allowed.has(permission),
  }),
}));

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
    apiMock.listSprints.mockResolvedValue([]);
  });

  it.each([
    {
      permissions: [] as string[],
      visible: false,
      caseName: 'without quality read permission',
    },
    {
      permissions: ['spec.validation.read'],
      visible: false,
      caseName: 'with only the neighboring validation permission',
    },
    {
      permissions: ['spec.quality.read'],
      visible: true,
      caseName: 'with quality read permission',
    },
  ])(
    'controls visibility $caseName',
    async ({ permissions, visible }) => {
      permissionMock.allowed = new Set(permissions);
      renderSpec();

      await screen.findByText(baseSpec.title);
      expect(screen.queryByRole('tab', {
        name: 'Quality',
      })).not.toBeInTheDocument();
      const validationTab = screen.queryByRole('tab', {
        name: 'Validation',
      });

      if (!visible) {
        expect(validationTab).not.toBeInTheDocument();
        expect(
          screen.queryByTestId('spec-quality-panel'),
        ).not.toBeInTheDocument();
        return;
      }

      expect(validationTab).toBeInTheDocument();
      fireEvent.click(validationTab!);
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
        subjectStatus: status,
        subjectArchived: expectedArchived,
        canRead: true,
        canAssess: false,
        canProposeQuestions: false,
      });
      expect(props?.onAssessmentRecorded).toBe(onChanged);

      fireEvent.click(screen.getByTestId('spec-quality-panel'));
      expect(onChanged).toHaveBeenCalledTimes(1);
    },
  );
});
