import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Spec, SpecStatus } from '@/types';

import { SpecModal } from '../SpecModal';

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  listSprints: vi.fn(),
}));
const permissionMock = vi.hoisted(() => ({
  allowAll: true,
  allowed: new Set<string>(),
}));

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
      permissionMock.allowAll ||
      permissionMock.allowed.has(permission),
  }),
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

vi.mock('@/components/quality', () => ({
  QualityPanel: () => <div data-testid="requirement-lint-panel" />,
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

vi.mock('@/components/shared/ValidationGateOverride', () => ({
  ValidationGateOverride: () => <div />,
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: () => <div />,
}));

vi.mock('../SpecChecklistPanel', () => ({
  SpecChecklistPanel: ({
    canExecute,
    showHistory,
  }: {
    canExecute: boolean;
    showHistory: boolean;
  }) => (
    <div
      data-testid="checklist-panel"
      data-can-execute={String(canExecute)}
      data-show-history={String(showHistory)}
    />
  ),
}));

vi.mock('../SpecValidationHistoryPanel', () => ({
  SpecValidationHistoryPanel: () => (
    <div data-testid="spec-validation-history" />
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseSpec: Spec = {
  id: 'spec-validation-tabs',
  board_id: 'board-1',
  ideation_id: null,
  refinement_id: null,
  title: 'Validation navigation spec',
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
  status: 'draft',
  edition: 1,
  version: 4,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-07-28T10:00:00Z',
  updated_at: '2026-07-28T10:00:00Z',
  labels: [],
  cards: [],
  knowledge_bases: [],
  qa_items: [],
};

function renderSpec(status: SpecStatus) {
  apiMock.getSpec.mockResolvedValue({ ...baseSpec, status });

  return render(
    <SpecModal
      specId={baseSpec.id}
      boardId={baseSpec.board_id}
      onClose={vi.fn()}
      onChanged={vi.fn()}
    />,
  );
}

function blockedPolicyDecision() {
  return {
    state: 'policy_compliance_receipt_stale',
    allowed: false,
    policy_compliance_required: true,
    reason_codes: ['policy_compliance_receipt_stale'],
    decision_digest: 'a'.repeat(64),
    fence_digest: 'b'.repeat(64),
    receipt_id: 'receipt-stale',
    currentness: 'stale',
    currentness_reasons: ['subject_content_changed'],
    applicable_rule_count: 2,
    applicable_blocking_rule_count: 1,
    blocking_rule_count: 0,
    waived_rule_count: 0,
    advisory_issue_count: 0,
  };
}

describe('SpecModal validation navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionMock.allowAll = true;
    permissionMock.allowed = new Set();
    apiMock.getAllowedTransitions.mockResolvedValue({
      allowed_transitions: [],
    });
    apiMock.listSprints.mockResolvedValue([]);
  });

  it.each([
    ['draft', true],
    ['review', true],
    ['approved', true],
    ['validated', true],
    ['in_progress', true],
    ['done', true],
    ['cancelled', true],
  ] satisfies [SpecStatus, boolean][])(
    'shows the Validation tab for status %s: %s',
    async (status, visible) => {
      renderSpec(status);

      await screen.findByText(baseSpec.title);
      const validationTab = screen.queryByRole('tab', {
        name: 'Validation',
      });

      if (visible) {
        expect(validationTab).toBeInTheDocument();
      } else {
        expect(validationTab).not.toBeInTheDocument();
      }
    },
  );

  it('separates Checklist and Spec Validation with the checklist first', async () => {
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const tabList = screen.getByRole('tablist', {
      name: 'Spec validation sections',
    });
    const checklistTab = within(tabList).getByRole('tab', {
      name: 'Checklist',
    });
    const validationTab = within(tabList).getByRole('tab', {
      name: 'Spec Validation',
    });
    const lintTab = within(tabList).getByRole('tab', {
      name: /Requirement lint/,
    });
    const policyTab = within(tabList).getByRole('tab', {
      name: 'Policy Compliance',
    });

    expect(checklistTab).toHaveAttribute('aria-selected', 'true');
    expect(validationTab).toHaveAttribute('aria-selected', 'false');
    expect(lintTab).toHaveAttribute('aria-selected', 'false');
    expect(policyTab).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-can-execute',
      'true',
    );
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-show-history',
      'true',
    );
    expect(
      screen.queryByTestId('spec-validation-history'),
    ).not.toBeInTheDocument();

    fireEvent.click(validationTab);

    expect(checklistTab).toHaveAttribute('aria-selected', 'false');
    expect(validationTab).toHaveAttribute('aria-selected', 'true');
    expect(
      screen.queryByTestId('checklist-panel'),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId('spec-validation-history'),
    ).toBeInTheDocument();
  });

  it('hides Validation when neither subtab can be read', async () => {
    permissionMock.allowAll = false;
    renderSpec('approved');

    await screen.findByText(baseSpec.title);

    expect(
      screen.queryByRole('tab', { name: 'Validation' }),
    ).not.toBeInTheDocument();
  });

  it('keeps Validation reachable for a policy-only actor in Draft', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set([
      'guidelines.assessments.read',
    ]);
    renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const tabList = screen.getByRole('tablist', {
      name: 'Spec validation sections',
    });
    expect(
      within(tabList).getByRole('tab', {
        name: 'Policy Compliance',
      }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(
      within(tabList).queryByRole('tab', {
        name: /Requirement lint/,
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-board-id',
      'board-1',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-entity-type',
      'spec',
    );
    expect(screen.getByTestId('policy-compliance-panel')).toHaveAttribute(
      'data-subject-id',
      baseSpec.id,
    );
    expect(
      screen.getByTestId('policy-transition-preview'),
    ).toBeInTheDocument();
  });

  it('falls back from an active Policy tab when its permission is revoked', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set([
      'guidelines.assessments.read',
      'spec.quality.read',
    ]);
    const rendered = renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    fireEvent.click(
      screen.getByRole('tab', { name: 'Policy Compliance' }),
    );
    expect(screen.getByTestId('policy-compliance-panel'))
      .toBeInTheDocument();

    permissionMock.allowed = new Set(['spec.quality.read']);
    rendered.rerender(
      <SpecModal
        specId={baseSpec.id}
        boardId={baseSpec.board_id}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole('tab', { name: 'Policy Compliance' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: /Requirement lint/ }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('requirement-lint-panel'))
      .toBeInTheDocument();
  });

  it('withholds every transition action while authority loading fails', async () => {
    apiMock.getAllowedTransitions.mockRejectedValue(
      new Error('transition authority unavailable'),
    );

    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    expect(
      screen.queryByRole('button', { name: 'Validate' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Draft' }),
    ).not.toBeInTheDocument();
  });

  it('withholds Validate on a blocked policy edge while preserving recovery', async () => {
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'spec',
      entity_id: baseSpec.id,
      current_status: 'approved',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        {
          to_status: 'validated',
          label: 'Validated',
          gate: 'spec_validation',
          policy_compliance: true,
          policy_compliance_decision: blockedPolicyDecision(),
        },
        {
          to_status: 'draft',
          label: 'Draft',
          gate: 'reopen',
          policy_compliance: false,
          policy_compliance_decision: null,
        },
      ],
    });
    renderSpec('approved');

    await screen.findByText(baseSpec.title);

    expect(
      screen.queryByRole('button', { name: 'Validate' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Draft' }),
    ).toBeInTheDocument();
  });

  it('shows only the validation history allowed by the preset', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.validation.read']);
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const tabList = screen.getByRole('tablist', {
      name: 'Spec validation sections',
    });
    expect(
      within(tabList).queryByRole('tab', { name: 'Checklist' }),
    ).not.toBeInTheDocument();
    expect(
      within(tabList).getByRole('tab', { name: 'Spec Validation' }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(
      screen.getByTestId('spec-validation-history'),
    ).toBeInTheDocument();
  });

  it('shows only the checklist allowed by the preset', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.checklist.read']);
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const tabList = screen.getByRole('tablist', {
      name: 'Spec validation sections',
    });
    expect(
      within(tabList).getByRole('tab', { name: 'Checklist' }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(
      within(tabList).queryByRole('tab', {
        name: 'Spec Validation',
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('checklist-panel')).toBeInTheDocument();
  });

  it('shows only Requirement lint before Approved when quality can be read', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.quality.read']);
    renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const tabList = screen.getByRole('tablist', {
      name: 'Spec validation sections',
    });
    expect(
      within(tabList).getByRole('tab', { name: /Requirement lint/ }),
    ).toHaveAttribute('aria-selected', 'true');
    expect(
      within(tabList).queryByRole('tab', { name: 'Checklist' }),
    ).not.toBeInTheDocument();
    expect(
      within(tabList).queryByRole('tab', { name: 'Spec Validation' }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('requirement-lint-panel')).toBeInTheDocument();
  });

  it('keeps the checklist history read-only after Approved', async () => {
    renderSpec('validated');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    const checklistTab = screen.getByRole('tab', { name: 'Checklist' });
    fireEvent.click(checklistTab);

    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-can-execute',
      'false',
    );
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-show-history',
      'true',
    );
  });
});
