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
      name: 'Policy Governance — Semantic guidelines with auditable board behavior',
    });

    expect(heading).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      /custom metrics that evaluators score from 0 to 100/i,
    );
    expect(dialog).toHaveTextContent(
      /This context-only form is valid/i,
    );
    expect(dialog).toHaveTextContent(
      /Choose Advisory or Blocking/i,
    );
    expect(dialog).toHaveTextContent(
      /does not expose policy classes, codes, Facts, predicates, or operators/i,
    );
    expect(dialog).toHaveTextContent(
      /stable metric identity is managed as read-only technical metadata/i,
    );
    expect(dialog).toHaveTextContent(
      /current head is fenced against concurrent edits/i,
    );
    expect(dialog).toHaveTextContent(
      /server rejects a selected bump below the minimum required/i,
    );
    expect(dialog).toHaveTextContent(/Ready with waivers/i);
    expect(dialog).toHaveTextContent(
      /earlier results remain available for review and never authorize the new edition/i,
    );
    expect(dialog).toHaveTextContent(/Advisory findings never block/i);
    expect(dialog).toHaveTextContent(
      /Full Control receives all introduced leaves/i,
    );
    expect(dialog).toHaveTextContent(/guidelines\.metrics\.author/i);
    expect(dialog).toHaveTextContent(/guidelines\.assessments\.read/i);
    expect(dialog).toHaveTextContent(
      /Agents cannot create it through MCP/i,
    );
    expect(
      screen.getByText('okto-pulse://reference/policy-compliance'),
    ).toBeInTheDocument();
  });

  it('opens semantic metric authoring and board-configuration guidance', () => {
    render(
      <HelpPanel
        initialSectionId="semantic-guideline-metrics"
        onClose={vi.fn()}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(screen.getByRole('heading', {
      name: 'Semantic guideline metrics — Authoring and board configuration',
    })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      /Confidence is fixed and system-owned/i,
    );
    expect(dialog).toHaveTextContent(
      /cannot be renamed, removed, targeted, or added to the metric override map/i,
    );
    expect(dialog).toHaveTextContent(
      /Evaluation rubric/i,
    );
    expect(dialog).toHaveTextContent(
      /Context-only revisions/i,
    );
    expect(dialog).toHaveTextContent(
      /Overrides use the stable metric key and never include confidence/i,
    );
    expect(dialog).toHaveTextContent(
      /stale preview or binding-head conflict requires a new preview/i,
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
