import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModalStackProvider } from '@/contexts/ModalStackContext';
import type { SpecSummary } from '@/types';
import { RefinementEvidenceMatrixNavigation } from '@/components/refinements/RefinementModal';
import { ModalStackRenderer } from '../ModalStackRenderer';

const specModalSpy = vi.hoisted(() => vi.fn());

vi.mock('@/components/specs/SpecModal', () => ({
  SpecModal: (props: Record<string, unknown>) => {
    specModalSpy(props);
    return <div data-testid="stacked-spec-modal" />;
  },
}));
vi.mock('@/components/kanban/CardModal', () => ({ CardModal: () => null }));
vi.mock('@/components/stories', () => ({ StoryModal: () => null }));
vi.mock('@/components/ideations/IdeationModal', () => ({ IdeationModal: () => null }));
vi.mock('@/components/refinements/RefinementModal', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/refinements/RefinementModal')>();
  return { ...actual, RefinementModal: () => null };
});
vi.mock('@/components/sprints/SprintModal', () => ({ SprintModal: () => null }));
vi.mock('@/components/knowledge/NodeDetailModal', () => ({ NodeDetailModal: () => null }));
vi.mock('@/services/api', () => ({
  useDashboardApi: () => ({ listTopics: vi.fn().mockResolvedValue([]) }),
}));
vi.mock('@/store/dashboard', () => ({
  useDashboardStore: (selector: (state: Record<string, unknown>) => unknown) => selector({
    openCardModal: vi.fn(),
    closeCardModal: vi.fn(),
  }),
}));

function spec(id: string, title: string): SpecSummary {
  return {
    id,
    board_id: 'board-1',
    ideation_id: null,
    refinement_id: 'refinement-1',
    title,
    description: null,
    status: 'draft',
    edition: 1,
    version: 1,
    assignee_id: null,
    created_by: 'user-1',
    created_at: '2026-08-22T12:00:00Z',
    updated_at: '2026-08-22T12:00:00Z',
    labels: [],
  };
}

function activateFocusedButtonWithEnter(button: HTMLButtonElement) {
  button.focus();
  expect(button).toHaveFocus();
  fireEvent.keyDown(button, { key: 'Enter', code: 'Enter' });
  // jsdom does not execute the native Enter default action, so dispatch the
  // corresponding click through Testing Library's React-aware event wrapper.
  fireEvent.click(button);
  fireEvent.keyUp(button, { key: 'Enter', code: 'Enter' });
}

describe('ModalStackRenderer Spec tab routing', () => {
  it('ts_e352303b — routes the real single-Spec keyboard action to its Evidence Matrix tab', () => {
    specModalSpy.mockClear();
    render(
      <ModalStackProvider>
        <RefinementEvidenceMatrixNavigation
          specs={[spec('spec-checkout', 'Checkout contract')]}
        />
        <ModalStackRenderer boardId="board-1" />
      </ModalStackProvider>,
    );

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    activateFocusedButtonWithEnter(screen.getByRole('button', {
      name: 'Open Code Evidence Matrix for Checkout contract',
    }));

    expect(screen.getByTestId('stacked-spec-modal')).toBeInTheDocument();
    expect(specModalSpy).toHaveBeenLastCalledWith(expect.objectContaining({
      specId: 'spec-checkout',
      boardId: 'board-1',
      initialTab: 'evidence-matrix',
    }));
  });
});
