import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HelpPanel } from '../HelpPanel';

describe('Spec validation user guide', () => {
  it('opens the exact result guide and explains outcomes, history and authority', () => {
    render(<HelpPanel initialSectionId="spec-validation" onClose={vi.fn()} />);
    const heading = screen.getByRole('heading', { name: 'Spec validation — Understand the result' });
    const content = heading.parentElement!;
    for (const title of ['Spec validated', 'Spec validated with exceptions', 'Ready to conclude validation',
      'Spec not yet validated', 'Validation could not be confirmed', 'Validation not required']) {
      expect(content).toHaveTextContent(title);
    }
    expect(content).toHaveTextContent('quality can pass while task decomposition still needs attention');
    expect(content).toHaveTextContent('Opening the screen or refreshing it never approves the Spec');
    expect(content).toHaveTextContent('Collapsing Details preserves drafts');
    expect(content).toHaveTextContent('earlier approvals remain in history');
    expect(content).toHaveTextContent('according to your permissions');
    expect(content).not.toHaveTextContent('validation-summary/v1');
  });

  it('is discoverable from the central Help navigation', () => {
    render(<HelpPanel onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Spec validation' }));
    expect(screen.getByRole('heading', { name: 'Spec validation — Understand the result' })).toBeInTheDocument();
  });
});
