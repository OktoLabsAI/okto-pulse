import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AccessiblePaginator } from '../AccessiblePaginator';

describe('AccessiblePaginator', () => {
  it('uses the filtered total for pages and reports the overall total separately', () => {
    render(
      <AccessiblePaginator
        page={2}
        pageSize={25}
        itemCount={25}
        totalFiltered={63}
        totalOverall={140}
        onPaginationChange={vi.fn()}
        ariaLabel="Specs pagination"
      />,
    );

    expect(screen.getByRole('navigation', { name: 'Specs pagination' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Showing 26–50 of 63 matching. 140 overall. Page 2 of 3.',
    );
    expect(screen.getByRole('button', { name: 'Page 2' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('button', { name: 'Page 4' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Items per page')).toHaveValue('25');
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['25', '50', '100']);
  });

  it('emits exactly one atomic request intent for a page or size change', () => {
    const onPaginationChange = vi.fn();
    render(
      <AccessiblePaginator
        page={2}
        pageSize={25}
        totalFiltered={200}
        totalOverall={240}
        onPaginationChange={onPaginationChange}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    expect(onPaginationChange).toHaveBeenCalledTimes(1);
    expect(onPaginationChange).toHaveBeenLastCalledWith({
      page: 3,
      pageSize: 25,
      offset: 50,
      limit: 25,
    });

    onPaginationChange.mockClear();
    fireEvent.change(screen.getByLabelText('Items per page'), { target: { value: '50' } });
    expect(onPaginationChange).toHaveBeenCalledTimes(1);
    expect(onPaginationChange).toHaveBeenLastCalledWith({
      page: 1,
      pageSize: 50,
      offset: 0,
      limit: 50,
    });
  });

  it('announces loading and disables request-producing controls', () => {
    render(
      <AccessiblePaginator
        page={1}
        pageSize={25}
        totalFiltered={75}
        totalOverall={75}
        loading
        onPaginationChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId('accessible-paginator')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('Loading page 1');
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByLabelText('Items per page')).toBeDisabled();
  });

  it('announces errors and exposes an optional retry action', () => {
    const onRetry = vi.fn();
    render(
      <AccessiblePaginator
        page={1}
        pageSize={25}
        totalFiltered={75}
        totalOverall={100}
        error="Network unavailable."
        onRetry={onRetry}
        onPaginationChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not load results. Network unavailable.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('renders empty and out-of-range states without automatic requests', () => {
    const emptyChange = vi.fn();
    const { rerender } = render(
      <AccessiblePaginator
        page={1}
        pageSize={25}
        totalFiltered={0}
        totalOverall={12}
        onPaginationChange={emptyChange}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('No matching results. 12 overall.');
    expect(emptyChange).not.toHaveBeenCalled();

    rerender(
      <AccessiblePaginator
        page={5}
        pageSize={25}
        totalFiltered={63}
        totalOverall={100}
        onPaginationChange={emptyChange}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Page 5 is out of range. The last available page is 3.',
    );
    expect(emptyChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Go to page 3' }));
    expect(emptyChange).toHaveBeenCalledTimes(1);
    expect(emptyChange).toHaveBeenCalledWith({
      page: 3,
      pageSize: 25,
      offset: 50,
      limit: 25,
    });
  });

  it('keeps visible keyboard focus styling on every interactive control', () => {
    render(
      <AccessiblePaginator
        page={1}
        pageSize={25}
        totalFiltered={50}
        totalOverall={50}
        onPaginationChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: 'Next page' })).toHaveClass('focus-visible:ring-2');
    expect(screen.getByLabelText('Items per page')).toHaveClass('focus-visible:ring-2');
  });
});
