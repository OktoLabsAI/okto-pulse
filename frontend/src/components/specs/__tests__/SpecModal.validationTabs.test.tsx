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
    ['draft', false],
    ['review', false],
    ['approved', true],
    ['validated', true],
    ['in_progress', true],
    ['done', true],
    ['cancelled', false],
  ] satisfies [SpecStatus, boolean][])(
    'shows the Validation tab for status %s: %s',
    async (status, visible) => {
      renderSpec(status);

      await screen.findByText(baseSpec.title);
      const validationTab = screen.queryByRole('button', {
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
      screen.getByRole('button', { name: 'Validation' }),
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

    expect(checklistTab).toHaveAttribute('aria-selected', 'true');
    expect(validationTab).toHaveAttribute('aria-selected', 'false');
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
      screen.queryByRole('button', { name: 'Validation' }),
    ).not.toBeInTheDocument();
  });

  it('shows only the validation history allowed by the preset', async () => {
    permissionMock.allowAll = false;
    permissionMock.allowed = new Set(['spec.validation.read']);
    renderSpec('approved');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('button', { name: 'Validation' }),
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
      screen.getByRole('button', { name: 'Validation' }),
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

  it('keeps the checklist history read-only after Approved', async () => {
    renderSpec('validated');

    await screen.findByText(baseSpec.title);
    fireEvent.click(
      screen.getByRole('button', { name: 'Validation' }),
    );

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
