import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  GuidedHelpProvider,
  guidedHelpRegistry,
} from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel Requirement lint guide', () => {
  it('deep-links to the advisory score and currentness guidance', () => {
    render(
      <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
        <HelpPanel
          initialSectionId="requirement-lint"
          onClose={vi.fn()}
        />
      </GuidedHelpProvider>,
    );

    const heading = screen.getByRole('heading', {
      name: 'Requirement lint — Edition-based advisory findings',
    });
    expect(heading).toBeInTheDocument();
    expect(heading.parentElement).toHaveTextContent(
      /finding count, not a percentage or approval score/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /lower is better/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /accepted requirement-lint result for the current edition is required/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /findings do not block by count or severity/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /external agent evaluates the current edition/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /result moves to Previous/i,
    );
    expect(heading.parentElement).not.toHaveTextContent(
      /stale receipt|receipt history/i,
    );
  });
});
