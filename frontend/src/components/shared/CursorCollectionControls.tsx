export interface CursorCollectionControlsProps {
  collectionLabel: string;
  itemCount: number;
  hasMore: boolean;
  loading: boolean;
  error: string | null;
  restartRequired?: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  onRestart: () => void;
  testId: string;
}

/**
 * Accessible controls for an opaque keyset cursor collection.
 *
 * It deliberately exposes no cursor value and makes recovery explicit when a
 * cursor is invalid or repeated. The caller owns item identity and ordering.
 */
export function CursorCollectionControls({
  collectionLabel,
  itemCount,
  hasMore,
  loading,
  error,
  restartRequired = false,
  onLoadMore,
  onRetry,
  onRestart,
  testId,
}: CursorCollectionControlsProps) {
  const action = restartRequired ? onRestart : onRetry;
  const actionLabel = restartRequired ? 'Restart from newest' : 'Try again';

  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-900/60"
      data-testid={testId}
      aria-busy={loading}
    >
      <p
        role={error ? 'alert' : 'status'}
        aria-live={error ? 'assertive' : 'polite'}
        aria-atomic="true"
        className={`text-xs ${
          error
            ? 'text-red-700 dark:text-red-300'
            : 'text-surface-500 dark:text-surface-400'
        }`}
      >
        {error
          ? `Could not continue ${collectionLabel}. ${error}`
          : loading
            ? `Loading ${collectionLabel}…`
            : `${itemCount} ${itemCount === 1 ? 'item' : 'items'} loaded${hasMore ? '; more available.' : '; all available items loaded.'}`}
      </p>

      {error ? (
        <button
          type="button"
          onClick={action}
          disabled={loading}
          className="min-h-8 rounded-lg border border-red-300 bg-white px-2.5 py-1 text-xs font-semibold text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-700 dark:bg-red-950/20 dark:text-red-200"
        >
          {actionLabel}
        </button>
      ) : hasMore ? (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loading}
          className="min-h-8 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs font-semibold text-surface-700 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      ) : itemCount > 0 ? (
        <button
          type="button"
          onClick={onRestart}
          disabled={loading}
          className="min-h-8 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs font-medium text-surface-600 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-300"
        >
          Refresh from newest
        </button>
      ) : null}
    </div>
  );
}

export default CursorCollectionControls;
