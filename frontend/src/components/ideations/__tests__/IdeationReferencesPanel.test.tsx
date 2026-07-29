import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Ideation } from '@/types';
import { IdeationReferencesPanel } from '../IdeationReferencesPanel';

const modalStackMock = vi.hoisted(() => ({
  push: vi.fn(),
  pop: vi.fn(),
  clear: vi.fn(),
}));

vi.mock('@/contexts/ModalStackContext', () => ({
  useOptionalModalStack: () => ({
    stack: [],
    ...modalStackMock,
  }),
}));

function ideationWithReferences(): Ideation {
  return {
    id: 'ideation-1',
    board_id: 'board-1',
    title: 'Referenced ideation',
    description: 'Description',
    problem_statement: 'Problem',
    proposed_approach: 'Approach',
    scope_assessment: null,
    complexity: null,
    screen_mockups: [],
    architecture_designs: [],
    status: 'review',
    version: 1,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-07-28T12:00:00Z',
    updated_at: '2026-07-28T12:00:00Z',
    labels: [],
    knowledge_bases: [],
    qa_items: [],
    stories: [
      {
        id: 'story-1',
        board_id: 'board-1',
        topic_id: 'topic-1',
        title: 'Checkout story',
        description: 'A story referenced by this ideation',
        actor: 'Buyer',
        goal: 'Complete checkout',
        benefit: 'Buy a product',
        labels: [],
        status: 'ready',
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-07-28T12:00:00Z',
        updated_at: '2026-07-28T12:00:00Z',
        archived: false,
        pre_archive_status: null,
        screen_mockups: [],
        ideation_links: [],
      },
    ],
    refinements: [
      {
        id: 'refinement-1',
        ideation_id: 'ideation-1',
        board_id: 'board-1',
        title: 'Checkout refinement',
        description: 'Derived refinement',
        status: 'approved',
        version: 3,
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-07-28T12:00:00Z',
        updated_at: '2026-07-28T12:00:00Z',
        labels: [],
      },
    ],
    specs: [
      {
        id: 'spec-direct',
        board_id: 'board-1',
        ideation_id: 'ideation-1',
        refinement_id: null,
        title: 'Direct checkout spec',
        description: 'Derived directly from the ideation',
        status: 'approved',
        version: 2,
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-07-28T12:00:00Z',
        updated_at: '2026-07-28T12:00:00Z',
        labels: [],
      },
      {
        id: 'spec-via-refinement',
        board_id: 'board-1',
        ideation_id: 'ideation-1',
        refinement_id: 'refinement-1',
        title: 'Spec derived through refinement',
        description: 'Must not be duplicated in direct specs',
        status: 'draft',
        version: 1,
        assignee_id: null,
        created_by: 'agent-1',
        created_at: '2026-07-28T12:00:00Z',
        updated_at: '2026-07-28T12:00:00Z',
        labels: [],
      },
    ],
  };
}

describe('IdeationReferencesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('groups stories, refinements and direct specs without duplicating specs derived through refinements', () => {
    const ideation = ideationWithReferences();

    render(<IdeationReferencesPanel ideation={ideation} />);

    expect(within(screen.getByTestId('ideation-reference-stories')).getByText('Checkout story')).toBeInTheDocument();
    expect(within(screen.getByTestId('ideation-reference-refinements')).getByText('Checkout refinement')).toBeInTheDocument();
    const directSpecs = screen.getByTestId('ideation-reference-specs');
    expect(within(directSpecs).getByText('Direct checkout spec')).toBeInTheDocument();
    expect(within(directSpecs).queryByText('Spec derived through refinement')).not.toBeInTheDocument();
  });

  it('opens every reference through the shared modal stack', () => {
    render(<IdeationReferencesPanel ideation={ideationWithReferences()} />);

    fireEvent.click(screen.getByTestId('ideation-reference-story-story-1'));
    fireEvent.click(screen.getByTestId('ideation-reference-refinement-refinement-1'));
    fireEvent.click(screen.getByTestId('ideation-reference-spec-spec-direct'));

    expect(modalStackMock.push.mock.calls).toEqual([
      [{ type: 'story', id: 'story-1' }],
      [{ type: 'refinement', id: 'refinement-1' }],
      [{ type: 'spec', id: 'spec-direct' }],
    ]);
  });
});
