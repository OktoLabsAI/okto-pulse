import type { ReactNode } from 'react';
import {
  PAGINATION_PAGE_SIZES,
  paginationRequestIntent,
  type PaginationPageSize,
  type PaginationRequestIntent,
} from '@/hooks/usePersistedPagination';
import { paginationPageCount, visiblePaginationPages } from './pagination';

export interface AccessiblePaginatorProps {
  /** One-based page number controlled by the caller. */
  page: number;
  pageSize: PaginationPageSize;
  /** Count after active filters; this value drives the page count. */
  totalFiltered: number;
  /** Count before optional filters; displayed separately from totalFiltered. */
  totalOverall: number;
  /** Number of rows in the current response, used for an exact visible range. */
  itemCount?: number;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** One atomic callback per page or page-size interaction. */
  onPaginationChange: (intent: PaginationRequestIntent) => void;
  ariaLabel?: string;
  emptyMessage?: ReactNode;
  className?: string;
  testId?: string;
  /** Compact layout for narrow containers such as a Kanban column. */
  compact?: boolean;
}

const focusRing = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-900';
const buttonBase = `min-h-9 min-w-9 rounded-lg border px-2.5 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`;
const idleButton = `${buttonBase} border-surface-200 bg-white text-surface-700 hover:bg-surface-100 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-200 dark:hover:bg-surface-700`;

function nonNegativeInteger(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

export function AccessiblePaginator({
  page,
  pageSize,
  totalFiltered,
  totalOverall,
  itemCount,
  loading = false,
  error = null,
  onRetry,
  onPaginationChange,
  ariaLabel = 'Pagination',
  emptyMessage = 'No matching results.',
  className = '',
  testId = 'accessible-paginator',
  compact = false,
}: AccessiblePaginatorProps) {
  const filtered = nonNegativeInteger(totalFiltered);
  const overall = nonNegativeInteger(totalOverall);
  const currentPage = Math.max(1, Math.floor(page));
  const pageCount = paginationPageCount(filtered, pageSize);
  const lastPage = Math.max(1, pageCount);
  const outOfRange = currentPage > lastPage;
  const responseSize = itemCount === undefined
    ? Math.min(pageSize, Math.max(0, filtered - ((currentPage - 1) * pageSize)))
    : Math.min(pageSize, nonNegativeInteger(itemCount));
  const rangeStart = filtered === 0 || outOfRange ? 0 : ((currentPage - 1) * pageSize) + 1;
  const rangeEnd = rangeStart === 0 ? 0 : Math.min(filtered, rangeStart + responseSize - 1);
  const navigationDisabled = loading || Boolean(error) || filtered === 0 || outOfRange;

  const emit = (nextPage: number, nextPageSize: PaginationPageSize) => {
    onPaginationChange(paginationRequestIntent({ page: nextPage, pageSize: nextPageSize }));
  };

  let status: ReactNode;
  let statusRole: 'alert' | 'status' = 'status';
  let liveMode: 'assertive' | 'polite' = 'polite';
  if (loading) {
    status = `Loading page ${currentPage}…`;
  } else if (error) {
    statusRole = 'alert';
    liveMode = 'assertive';
    status = (
      <>
        <span>Could not load results. {error}</span>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className={`ml-2 underline underline-offset-2 ${focusRing}`}
          >
            Try again
          </button>
        )}
      </>
    );
  } else if (outOfRange) {
    status = (
      <>
        <span>
          Page {currentPage} is out of range. {pageCount === 0
            ? 'There are no matching results.'
            : `The last available page is ${lastPage}.`}
        </span>
        <button
          type="button"
          onClick={() => emit(lastPage, pageSize)}
          className={`ml-2 underline underline-offset-2 ${focusRing}`}
        >
          Go to {lastPage === 1 ? 'first page' : `page ${lastPage}`}
        </button>
      </>
    );
  } else if (filtered === 0) {
    status = (
      <>
        {emptyMessage} <span>{overall} overall.</span>
      </>
    );
  } else {
    status = (
      <>
        Showing {rangeStart}–{rangeEnd} of {filtered} matching. {overall} overall.
        {' '}Page {currentPage} of {pageCount}.
      </>
    );
  }

  return (
    <section
      aria-busy={loading}
      data-testid={testId}
      className={`flex gap-3 rounded-xl border border-surface-200 bg-white text-surface-700 dark:border-surface-700 dark:bg-surface-900 dark:text-surface-200 ${
        compact
          ? 'flex-col p-2'
          : 'flex-col p-3 sm:flex-row sm:items-center sm:justify-between'
      } ${className}`}
    >
      <p
        role={statusRole}
        aria-live={liveMode}
        aria-atomic="true"
        data-testid={`${testId}-status`}
        className={compact ? 'text-xs leading-4' : 'text-sm'}
      >
        {status}
      </p>

      <div className={compact ? 'flex flex-col gap-2' : 'flex flex-wrap items-center gap-3'}>
        <label
          className={compact
            ? 'flex items-center justify-between gap-2 text-xs'
            : 'flex items-center gap-2 text-sm'}
          htmlFor={`${testId}-page-size`}
        >
          <span>{compact ? 'Per page' : 'Items per page'}</span>
          <select
            id={`${testId}-page-size`}
            aria-label="Items per page"
            value={pageSize}
            disabled={loading}
            onChange={(event) => emit(1, Number(event.target.value) as PaginationPageSize)}
            className={`${compact ? 'min-h-8 text-xs' : 'min-h-9 text-sm'} rounded-lg border border-surface-300 bg-white px-2 py-1 text-surface-800 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-100 ${focusRing}`}
          >
            {PAGINATION_PAGE_SIZES.map((size) => (
              <option key={size} value={size}>{size}</option>
            ))}
          </select>
        </label>

        <nav aria-label={ariaLabel}>
          <div className={compact
            ? 'grid grid-cols-[1fr_auto_1fr] items-center gap-1'
            : 'flex items-center gap-1'}
          >
            <button
              type="button"
              disabled={navigationDisabled || currentPage <= 1}
              onClick={() => emit(currentPage - 1, pageSize)}
              aria-label="Previous page"
              className={idleButton}
            >
              Previous
            </button>

            {compact ? (
              <span
                aria-label={`Page ${currentPage} of ${lastPage}`}
                aria-current="page"
                className="min-w-10 text-center text-xs font-semibold text-surface-600 dark:text-surface-300"
              >
                {currentPage}/{lastPage}
              </span>
            ) : (
              visiblePaginationPages(currentPage, pageCount).map((token) => (
                typeof token === 'number' ? (
                  <button
                    key={token}
                    type="button"
                    disabled={navigationDisabled || token === currentPage}
                    onClick={() => emit(token, pageSize)}
                    aria-label={`Page ${token}`}
                    aria-current={token === currentPage ? 'page' : undefined}
                    className={token === currentPage
                      ? `${buttonBase} border-accent-600 bg-accent-600 text-white`
                      : idleButton}
                  >
                    {token}
                  </button>
                ) : (
                  <span
                    key={token}
                    aria-hidden="true"
                    className="min-w-6 text-center text-surface-400"
                  >
                    …
                  </span>
                )
              ))
            )}

            <button
              type="button"
              disabled={navigationDisabled || currentPage >= pageCount}
              onClick={() => emit(currentPage + 1, pageSize)}
              aria-label="Next page"
              className={idleButton}
            >
              Next
            </button>
          </div>
        </nav>
      </div>
    </section>
  );
}

export default AccessiblePaginator;
