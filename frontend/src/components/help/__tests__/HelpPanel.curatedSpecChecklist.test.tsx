import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  GuidedHelpProvider,
  guidedHelpRegistry,
} from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel Curated Spec Checklist guide', () => {
  it('opens directly on the requested page and documents policy semantics', () => {
    render(
      <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
        <HelpPanel
          initialSectionId="curated-spec-checklist"
          onClose={vi.fn()}
        />
      </GuidedHelpProvider>,
    );

    const guideHeading = screen.getByRole('heading', {
        name: 'Curated Spec Checklist — Traceable Spec quality governance',
      });
    expect(screen.getByRole('dialog', { name: 'Help Guide' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close help' })).toBeInTheDocument();
    expect(guideHeading).toBeInTheDocument();
    expect(guideHeading.parentElement).toHaveTextContent(
      /Advisory is the recommended adoption mode/i,
    );
    expect(guideHeading.parentElement).toHaveTextContent(
      /Changing only the policy from Advisory to Blocking/i,
    );
    expect(guideHeading.parentElement).toHaveTextContent(
      /Turning off the score-based gate does not turn off this checklist/i,
    );
    expect(guideHeading.parentElement).toHaveTextContent(
      /does not alter existing boards/i,
    );
    expect(guideHeading.parentElement).toHaveTextContent(
      /board:read.*spec\.checklist\.read/i,
    );
  });
});
