import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { HelpPanel } from '../HelpPanel';

function NestedHelpHarness({
  closeParent,
}: {
  closeParent: () => void;
}) {
  const [helpOpen, setHelpOpen] = useState(false);
  useEscapeToClose(closeParent, { priority: 150 });

  return (
    <>
      <div
        role="dialog"
        aria-label="Policy editor"
        aria-modal="true"
        aria-hidden="false"
      >
        <button type="button" onClick={() => setHelpOpen(true)}>
          Open policy help
        </button>
      </div>
      {helpOpen ? (
        <HelpPanel
          initialSectionId="policy-governance"
          onClose={() => setHelpOpen(false)}
        />
      ) : null}
    </>
  );
}

describe('HelpPanel Policy Governance guide', () => {
  it('deep-links to the canonical contract and exposes its governance markers', () => {
    render(
      <HelpPanel
        initialSectionId="policy-governance"
        onClose={vi.fn()}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    const heading = screen.getByRole('heading', {
      name: 'Policy Governance — Versioned rules with auditable enforcement',
    });

    expect(heading).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      /Only a structured policy\/v1 rule with explicit target entity types is executable/i,
    );
    expect(dialog).toHaveTextContent(
      /This context-only form is valid/i,
    );
    expect(dialog).toHaveTextContent(
      /Enforcement belongs to each rule/i,
    );
    expect(dialog).toHaveTextContent(
      /Is present and Is not present do not need a Value/i,
    );
    expect(dialog).toHaveTextContent(
      /Policy class records governance intent/i,
    );
    expect(dialog).toHaveTextContent(
      /They do not invoke a specialized coverage calculator, permission check, reviewer-identity check, or KG lineage check/i,
    );
    expect(dialog).toHaveTextContent(
      /An under-bump is rejected before a revision is created/i,
    );
    expect(dialog).toHaveTextContent(/Ready with waivers/i);
    expect(dialog).toHaveTextContent(
      /Stale history remains auditable but never authorizes a transition/i,
    );
    expect(dialog).toHaveTextContent(/Advisory findings never block/i);
    expect(dialog).toHaveTextContent(
      /Full Control receives all introduced leaves/i,
    );
    expect(
      screen.getByText('okto-pulse://reference/policy-compliance'),
    ).toBeInTheDocument();
  });

  it('opens the dedicated Fact catalog with configuration guidance and edge cases', () => {
    render(
      <HelpPanel
        initialSectionId="policy-facts"
        onClose={vi.fn()}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(screen.getByRole('heading', {
      name: 'Policy Facts — Configure deterministic conditions',
    })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      /A Fact is a typed, server-owned field from the entity snapshot/i,
    );
    expect(dialog).toHaveTextContent(
      /Conditions decide what those entities must satisfy for the rule to pass/i,
    );
    expect(dialog).toHaveTextContent(
      /Is present only tests whether the field exists; it does not mean true/i,
    );
    expect(dialog).toHaveTextContent(/Acceptance criteria coverage/i);
    expect(dialog).toHaveTextContent(/validation_unavailable/i);
    expect(dialog).toHaveTextContent(
      /Evidence count means current, authenticated scenario evidence, not a count of attachments/i,
    );
  });

  it('stacks above an opener dialog and restores accessibility, focus, and Escape ownership', () => {
    const closeParent = vi.fn();
    render(<NestedHelpHarness closeParent={closeParent} />);

    const parent = screen.getByRole('dialog', { name: 'Policy editor' });
    const opener = screen.getByRole('button', { name: 'Open policy help' });
    opener.focus();
    fireEvent.click(opener);

    const help = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(help.parentElement).toHaveClass('z-[200]');
    expect(help).toHaveAttribute('aria-modal', 'true');
    expect(parent).not.toHaveAttribute('aria-modal');
    expect(parent).toHaveAttribute('aria-hidden', 'true');
    expect(document.activeElement).toBe(help);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: 'Help Guide' })).not.toBeInTheDocument();
    expect(closeParent).not.toHaveBeenCalled();
    expect(parent).toHaveAttribute('aria-modal', 'true');
    expect(parent).toHaveAttribute('aria-hidden', 'false');
    expect(document.activeElement).toBe(opener);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closeParent).toHaveBeenCalledTimes(1);
  });

  it('restores the opener dialog when the explicit close action is used', () => {
    render(<NestedHelpHarness closeParent={vi.fn()} />);

    const parent = screen.getByRole('dialog', { name: 'Policy editor' });
    const opener = screen.getByRole('button', { name: 'Open policy help' });
    opener.focus();
    fireEvent.click(opener);

    fireEvent.click(screen.getByRole('button', { name: 'Close help' }));

    expect(screen.queryByRole('dialog', { name: 'Help Guide' })).not.toBeInTheDocument();
    expect(parent).toHaveAttribute('aria-modal', 'true');
    expect(parent).toHaveAttribute('aria-hidden', 'false');
    expect(document.activeElement).toBe(opener);
  });
});
