import { ArrowLeft } from 'lucide-react';
import {
  CanonicalCoveragePanel,
  type CanonicalCoveragePanelProps,
} from './CanonicalCoveragePanel';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';

export interface CanonicalCoverageFullViewProps extends Omit<
  CanonicalCoveragePanelProps,
  'from' | 'to' | 'onOpenFullView' | 'onQueryStateChange' | 'queryState' | 'viewMode'
> {
  boardId: string;
  queryState: CanonicalCoverageQueryState;
  onQueryStateChange: (query: CanonicalCoverageQueryState) => void;
  onBack: () => void;
}

export function CanonicalCoverageFullView({
  boardId,
  queryState,
  onQueryStateChange,
  onBack,
  ...panelProps
}: CanonicalCoverageFullViewProps) {
  return (
    <main
      aria-label="Coverage and Traceability analytics"
      className="min-h-screen space-y-5 bg-gray-50 p-4 text-gray-900 dark:bg-gray-950 dark:text-gray-100 md:p-8"
      data-board-id={boardId}
      data-testid="canonical-coverage-full-view"
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-cyan-600 dark:text-cyan-400">Analytics</p>
          <h1 className="mt-1 text-2xl font-semibold">Coverage &amp; Traceability</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Native coverage, governed skips, readiness, and evidence currentness.
          </p>
        </div>
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-2 rounded-md border border-gray-300 bg-white px-3 py-2 text-sm font-semibold hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-500 dark:border-gray-700 dark:bg-gray-900 dark:hover:bg-gray-800"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Back to Board Analytics
        </button>
      </header>

      <CanonicalCoveragePanel
        {...panelProps}
        from={queryState.from}
        to={queryState.to}
        queryState={queryState}
        onQueryStateChange={onQueryStateChange}
        viewMode="full"
      />
    </main>
  );
}
