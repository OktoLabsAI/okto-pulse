import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecModal } from '../SpecModal';
import type { Spec, SpecHistoryEntry } from '@/types';

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  listSprints: vi.fn(),
  listSpecHistory: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: null, agents: [] }),
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: null,
    isLoading: false,
    error: null,
    has: () => true,
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
  status: 'draft',
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
    fireEvent.click(screen.getByRole('button', { name: 'Activity' }));

    await waitFor(() => expect(apiMock.listSpecHistory).toHaveBeenCalledWith(spec.id));
    const actionBadge = await screen.findByText('Updated');
    expect(screen.getByText(historyEntry.summary!)).toBeInTheDocument();
    expect(screen.getByText(historyEntry.actor_name)).toBeInTheDocument();
    expect(screen.getByText('v12')).toBeInTheDocument();

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
});
