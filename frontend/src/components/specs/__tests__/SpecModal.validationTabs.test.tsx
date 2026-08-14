import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Spec, SpecStatus } from '@/types';

import { SpecModal } from '../SpecModal';

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  getValidationCycle: vi.fn(),
  getValidationTechnicalAudit: vi.fn(),
  getSpecChecklistState: vi.fn(),
  getCurrentSpecValidation: vi.fn(),
  listSprints: vi.fn(),
}));
const permissionMock = vi.hoisted(() => ({
  allowAll: true,
  allowed: new Set<string>(),
}));
const boardSettingsMock = vi.hoisted(() => ({
  requireSpecValidation: true,
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
      require_spec_validation:
        boardSettingsMock.requireSpecValidation,
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
      has: (permission: string) =>
        permissionMock.allowAll ||
        permissionMock.allowed.has(permission),
    }),
  };
});

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
    blocked_reason?: string | null;
    policy_compliance?: boolean;
    policy_compliance_decision?: { allowed?: boolean } | null;
  }) => (
    transition.blocked_reason == null
    && (
      transition.policy_compliance === false
      || (
        transition.policy_compliance === true
        && transition.policy_compliance_decision?.allowed === true
      )
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
  projectPolicyTransitions: (transitions: Array<{
    to_status: string;
    label: string;
    gate: string;
    policy_compliance: boolean;
    policy_compliance_decision: { allowed: boolean | null } | null;
  }>) => ({
    governed: transitions
      .filter((transition) => transition.policy_compliance)
      .map((transition) => ({
        toStatus: transition.to_status,
        label: transition.label,
        gate: transition.gate,
        blockedReason: null,
        decision: transition.policy_compliance_decision,
      })),
    ungoverned: transitions.filter((transition) => !transition.policy_compliance),
  }),
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
    validationStageActive,
  }: {
    canExecute: boolean;
    showHistory: boolean;
    validationStageActive: boolean;
  }) => (
    <div
      data-testid="checklist-panel"
      data-can-execute={String(canExecute)}
      data-show-history={String(showHistory)}
      data-validation-stage-active={String(validationStageActive)}
    />
  ),
}));

