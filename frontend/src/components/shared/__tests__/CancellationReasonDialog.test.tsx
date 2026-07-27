import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CancellationDetails, CancellationReasonDialog } from '../CancellationReasonDialog';

describe('CancellationReasonDialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <CancellationReasonDialog open={false} entityLabel="spec" onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('requires a non-empty reason before confirming', () => {
    const onConfirm = vi.fn();
    render(
      <CancellationReasonDialog open entityLabel="spec" onConfirm={onConfirm} onCancel={vi.fn()} />,
    );

    const confirm = screen.getByTestId('cancellation-dialog-confirm');
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    // Whitespace-only input still does not enable the confirm button.
    fireEvent.change(screen.getByTestId('cancellation-reason-input'), { target: { value: '   ' } });
    expect(screen.getByTestId('cancellation-dialog-confirm')).toBeDisabled();
    expect(screen.getByText(/A non-empty reason is required/)).toBeInTheDocument();
  });

  it('confirms with the trimmed reason once filled', () => {
    const onConfirm = vi.fn();
    render(
      <CancellationReasonDialog open entityLabel="card" onConfirm={onConfirm} onCancel={vi.fn()} />,
    );

    fireEvent.change(screen.getByTestId('cancellation-reason-input'), {
      target: { value: '  Duplicated work  ' },
    });
    const confirm = screen.getByTestId('cancellation-dialog-confirm');
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith('Duplicated work');
  });

  it('dismisses via the secondary button without confirming', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <CancellationReasonDialog open entityLabel="sprint" onConfirm={onConfirm} onCancel={onCancel} />,
    );

    fireEvent.click(screen.getByTestId('cancellation-dialog-dismiss'));
    expect(onCancel).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe('CancellationDetails', () => {
  it('renders the reason as markdown plus who and when', () => {
    render(
      <CancellationDetails
        reason={'## Why\n\nThis is **obsolete**'}
        cancelledBy="agent-42"
        cancelledAt="2026-07-01T12:00:00Z"
      />,
    );

    expect(screen.getByRole('heading', { name: 'Why' })).toBeInTheDocument();
    const bold = screen.getByText('obsolete');
    expect(bold.tagName).toBe('STRONG');
    expect(screen.getByText('agent-42')).toBeInTheDocument();
    expect(screen.getByText(new RegExp(new Date('2026-07-01T12:00:00Z').getFullYear().toString()))).toBeInTheDocument();
  });

  it('resolves the actor name when a resolver is provided and handles missing reason', () => {
    render(
      <CancellationDetails
        reason={null}
        cancelledBy="user-1"
        cancelledAt={null}
        resolveActorName={() => 'Jane Operator'}
      />,
    );

    expect(screen.getByText('Jane Operator')).toBeInTheDocument();
    expect(screen.getByText('No reason recorded')).toBeInTheDocument();
  });
});
