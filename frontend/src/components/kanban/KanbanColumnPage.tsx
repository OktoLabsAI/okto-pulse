import { useEffect, useRef, useState } from 'react';
import { AccessiblePaginator } from '@/components/shared/AccessiblePaginator';
import { usePersistedPagination } from '@/hooks/usePersistedPagination';
import { type BoardColumnsQuery, useDashboardApi } from '@/services/api';
import { useDashboardStore } from '@/store/dashboard';
import {
  STATUS_LABELS,
  type CardStatus,
  type CardSummary,
  type KanbanColumnMeta,
} from '@/types';
import { KanbanColumn, type KanbanColumnProps } from './KanbanColumn';
import { AuthenticatedFetchError } from '@/lib/authFetch';

interface KanbanColumnPageProps extends Omit<
  KanbanColumnProps,
  'cards' | 'canViewAll' | 'onViewAll' | 'footer'
> {
  boardId: string;
  query: BoardColumnsQuery;
  initialCards: CardSummary[];
  onItemsChange: (status: CardStatus, items: CardSummary[]) => void;
  onCollapse: () => void;
  paused?: boolean;
  onAccessLost?: () => void;
}
function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Server-side card pagination rendered in-place as a regular Kanban column.
 *
 * The page cache still uses the dashboard generation guards, while the
 * current response is rendered with the same KanbanCard surface as page one.
 */
export function KanbanColumnPage({
  boardId,
  status,
  query,
  initialCards,
  onItemsChange,
  onCollapse,
  paused = false,
  onAccessLost,
  ...columnProps
}: KanbanColumnPageProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const pagination = usePersistedPagination(`kanban-column-${status}`, boardId);
  const { setPagination } = pagination;
  const beginColumnPage = useDashboardStore((state) => state.beginColumnPage);
  const applyColumnPage = useDashboardStore((state) => state.applyColumnPage);
  const failColumnPage = useDashboardStore((state) => state.failColumnPage);
  const columnsGeneration = useDashboardStore((state) => state.columnsGeneration);
  const [items, setItems] = useState<CardSummary[]>(initialCards);
  const [meta, setMeta] = useState<KanbanColumnMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    if (paused) return;
    const controller = new AbortController();
    const token = beginColumnPage(status, pagination.requestIntent.offset);
    if (!token) {
      setLoading(false);
      setError('A request for this column is already in progress.');
      return () => controller.abort();
    }

    setLoading(true);
    setError(null);
    void apiRef.current.getBoardColumnPage(
      boardId,
      status,
      token.offset,
      {
        ...query,
        perColumnLimit: pagination.requestIntent.limit,
        signal: controller.signal,
      },
    ).then((response) => {
      if (controller.signal.aborted) return;
      if (
        response.board_id !== boardId
        || response.column !== status
        || response.offset !== token.offset
      ) {
        throw new Error('The server returned a page for a different column window.');
      }
      if (!applyColumnPage(token, response)) return;
      if (token.offset > 0 && token.offset >= response.meta.total_filtered) {
        // A live deletion can remove the current page. Keep the expanded
        // column, but request its nearest valid window instead of stranding it.
        setPagination(previous => ({
          ...previous,
          page: Math.max(1, Math.ceil(response.meta.total_filtered / previous.pageSize)),
        }));
        return;
      }
      setItems(response.items);
      setMeta(response.meta);
      onItemsChange(status, response.items);
    }).catch((requestError: unknown) => {
      if (controller.signal.aborted || isAbortError(requestError)) return;
      if (requestError instanceof AuthenticatedFetchError && [401, 403, 404].includes(requestError.status)) {
        onAccessLost?.(); return;
      }
      const message = requestError instanceof Error
        ? requestError.message
        : 'Failed to load the column page';
      failColumnPage(token, message);
      setError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });

    return () => {
      controller.abort();
      failColumnPage(token, 'Request superseded');
    };
  }, [
    applyColumnPage,
    beginColumnPage,
    boardId,
    columnsGeneration,
    failColumnPage,
    onItemsChange,
    pagination.requestIntent.limit,
    pagination.requestIntent.offset,
    setPagination,
    query,
    retryToken,
    status,
    paused,
    onAccessLost,
  ]);

  const totalFiltered = meta?.total_filtered ?? columnProps.totalCount ?? items.length;
  const totalOverall = meta?.total_overall ?? columnProps.totalCount ?? items.length;

  return (
    <KanbanColumn
      {...columnProps}
      status={status}
      cards={items}
      canViewAll={false}
      footer={(
        <div className="space-y-2 pt-1">
          <AccessiblePaginator
            compact
            page={pagination.page}
            pageSize={pagination.pageSize}
            totalFiltered={totalFiltered}
            totalOverall={totalOverall}
            itemCount={items.length}
            loading={loading}
            error={error}
            onRetry={() => setRetryToken((value) => value + 1)}
            onPaginationChange={pagination.setPagination}
            ariaLabel={`${STATUS_LABELS[status]} cards pagination`}
            emptyMessage={`No cards in ${STATUS_LABELS[status]}.`}
            testId={`kanban-column-${status}-paginator`}
          />
          <button
            type="button"
            onClick={onCollapse}
            aria-label={`Show fewer cards from ${STATUS_LABELS[status]}`}
            className="w-full rounded-lg border border-dashed border-surface-300 bg-white px-3 py-2 text-xs font-medium text-surface-600 transition-colors hover:bg-surface-100 dark:border-surface-600 dark:bg-gray-800 dark:text-surface-300 dark:hover:bg-gray-700"
          >
            Show fewer ↑
          </button>
        </div>
      )}
    />
  );
}
