import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { SpecSummary } from '@/types';
import {
  RefinementCodeEvidenceTabContent,
  RefinementEvidenceMatrixNavigation,
} from '../RefinementModal';

vi.mock('@/components/code-traceability', () => ({
  CodeEvidencePanel: () => <section data-testid="mock-source-context">Source context</section>,
  useCodeTraceabilityAuthority: () => ({ canReadProjection: true }),
}));

function spec(id: string, title: string, edition = 1): SpecSummary {
  return {
    id,
    board_id: 'board-1',
    ideation_id: null,
    refinement_id: 'refinement-1',
    title,
    description: null,
    status: 'draft',
    edition,
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
  // jsdom does not execute the native button default action for key events.
  // `.click()` models the browser activation that follows an uncancelled Enter.
  button.click();
  fireEvent.keyUp(button, { key: 'Enter', code: 'Enter' });
}

describe('RefinementEvidenceMatrixNavigation', () => {
  it('UT-UI-14 keeps Source Context before the matrix next action in DOM and visual order', () => {
    render(
      <RefinementCodeEvidenceTabContent
        boardId="board-1"
        refinementId="refinement-1"
        refinementVersion={3}
        specs={[spec('spec-one', 'Checkout contract')]}
      />,
    );

    const sourceContext = screen.getByTestId('mock-source-context');
    const nextAction = screen.getByTestId('refinement-evidence-matrix-navigation');
    expect(
      sourceContext.compareDocumentPosition(nextAction)
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByTestId('refinement-code-evidence-tab-content')).toHaveClass('space-y-4');
  });

  it('ts_4991bedc — explains the zero-accessible-Spec variant without an arbitrary action', () => {
    const onOpenSpec = vi.fn();

    render(
      <RefinementEvidenceMatrixNavigation specs={[]} onOpenSpec={onOpenSpec} />,
    );

    expect(screen.getByText('No derived Spec yet')).toBeInTheDocument();
    expect(screen.getByText(/matrix becomes available after a Spec is derived/i))
      .toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /open matrix/i })).not.toBeInTheDocument();
    expect(onOpenSpec).not.toHaveBeenCalled();
  });

  it('ts_e352303b — activates the only accessible Spec directly by keyboard without a selector', () => {
    const onOpenSpec = vi.fn();

    render(
      <RefinementEvidenceMatrixNavigation
        specs={[spec('spec-one', 'Checkout contract')]}
        onOpenSpec={onOpenSpec}
      />,
    );

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    const openButton = screen.getByRole<HTMLButtonElement>('button', {
      name: 'Open Code Evidence Matrix for Checkout contract',
    });
    expect(onOpenSpec).not.toHaveBeenCalled();

    activateFocusedButtonWithEnter(openButton);

    expect(openButton).toHaveFocus();
    expect(onOpenSpec).toHaveBeenCalledTimes(1);
    expect(onOpenSpec).toHaveBeenCalledWith('spec-one');
  });

  it('ts_4991bedc — requires keyboard-confirmed selection among provided accessible Specs', () => {
    const onOpenSpec = vi.fn();
    const accessibleSpecs = [
      spec('spec-zeta', 'Zeta worker', 3),
      spec('spec-alpha', 'Alpha API', 2),
    ];

    render(
      <RefinementEvidenceMatrixNavigation
        specs={accessibleSpecs}
        onOpenSpec={onOpenSpec}
      />,
    );

    const openButton = screen.getByRole<HTMLButtonElement>('button', { name: 'Open matrix' });
    const specSelector = screen.getByRole('combobox', {
      name: 'Choose a Spec for its Code Evidence Matrix',
    }) as HTMLSelectElement;
    expect(openButton).toBeDisabled();
    expect(specSelector).toHaveValue('');
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Select a Spec…',
      'Alpha API · edition 2',
      'Zeta worker · edition 3',
    ]);
    fireEvent.keyDown(openButton, { key: 'Enter', code: 'Enter' });
    openButton.click();
    fireEvent.keyUp(openButton, { key: 'Enter', code: 'Enter' });
    expect(onOpenSpec).not.toHaveBeenCalled();

    specSelector.focus();
    fireEvent.keyDown(specSelector, { key: 'ArrowDown', code: 'ArrowDown' });
    fireEvent.change(specSelector, { target: { value: 'spec-zeta' } });
    fireEvent.keyUp(specSelector, { key: 'ArrowDown', code: 'ArrowDown' });
    expect(specSelector).toHaveFocus();
    expect(specSelector).toHaveValue('spec-zeta');
    expect(openButton).toBeEnabled();
    expect(onOpenSpec).not.toHaveBeenCalled();

    activateFocusedButtonWithEnter(openButton);

    expect(onOpenSpec).toHaveBeenCalledTimes(1);
    expect(onOpenSpec).toHaveBeenCalledWith('spec-zeta');
  });
});
