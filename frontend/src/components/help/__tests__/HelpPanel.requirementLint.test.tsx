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
      name: 'Requirement lint — deterministic advisory analysis',
    });
    expect(heading).toBeInTheDocument();
    expect(heading.parentElement).toHaveTextContent(
      /finding count, not a percentage or approval score/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /lower is better/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /Zero findings does not authorize a transition/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /Checklist and Spec Validation are the authoritative controls/i,
    );
  });
});
