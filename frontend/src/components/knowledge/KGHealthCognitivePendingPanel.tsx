/**
 * KGHealthCognitivePendingPanel — read-only feedback panel for cognitive
 * consolidation items (KG-03.5 / api_897dde99).
 *
 * Mounted inside KGHealthView after a successful rebuild. Polls
 * GET /api/v1/kg/cognitive-pending every `pollIntervalMs` while the
 * overlay is mounted, surfaces:
 *
 *   - counts by status (pending / in_progress / consolidated / skipped /
 *     failed / total)
 *   - selected generation id (with "latest" hint when caller did not
 *     pin a generation)
 *   - compact item list (item_id, artifact_type, source_ref, status,
 *     recorded_at, optional updated_at + agent + reason_code)
 *   - legacy mode badge when the on-disk record predates KG-03.1
 *
 * UI states (api_897dde99): loading | empty | ready | error.
 *
 * INVARIANT (br_2065f80b + AC9): this panel exposes NO action that
 * marks an item consolidated, skipped or failed. There is no
 * complete/skip/fail button, no free-text cognitive judgement input,
 * no inline status dropdown. Cognitive mutation flows through the MCP
 * tool only.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, BookOpen, CheckCircle2, ChevronLeft, ChevronRight, Clock, FileText, Inbox, Loader2, RefreshCw, SkipForward, XOctagon } from 'lucide-react';

import {
  getKGCognitivePendingItems,
  type KGCognitivePendingResponse,
} from '@/services/kg-health-api';
import { recordKGCognitivePendingPanelState } from '@/services/kg-cognitive-pending-telemetry';

interface KGHealthCognitivePendingPanelProps {
  boardId: string | null;
  selectedKgGenerationId: string | null;
  pollIntervalMs?: number;
}

const DEFAULT_POLL_INTERVAL_MS = 30000;
const DEFAULT_LIMIT = 25;

type UIState = 'loading' | 'empty' | 'ready' | 'error';

export function KGHealthCognitivePendingPanel({
  boardId,
  selectedKgGenerationId,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: KGHealthCognitivePendingPanelProps) {
  const [data, setData] = useState<KGCognitivePendingResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [uiState, setUiState] = useState<UIState>('loading');
  const [pageIndex, setPageIndex] = useState(0);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef<boolean>(false);
  // dataRef mirrors ``data`` so the error path can ALWAYS read the
  // latest successful snapshot, even when the polling interval was
  // created with a stale closure (Codex audit val_ed0f9548).
  const dataRef = useRef<KGCognitivePendingResponse | null>(null);
  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  const fetchOnce = useCallback(async () => {
    if (!boardId) {
      setUiState('empty');
      setData(null);
      return;
    }
    if (inFlightRef.current) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      return;
    }
    inFlightRef.current = true;
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const hasGeneration = Boolean(selectedKgGenerationId);
    try {
      const resp = await getKGCognitivePendingItems(
        boardId,
        {
          kgGenerationId: selectedKgGenerationId,
          limit: DEFAULT_LIMIT,
          offset: pageIndex * DEFAULT_LIMIT,
        },
        abortRef.current.signal,
      );
      setData(resp);
      setError(null);
      const nextState: UIState = resp.counts.total === 0 ? 'empty' : 'ready';
      setUiState(nextState);
      // or_229dfe09: emit one bounded sample per fetch outcome.
      recordKGCognitivePendingPanelState(
        nextState,
        Boolean(resp.selected_kg_generation_id) || hasGeneration,
      );
    } catch (err) {
      if ((err as DOMException)?.name === 'AbortError') return;
      setError(err as Error);
      // Preserve previous snapshot — error state stays non-blocking.
      const stillHasData = dataRef.current !== null;
      setUiState(stillHasData ? 'ready' : 'error');
      // or_229dfe09: always emit ``error`` for a failed fetch, even if
      // the UI keeps showing the previous snapshot. has_generation is
      // derived from the prior data or the explicit prop.
      const hasGenLabel =
        Boolean(dataRef.current?.selected_kg_generation_id) || hasGeneration;
      recordKGCognitivePendingPanelState('error', hasGenLabel);
    } finally {
      inFlightRef.current = false;
    }
  }, [boardId, selectedKgGenerationId, pageIndex]);

  useEffect(() => {
    setPageIndex(0);
  }, [boardId, selectedKgGenerationId]);

  useEffect(() => {
    if (!data) return;
    const maxPageIndex = Math.max(
      0,
      Math.ceil(data.counts.total / DEFAULT_LIMIT) - 1,
    );
    if (pageIndex > maxPageIndex) {
      setPageIndex(maxPageIndex);
    }
  }, [data, pageIndex]);

  useEffect(() => {
    if (!boardId) return;
    fetchOnce();
    intervalRef.current = setInterval(fetchOnce, pollIntervalMs);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      abortRef.current?.abort();
    };
  }, [boardId, selectedKgGenerationId, pollIntervalMs, fetchOnce]);

  const handleRefresh = useCallback(() => {
    void fetchOnce();
  }, [fetchOnce]);

  return (
    <section
      data-testid="kg-cognitive-pending-panel"
      aria-label="Cognitive consolidation feedback"
      className="mb-6 rounded-md border border-surface-200 bg-white p-4 shadow-sm dark:border-surface-700 dark:bg-surface-800"
    >
      <PanelHeader
        legacyMode={data?.legacy_mode ?? false}
        selectedKgGenerationId={data?.selected_kg_generation_id ?? null}
        explicitGeneration={Boolean(selectedKgGenerationId)}
        onRefresh={handleRefresh}
        loading={inFlightRef.current && !data}
      />

      {uiState === 'loading' && !data && <PanelSkeleton />}

      {uiState === 'error' && !data && error && (
        <ErrorState message={error.message} onRetry={handleRefresh} />
      )}

      {data && (
        <>
          {error && <InlineWarning message={error.message} />}
          <CountsRow counts={data.counts} />
          {data.items.length === 0 ? (
            <EmptyItemsHint />
          ) : (
            <>
              <PaginationControls
                pageIndex={pageIndex}
                pageSize={DEFAULT_LIMIT}
                total={data.counts.total}
                currentItemCount={data.items.length}
                onPrevious={() => setPageIndex((current) => Math.max(0, current - 1))}
                onNext={() => setPageIndex((current) => current + 1)}
              />
              <ItemList items={data.items} />
            </>
          )}
        </>
      )}

      {uiState === 'empty' && !data && (
        <EmptyItemsHint />
      )}
    </section>
  );
}

interface PanelHeaderProps {
  legacyMode: boolean;
  selectedKgGenerationId: string | null;
  explicitGeneration: boolean;
  onRefresh: () => void;
  loading: boolean;
}

function PanelHeader({
  legacyMode,
  selectedKgGenerationId,
  explicitGeneration,
  onRefresh,
  loading,
}: PanelHeaderProps) {
  return (
    <header className="mb-3 flex items-start justify-between gap-3">
      <div>
        <h3 className="flex items-center gap-2 text-sm font-semibold text-surface-900 dark:text-surface-100">
          <BookOpen className="h-4 w-4 text-indigo-500" aria-hidden />
          Cognitive consolidation
        </h3>
        <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
          Read-only progress for items the rebuild marked as pending
          cognitive consolidation.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {selectedKgGenerationId ? (
            <span
              data-testid="kg-cognitive-pending-generation"
              className="rounded bg-surface-100 px-2 py-0.5 font-mono text-surface-600 dark:bg-surface-700 dark:text-surface-300"
            >
              gen: {selectedKgGenerationId.slice(0, 8)}…
              {!explicitGeneration && (
                <span className="ml-1 text-surface-400">(latest)</span>
              )}
            </span>
          ) : (
            <span className="rounded bg-surface-100 px-2 py-0.5 text-surface-500 dark:bg-surface-700 dark:text-surface-300">
              no generation yet
            </span>
          )}
          {legacyMode && (
            <span
              data-testid="kg-cognitive-pending-legacy-badge"
              title="On-disk record predates KG-03; items are synthesized from the KG-02 aggregate"
              className="rounded bg-amber-100 px-2 py-0.5 font-semibold text-amber-800 dark:bg-amber-900 dark:text-amber-200"
            >
              legacy mode
            </span>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onRefresh}
        className="inline-flex items-center gap-1 rounded border border-surface-200 px-2 py-1 text-xs text-surface-600 hover:bg-surface-50 dark:border-surface-700 dark:text-surface-300 dark:hover:bg-surface-700"
        aria-label="Refresh cognitive pending"
      >
        {loading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
        ) : (
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        )}
        Refresh
      </button>
    </header>
  );
}

interface CountsRowProps {
  counts: KGCognitivePendingResponse['counts'];
}

function CountsRow({ counts }: CountsRowProps) {
  return (
    <div
      data-testid="kg-cognitive-pending-counts"
      className="grid grid-cols-2 gap-2 sm:grid-cols-6"
    >
      <CountTile
        label="Pending"
        value={counts.pending}
        tone="pending"
        icon={<Clock className="h-4 w-4" aria-hidden />}
        testId="count-pending"
      />
      <CountTile
        label="In progress"
        value={counts.in_progress}
        tone="in_progress"
        icon={<Loader2 className="h-4 w-4" aria-hidden />}
        testId="count-in_progress"
      />
      <CountTile
        label="Consolidated"
        value={counts.consolidated}
        tone="consolidated"
        icon={<CheckCircle2 className="h-4 w-4" aria-hidden />}
        testId="count-consolidated"
      />
      <CountTile
        label="Skipped"
        value={counts.skipped}
        tone="skipped"
        icon={<SkipForward className="h-4 w-4" aria-hidden />}
        testId="count-skipped"
      />
      <CountTile
        label="Failed"
        value={counts.failed}
        tone="failed"
        icon={<XOctagon className="h-4 w-4" aria-hidden />}
        testId="count-failed"
      />
      <CountTile
        label="Total"
        value={counts.total}
        tone="total"
        icon={<FileText className="h-4 w-4" aria-hidden />}
        testId="count-total"
      />
    </div>
  );
}

type CountTone =
  | 'pending'
  | 'in_progress'
  | 'consolidated'
  | 'skipped'
  | 'failed'
  | 'total';

const TONE_CLASSES: Record<CountTone, string> = {
  pending: 'bg-amber-50 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
  in_progress: 'bg-blue-50 text-blue-800 dark:bg-blue-900/30 dark:text-blue-200',
  consolidated: 'bg-emerald-50 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200',
  skipped: 'bg-surface-50 text-surface-600 dark:bg-surface-900/40 dark:text-surface-300',
  failed: 'bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-200',
  total: 'bg-indigo-50 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-200',
};

interface CountTileProps {
  label: string;
  value: number;
  tone: CountTone;
  icon: React.ReactNode;
  testId: string;
}

function CountTile({ label, value, tone, icon, testId }: CountTileProps) {
  return (
    <div
      data-testid={`kg-cognitive-pending-${testId}`}
      className={`flex items-center justify-between rounded px-3 py-2 text-sm font-medium ${TONE_CLASSES[tone]}`}
    >
      <span className="flex items-center gap-2 text-xs uppercase tracking-wide">
        {icon}
        {label}
      </span>
      <span className="font-mono text-base">{value}</span>
    </div>
  );
}

interface PaginationControlsProps {
  pageIndex: number;
  pageSize: number;
  total: number;
  currentItemCount: number;
  onPrevious: () => void;
  onNext: () => void;
}

function PaginationControls({
  pageIndex,
  pageSize,
  total,
  currentItemCount,
  onPrevious,
  onNext,
}: PaginationControlsProps) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const first = total === 0 ? 0 : pageIndex * pageSize + 1;
  const last = total === 0 ? 0 : first + currentItemCount - 1;
  const canPrevious = pageIndex > 0;
  const canNext = pageIndex + 1 < pageCount;

  return (
    <div
      data-testid="kg-cognitive-pending-pagination"
      className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded border border-surface-100 bg-surface-50 px-3 py-2 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300"
    >
      <span data-testid="kg-cognitive-pending-pagination-summary">
        Showing {first}-{last} of {total}
      </span>
      <div className="flex items-center gap-2">
        <span data-testid="kg-cognitive-pending-pagination-page">
          Page {pageIndex + 1} of {pageCount}
        </span>
        <button
          type="button"
          onClick={onPrevious}
          disabled={!canPrevious}
          className="inline-flex h-7 w-7 items-center justify-center rounded border border-surface-200 bg-white text-surface-600 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-300 dark:hover:bg-surface-700"
          aria-label="Previous cognitive pending page"
        >
          <ChevronLeft className="h-4 w-4" aria-hidden />
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canNext}
          className="inline-flex h-7 w-7 items-center justify-center rounded border border-surface-200 bg-white text-surface-600 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-300 dark:hover:bg-surface-700"
          aria-label="Next cognitive pending page"
        >
          <ChevronRight className="h-4 w-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

interface ItemListProps {
  items: KGCognitivePendingResponse['items'];
}

function ItemList({ items }: ItemListProps) {
  return (
    <ul
      data-testid="kg-cognitive-pending-items"
      className="mt-3 divide-y divide-surface-100 rounded border border-surface-100 dark:divide-surface-700 dark:border-surface-700"
    >
      {items.map((item) => (
        <li
          key={item.item_id}
          data-testid="kg-cognitive-pending-item"
          className="grid grid-cols-1 gap-1 px-3 py-2 text-xs sm:grid-cols-[1fr_auto] sm:items-center"
        >
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-surface-500 dark:text-surface-400">
              {item.item_id.slice(0, 12)}…
            </span>
            <span className="rounded bg-surface-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-surface-600 dark:bg-surface-700 dark:text-surface-300">
              {item.artifact_type}
            </span>
            <span className="font-mono text-surface-700 dark:text-surface-200">
              {item.source_ref}
            </span>
            {item.updated_by_agent_id && (
              <span className="text-surface-500 dark:text-surface-400">
                by {item.updated_by_agent_id.slice(0, 10)}…
              </span>
            )}
            {item.reason_code && (
              <span className="rounded bg-surface-100 px-1.5 py-0.5 font-mono text-[10px] text-surface-600 dark:bg-surface-700 dark:text-surface-300">
                {item.reason_code}
              </span>
            )}
          </div>
          <StatusBadge status={item.status} />
        </li>
      ))}
    </ul>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone: CountTone = (['pending', 'in_progress', 'consolidated', 'skipped', 'failed'] as CountTone[]).includes(
    status as CountTone,
  )
    ? (status as CountTone)
    : 'total';
  return (
    <span
      data-testid={`kg-cognitive-pending-item-status-${status}`}
      className={`inline-flex items-center justify-center rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${TONE_CLASSES[tone]}`}
    >
      {status}
    </span>
  );
}

function PanelSkeleton() {
  return (
    <div
      data-testid="kg-cognitive-pending-loading"
      className="flex items-center justify-center py-6 text-sm text-surface-500 dark:text-surface-400"
    >
      <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
      Loading cognitive consolidation status…
    </div>
  );
}

function EmptyItemsHint() {
  return (
    <div
      data-testid="kg-cognitive-pending-empty"
      className="flex flex-col items-center justify-center gap-1 rounded border border-dashed border-surface-200 py-6 text-sm text-surface-500 dark:border-surface-700 dark:text-surface-400"
    >
      <Inbox className="h-5 w-5" aria-hidden />
      No cognitive consolidation items for this generation.
    </div>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div
      data-testid="kg-cognitive-pending-error"
      className="flex items-start gap-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-900/30 dark:text-red-200"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">
        <p className="font-medium">
          Cognitive consolidation feedback unavailable
        </p>
        <p className="mt-0.5 text-xs">{message}</p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="rounded border border-red-300 px-2 py-1 text-xs font-medium text-red-800 hover:bg-red-100 dark:border-red-700 dark:text-red-200 dark:hover:bg-red-900/50"
      >
        Retry
      </button>
    </div>
  );
}

function InlineWarning({ message }: { message: string }) {
  return (
    <div
      data-testid="kg-cognitive-pending-inline-warning"
      className="mb-2 flex items-start gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-1 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200"
    >
      <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
      <span>
        Showing the last good snapshot — latest refresh failed: {message}
      </span>
    </div>
  );
}
