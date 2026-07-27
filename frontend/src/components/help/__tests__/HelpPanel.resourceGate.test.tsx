import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  GuidedHelpProvider,
  guidedHelpRegistry,
} from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel Resource Gate authority', () => {
  it('documents blocking Architecture/Mockup and advisory Knowledge Base', () => {
    render(
      <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
        <HelpPanel onClose={vi.fn()} />
      </GuidedHelpProvider>,
    );

    fireEvent.click(
      screen.getByRole('button', { name: /Governance & rules/i }),
    );

    expect(
      screen.getByRole('heading', {
        name: 'Resource Gate (Architecture / Mockups blocking; Knowledge advisory)',
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Architecture designs and screen mockups are blocking/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Knowledge Base entries are advisory/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/missing or uncovered KB never blocks entity completion/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/do not create filler solely to satisfy a gate/i),
    ).toBeInTheDocument();
  });
});
