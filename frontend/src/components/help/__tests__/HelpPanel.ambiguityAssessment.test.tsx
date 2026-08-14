import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  GuidedHelpProvider,
  guidedHelpRegistry,
} from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel ambiguity assessment lifecycle guidance', () => {
  it('explains when Ideation and Refinement assessments can be recorded', () => {
    render(
      <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
        <HelpPanel initialSectionId="ideations" onClose={vi.fn()} />
      </GuidedHelpProvider>,
    );

    let heading = screen.getByRole('heading', {
      name: 'Ambiguity assessment lifecycle',
    });
    expect(heading.parentElement).toHaveTextContent(
      /available only while the Ideation is Evaluating/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /review the current and previous results, but cannot record a new manual result/i,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Refinements' }));

    heading = screen.getByRole('heading', {
      name: 'Ambiguity assessment lifecycle',
    });
    expect(heading.parentElement).toHaveTextContent(
      /available only while the Refinement is Approved/i,
    );
    expect(heading.parentElement).toHaveTextContent(
      /previously recorded results remain available as history/i,
    );
  });
});
