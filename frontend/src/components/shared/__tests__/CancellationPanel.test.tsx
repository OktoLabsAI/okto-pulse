import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CancellationPanel } from '../CancellationPanel';

describe('CancellationPanel', () => {
  it('renders the recorded audit data and optional previous status', () => {
    const resolveActorName = vi.fn(() => 'Quality Agent');
    render(
      <CancellationPanel
        id="ideation-1-cancellation"
        entityLabel="ideation"
        reason="Duplicated **scope**"
        cancelledBy="agent-9"
        cancelledAt="2026-07-10T09:00:00Z"
        previousStatus="evaluating"
        resolveActorName={resolveActorName}
      />,
    );

    const panel = screen.getByTestId('cancellation-details');
    expect(panel).toHaveAttribute('id', 'ideation-1-cancellation');
    expect(panel).toHaveAttribute('tabindex', '-1');
    expect(panel).toHaveAccessibleName('This ideation was cancelled');
    expect(panel).toHaveTextContent('Quality Agent');
    expect(panel).toHaveTextContent('Previous status: evaluating');
    expect(screen.getByText('scope').tagName).toBe('STRONG');
    expect(resolveActorName).toHaveBeenCalledWith('agent-9');
  });

  it('does not infer unavailable audit fields', () => {
    render(<CancellationPanel reason={null} />);

    expect(screen.getByText('This item was cancelled')).toBeInTheDocument();
    expect(
      screen.getByText((_, element) => (
        element?.tagName === 'P'
        && element.textContent === 'Cancelled by Unknown'
      )),
    ).toBeInTheDocument();
    expect(screen.getByText('No reason recorded')).toBeInTheDocument();
    expect(screen.queryByText(/Previous status:/)).not.toBeInTheDocument();
  });

  it('keeps an invalid historical timestamp visible instead of throwing', () => {
    render(
      <CancellationPanel
        cancelledAt="legacy timestamp"
        testId="legacy-cancellation"
      />,
    );

    expect(screen.getByTestId('legacy-cancellation')).toHaveTextContent(
      'legacy timestamp',
    );
  });
});