vi.mock('../SpecValidationHistoryPanel', () => ({
  SpecValidationHistoryPanel: ({ view }: { view: 'current' | 'previous' }) => (
    <div data-testid="spec-validation-history" data-view={view} />
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
  skip_code_evidence_coverage: false,
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
    boardSettingsMock.requireSpecValidation = true;
    apiMock.getAllowedTransitions.mockResolvedValue({
      allowed_transitions: [],
    });
    apiMock.getSpecChecklistState.mockResolvedValue({
      status: 'not_started',
      subject: { spec_edition: 1 },
      binding: { mode: 'blocking' },
      current_receipt: null,
      gate: { allowed: false },
    });
    apiMock.getValidationCycle.mockResolvedValue({
      subject_type: 'spec',
      subject_id: baseSpec.id,
      edition: 1,
      subject_status: 'draft',
      visible_sections: [
        'spec_validation',
        'requirement_lint',
        'curated_checklist',
        'policy_compliance',
      ],
      cycle_state: 'pending',
      current_result: null,
      previous_result_count: 0,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 1,
        expected_subject_version: 4,
        expected_head_revision: 0,
      },
      checks: [
        { result_type: 'curated_checklist', status: 'not_started', summary: 'Not started' },
        { result_type: 'requirement_lint', status: 'not_started', summary: 'Not started' },
        { result_type: 'policy_compliance', status: 'not_started', summary: 'Not started' },
      ],
      remaining_actions: [],
    });
    apiMock.getValidationTechnicalAudit.mockResolvedValue(null);
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: baseSpec.id,
      edition: 1,
      lifecycle_state: 'pending',
      current_validation: null,
      previous_count: 0,
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

  it('presents four ordered, lazy validation subtabs', async () => {
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    expect(screen.getByTestId('spec-validation-workspace')).toBeInTheDocument();
    expect(screen.getByTestId('spec-validation-current')).toBeInTheDocument();
    expect(within(screen.getByRole('tablist', {
      name: 'Spec validation sections',
    })).getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      'Spec Validation',
      'Checklist',
      'Requirement lint',
      'Policy Compliance',
    ]);
    expect(screen.getByTestId('spec-validation-previous-toggle')).toBeInTheDocument();
    expect(screen.queryByTestId('checklist-panel')).not.toBeInTheDocument();
    expect(screen.getByTestId('spec-validation-history')).toHaveAttribute(
      'data-view',
      'current',
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Checklist' }));
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-validation-stage-active',
      'true',
    );
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-show-history',
      'true',
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Spec Validation' }));
    expect(screen.getByTestId('spec-validation-history')).toHaveAttribute(
      'data-view',
      'current',
    );
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

    expect(screen.getByRole('tab', { name: 'Policy Compliance' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.queryByRole('tab', { name: 'Requirement lint' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-current')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-previous-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-technical-audit-toggle'))
      .not.toBeInTheDocument();
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
    expect(screen.queryByTestId('policy-transition-preview')).not.toBeInTheDocument();
  });

  it('removes Policy details when its permission is revoked without hiding lint', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set([
      'guidelines.assessments.read',
      'spec.quality.read',
    ]);
    const rendered = renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(screen.getByRole('tab', { name: 'Validation' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Policy Compliance' }));
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
    expect(screen.getByRole('tab', { name: 'Requirement lint' })).toBeInTheDocument();
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

  it('offers the validation form for the canonical Spec Validation gate blocker', async () => {
    boardSettingsMock.requireSpecValidation = false;
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
          blocked_reason: (
            'spec_validation_required: submit the Spec Validation Gate; '
            + 'direct approved→validated moves are not admitted.'
          ),
          blocked_facts: null,
          preconditions: ['spec_validation_ready'],
          capabilities: ['validate'],
          effects: ['status_changed'],
          reason_codes: [
            'spec_validation_required',
            'spec_checklist_gate_required',
            'transition_not_allowed',
          ],
          policy_compliance: true,
          policy_compliance_decision: {
            ...blockedPolicyDecision(),
            state: 'policy_compliance_ready',
            allowed: true,
            reason_codes: ['policy_compliance_ready'],
            currentness: 'current',
          },
        },
      ],
    });
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      await screen.findByRole('button', { name: 'Validate' }),
    );

    expect(
      await screen.findByRole('heading', { name: 'Validate Spec' }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(apiMock.getSpecChecklistState).toHaveBeenCalledWith(
        'board-1',
        baseSpec.id,
      );
      expect(apiMock.getValidationCycle).toHaveBeenCalledWith(
        'spec',
        baseSpec.id,
        { includePrevious: false },
      );
    });
  });

  it('does not offer validation submission for another blocker on the same edge', async () => {
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
          blocked_reason: 'test_coverage_required: link every AC to a scenario.',
          blocked_facts: null,
          preconditions: ['spec_validation_ready'],
          capabilities: ['validate'],
          effects: ['status_changed'],
          reason_codes: [
            'spec_validation_required',
            'spec_checklist_gate_required',
            'transition_not_allowed',
          ],
          policy_compliance: true,
          policy_compliance_decision: {
            ...blockedPolicyDecision(),
            state: 'policy_compliance_ready',
            allowed: true,
            reason_codes: ['policy_compliance_ready'],
            currentness: 'current',
          },
        },
      ],
    });
    renderSpec('approved');

    await screen.findByText(baseSpec.title);

    expect(
      screen.queryByRole('button', { name: 'Validate' }),
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

  it('keeps validation history readable in Draft for a validation-only preset', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.validation.read']);
    renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    expect(screen.queryByRole('tab', { name: 'Checklist' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Requirement lint' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Policy Compliance' })).not.toBeInTheDocument();
    expect(screen.getByTestId('spec-validation-current')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('spec-validation-previous-toggle'));
    expect(screen.getAllByTestId('spec-validation-history').find(
      (element) => element.getAttribute('data-view') === 'previous',
    )).toHaveAttribute(
      'data-view',
      'previous',
    );
  });

  it('keeps checklist history readable in Draft without exposing validation results', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.checklist.read']);
    renderSpec('draft');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    expect(screen.getByRole('tab', { name: 'Checklist' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.queryByTestId('spec-validation-current')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-previous-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-technical-audit-toggle'))
      .not.toBeInTheDocument();
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

    expect(screen.getByRole('tab', { name: 'Requirement lint' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.queryByRole('tab', { name: 'Checklist' })).not.toBeInTheDocument();
    expect(screen.getByTestId('requirement-lint-panel')).toBeInTheDocument();
  });

  it('keeps the checklist history read-only after Approved', async () => {
    renderSpec('validated');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('tab', { name: 'Validation' }),
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Checklist' }));

    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-can-execute',
      'true',
    );
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-validation-stage-active',
      'false',
    );
    expect(screen.getByTestId('checklist-panel')).toHaveAttribute(
      'data-show-history',
      'true',
    );
  });
});
