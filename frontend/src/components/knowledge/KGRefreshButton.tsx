/**
 * KGRefreshButton — unified refresh control used across every KG sub-view.
 * A single implementation keeps the icon, keyboard shortcut hint and
 * disabled/loading states consistent between the graph canvas, audit log,
 * pending queue, settings panel and global discovery.
 */

import { useEffect } from 'react';

interface Props {
  onRefresh: () => void;
  loading?: boolean;
  /** Label used for aria-label + tooltip when `children` is the default text. */
  label?: string;
  /** When true, bind the `R` key to trigger the refresh (only one instance
   *  should enable this at a time to avoid duplicate firings). */
  shortcut?: boolean;
  className?: string;
  /** Optional data-testid override (default: `kg-refresh`). */
  testId?: string;
}

export function KGRefreshButton({
  onRefresh,
  loading = false,
  label = 'Refresh',
  shortcut = false,
  className = '',
  testId = 'kg-refresh',
}: Props) {
  useEffect(() => {
    if (!shortcut) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'r' && e.key !== 'R') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === 'INPUT' ||
          t.tagName === 'TEXTAREA' ||
          t.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      onRefresh();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onRefresh, shortcut]);

  return (
    <button
      type="button"
      onClick={onRefresh}
      disabled={loading}
      data-testid={testId}
      aria-label={label}
      title={shortcut ? `${label} (R)` : label}
      className={
        'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md shadow-sm ' +
        'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 ' +
        'text-sm text-gray-700 dark:text-gray-200 ' +
        'hover:bg-gray-50 dark:hover:bg-gray-700 ' +
        'disabled:opacity-50 disabled:cursor-wait ' +
        className
      }
    >
      <svg
        className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <polyline points="23 4 23 10 17 10" />
        <polyline points="1 20 1 14 7 14" />
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
        <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
      </svg>
      {loading ? 'Refreshing…' : label}
    </button>
  );
}
