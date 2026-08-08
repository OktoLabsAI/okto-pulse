import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Refinement } from '@/types';
import { RefinementResourcesPanel } from '../RefinementResourcesPanel';

const apiMock = vi.hoisted(() => ({
  updateRefinement: vi.fn(),
  createRefinementKnowledge: vi.fn(),
  deleteRefinementKnowledge: vi.fn(),
  getRefinementKnowledge: vi.fn(),
}));
const resourceGateMock = vi.hoisted(() => vi.fn());

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));
vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div data-testid="architecture-resource" />,
}));
vi.mock('@/components/resources/KnowledgeWorkspace', () => ({
  KnowledgeWorkspace: () => <div data-testid="knowledge-resource" />,
}));
vi.mock('@/components/resources/ResourceGateSummary', () => ({
  ResourceGateSummary: (props: unknown) => {
    resourceGateMock(props);
    return <div data-testid="resource-gate-summary" />;
  },
}));
vi.mock('@/components/specs/MockupsTab', () => ({
  MockupsTab: () => <div data-testid="mockups-resource" />,
}));
vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const refinement: Refinement = {
  id: 'refinement-1',
  ideation_id: 'ideation-1',
  board_id: 'board-1',
  title: 'Refinement resources',
  description: null,
  in_scope: [],
  out_of_scope: [],
  analysis: null,
  decisions: [],
  screen_mockups: [],
  architecture_designs: [],
  status: 'review',
  version: 2,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-07-27T10:00:00Z',
  updated_at: '2026-07-27T10:00:00Z',
  labels: [],
  specs: [],
  qa_items: [],
  knowledge_bases: [
    {
      id: 'knowledge-1',
      refinement_id: 'refinement-1',
      title: 'Knowledge',
      description: null,
      mime_type: 'text/markdown',
      created_at: '2026-07-27T10:00:00Z',
    },
  ],
};

describe('RefinementResourcesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('groups Mockups, Knowledge and Architecture with a compact collapsible gate', () => {
    render(
      <RefinementResourcesPanel
        refinement={refinement}
        fallbackBoardId="fallback-board"
        expanded={false}
        onRefinementChanged={vi.fn()}
        onArchitectureChanged={vi.fn()}
        onKnowledgeCreated={vi.fn()}
        onKnowledgeDeleted={vi.fn()}
      />,
    );

    expect(screen.getByTestId('mockups-resource')).toBeInTheDocument();
    expect(screen.queryByTestId('resource-gate-summary')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('resource-gate-disclosure-toggle'));
    expect(screen.getByTestId('resource-gate-summary')).toBeInTheDocument();
    expect(resourceGateMock).toHaveBeenCalledWith(
      expect.objectContaining({
        boardId: 'board-1',
        entityType: 'refinement',
        entityId: 'refinement-1',
        compact: true,
      }),
    );

    fireEvent.click(screen.getByRole('tab', { name: /Knowledge/ }));
    expect(screen.getByTestId('knowledge-resource')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /Architecture/ }));
    expect(screen.getByTestId('architecture-resource')).toBeInTheDocument();
  });
});
