import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import toast, { type Toast } from 'react-hot-toast';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PulseErrorMessage } from '@/lib/toastError';
import { PulseToastCard } from '../PulseToaster';

function toastItem(overrides: Partial<Toast> = {}): Toast {
  return {
    type: 'success',
    id: 'toast-1',
    message: 'Saved successfully',
    duration: 4000,
    pauseDuration: 0,
    ariaProps: {
      role: 'status',
      'aria-live': 'polite',
    },
    createdAt: Date.now(),
    visible: true,
    dismissed: false,
    ...overrides,
  };
}

describe('PulseToastCard', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    document.documentElement.classList.remove('dark');
  });

  it('renders a themed success notification and allows explicit dismissal', () => {
    const dismiss = vi.spyOn(toast, 'dismiss').mockImplementation(() => undefined);
    const { container } = render(<PulseToastCard item={toastItem()} />);

    expect(screen.getByRole('status')).toHaveTextContent('Success');
    expect(screen.getByRole('status')).toHaveTextContent('Saved successfully');
    expect(
      container.querySelector('[data-toast-type="success"] > div:last-child'),
    ).toHaveClass(
      'dark:bg-surface-900',
    );
    expect(screen.getByTestId('pulse-toast-status-icon')).toHaveClass(
      'bg-emerald-100',
      'text-emerald-600',
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Dismiss notification' }),
    );
    expect(dismiss).toHaveBeenCalledWith('toast-1');
  });

  it('copies the complete error and expands details when they are available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const message =
      'Unable to save the guideline\n\nHTTP 409 conflict\nRule version is stale';
    render(
      <PulseToastCard
        item={toastItem({
          type: 'error',
          message,
        })}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('Error');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Unable to save the guideline',
    );
    expect(
      screen.queryByText(/Rule version is stale/),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Copy error' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(message));
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument();

    const toastCard = screen.getByRole('alert').closest('.pulse-toast');
    expect(toastCard).toHaveAttribute('data-expanded', 'false');

    const detailsButton = screen.getByRole('button', {
      name: 'Show error details',
    });
    expect(detailsButton).toHaveTextContent('Details');
    expect(detailsButton).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(detailsButton);

    const details = screen.getByLabelText('Error details');
    const lessButton = screen.getByRole('button', {
      name: 'Hide error details',
    });
    expect(details).toHaveTextContent(/HTTP 409 conflict/);
    expect(details).toHaveTextContent(/Rule version is stale/);
    expect(lessButton).toHaveTextContent('Less');
    expect(lessButton).toHaveAttribute('aria-expanded', 'true');
    expect(
      details.compareDocumentPosition(lessButton)
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(toastCard).toHaveAttribute('data-expanded', 'true');
  });

  it('keeps concise errors compact and hides detail-only actions', () => {
    const { container } = render(
      <PulseToastCard
        item={toastItem({
          type: 'error',
          message: 'Name is required',
          visible: false,
        })}
      />,
    );

    expect(
      screen.queryByRole('button', { name: 'Show error details' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Copy error' }),
    ).not.toBeInTheDocument();
    expect(container.querySelector('.pulse-toast')).toHaveAttribute(
      'data-visible',
      'false',
    );
    expect(container.querySelector('.pulse-toast')).toHaveAttribute(
      'data-expanded',
      'false',
    );
    expect(screen.getByTestId('pulse-toast-status-icon')).toHaveClass(
      'bg-red-100',
      'text-red-600',
    );
    expect(container.querySelector('.pulse-toast > div:last-child')).toHaveClass(
      'rounded-xl',
      'border-surface-200',
      'bg-white',
      'shadow-lg',
    );
    expect(
      screen.getByRole('button', { name: 'Dismiss notification' }),
    ).toHaveClass('h-7', 'w-7', 'shrink-0');
  });

  it('renders structured error details supplied by the shared error helper', () => {
    render(
      <PulseToastCard
        item={toastItem({
          type: 'error',
          message: (
            <PulseErrorMessage
              summary="The update conflicted"
              details={'Code: version_conflict\nStatus: 409'}
              copyText={'The update conflicted\n\nCode: version_conflict\nStatus: 409'}
            />
          ),
        })}
      />,
    );

    expect(screen.getByRole('alert')).toHaveAttribute(
      'aria-live',
      'assertive',
    );
    const detailsButton = screen.getByRole('button', {
      name: 'Show error details',
    });
    expect(detailsButton.getAttribute('aria-controls')).toMatch(/\S+/);
    fireEvent.click(detailsButton);
    expect(screen.getByText(/Code: version_conflict/)).toBeInTheDocument();
  });
});
