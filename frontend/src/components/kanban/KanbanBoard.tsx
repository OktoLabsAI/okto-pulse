/**
 * KanbanBoard - Main board component with drag and drop
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  DndContext,
  type DragEndEvent,
  type DragOverEvent,
  DragOverlay,
  type DragStartEvent,
  rectIntersection,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import { Filter, Search, X, Check } from 'lucide-react';
import toast from 'react-hot-toast';
import {
  ImpactEvidenceEditor,
} from '@/components/cards/ImpactEvidenceEditor';
import {
  buildImpactEvidencePayload,
  emptyImpactEvidenceDraft,
  type ImpactEvidenceDraft,
} from '@/components/cards/impactEvidenceModel';
import { type BoardColumnsQuery, useDashboardApi } from '@/services/api';
import {
  useDashboardStore,
  useColumns,
  useColumnsMeta,
  useCurrentBoard,
} from '@/store/dashboard';
import {
  CARD_STATUSES,
  STATUS_LABELS,
  type CardStatus,
  type CardSummary,
  type CardType,
  type LookupOption,
} from '@/types';
import { SearchInput } from '@/components/shared/SearchInput';
import { KanbanColumn, type KanbanCardFilterType } from './KanbanColumn';
import { useCognitivePendingBadges } from '@/hooks/useCognitivePendingBadges';
import {
  hasPermissionWithState,
  usePermissions,
} from '@/hooks/usePermissions';
import { CardModal } from './CardModal';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { CreateCardModal } from './CreateCardModal';
import { CancellationReasonDialog } from '@/components/shared/CancellationReasonDialog';
import {
  resolveKanbanDropDestination,
  type KanbanDropDestination,
} from './kanbanDnd';
import { KanbanColumnPage } from './KanbanColumnPage';

interface KanbanBoardProps {
  boardId: string;
  /** Requests a server refresh without remounting or clearing active filters. */
  refreshKey?: number;
}

const CARD_TYPE_FILTERS: KanbanCardFilterType[] = ['task', 'test', 'bug'];
const KANBAN_COLUMN_LIMIT = 10;

type CardTypeFiltersByStatus = Record<CardStatus, Set<KanbanCardFilterType>>;

/**
 * Typed gate rejections carry a `remediation` string alongside the message.
 * authFetch parks the whole backend detail object on `details`, so read it
 * from there rather than re-parsing the message.
 */
function extractRemediation(error: unknown): string | null {
  const details = (error as { details?: unknown } | null)?.details;
  if (!details || typeof details !== 'object') return null;
  const remediation = (details as Record<string, unknown>).remediation;
  return typeof remediation === 'string' && remediation.trim()
    ? remediation
    : null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function apiCardType(type: KanbanCardFilterType): CardType {
  return type === 'task' ? 'normal' : type;
}

function createDefaultCardTypeFilters(): CardTypeFiltersByStatus {
  return CARD_STATUSES.reduce<CardTypeFiltersByStatus>((acc, status) => {
    acc[status] = new Set(CARD_TYPE_FILTERS);
    return acc;
  }, {} as CardTypeFiltersByStatus);
}

export function KanbanBoard({ boardId, refreshKey = 0 }: KanbanBoardProps) {
  const api = useDashboardApi();
  const permissions = usePermissions(boardId);
  const apiRef = useRef(api);
  apiRef.current = api;
  const columns = useColumns();
  const columnsMeta = useColumnsMeta();
  const currentBoard = useCurrentBoard();
  const {
    openCardModal,
    optimisticMoveCard,
    beginColumnsGeneration,
    applyColumnsBatch,
  } = useDashboardStore();

  // Build id→name map from board agents + owner
  const nameMap: Record<string, string> = {};
  if (currentBoard) {
    // Board owner (user) — show as "Owner" if no better name available
    if (currentBoard.owner_id) {
      nameMap[currentBoard.owner_id] = 'Owner';
    }
    for (const a of currentBoard.agents) {
      nameMap[a.id] = a.name;
    }
  }

  const [activeCard, setActiveCard] = useState<CardSummary | null>(null);
  const [dragFromStatus, setDragFromStatus] = useState<CardStatus | null>(null);
  const [createCardStatus, setCreateCardStatus] = useState<CardStatus | null>(null);
  // Execution report modal for Validation/Done moves
  const [conclusionPending, setConclusionPending] = useState<{
    cardId: string;
    sourceStatus: CardStatus;
    destination: KanbanDropDestination;
  } | null>(null);
  // Cancellation justification modal for drops on the Cancelled column (ITEM 17)
  const [cancelPending, setCancelPending] = useState<{
    cardId: string;
    sourceStatus: CardStatus;
    destination: KanbanDropDestination;
  } | null>(null);
  const [conclusionText, setConclusionText] = useState('');
  const [conclusionCompleteness, setConclusionCompleteness] = useState(100);
  const [conclusionCompletenessJustification, setConclusionCompletenessJustification] = useState('');
  const [conclusionDrift, setConclusionDrift] = useState(0);
  const [conclusionDriftJustification, setConclusionDriftJustification] = useState('');
  const [conclusionImpactDraft, setConclusionImpactDraft] = useState<ImpactEvidenceDraft>(emptyImpactEvidenceDraft());
  // AC-16: gate rejection renders inline and the modal stays open.
  const [conclusionGateError, setConclusionGateError] = useState<
    { message: string; remediation: string | null } | null
  >(null);
  const [showArchived, setShowArchived] = useState(false);
  const [specFilter, setSpecFilter] = useState<Set<string>>(new Set());
  const [cardTypeFilters, setCardTypeFilters] = useState<CardTypeFiltersByStatus>(
    createDefaultCardTypeFilters,
  );
  const [specs, setSpecs] = useState<LookupOption[]>([]);
  const [specSearchOpen, setSpecSearchOpen] = useState(false);
  const [specSearchQuery, setSpecSearchQuery] = useState('');
  const [debouncedSpecSearch, setDebouncedSpecSearch] = useState('');
  const [cardSearchQuery, setCardSearchQuery] = useState('');
  const [debouncedCardSearch, setDebouncedCardSearch] = useState('');
  const [columnsLoading, setColumnsLoading] = useState(false);
  const [columnsError, setColumnsError] = useState<string | null>(null);
  const [columnsReloadToken, setColumnsReloadToken] = useState(0);
  const [viewAllStatus, setViewAllStatus] = useState<CardStatus | null>(null);
  const [columnPageView, setColumnPageView] = useState<{
    status: CardStatus;
    items: CardSummary[];
  } | null>(null);
  const [viewAllContextKey, setViewAllContextKey] = useState<string | null>(null);
  const specDropdownRef = useRef<HTMLDivElement>(null);
  const hasPermission = permissions.has;
  const canCreateCard = hasPermission('card.entity.create')
    || hasPermission('card.entity.create_test');
  const canMoveCard = useCallback((
    sourceStatus: CardStatus,
    targetStatus: CardStatus,
  ) => hasPermissionWithState(
    hasPermission,
    sourceStatus === targetStatus
      ? 'card.entity.edit_fields'
      : `card.move.${sourceStatus}_to_${targetStatus}`,
    'card',
    sourceStatus,
  ), [hasPermission]);
  const canStartCardDrag = useCallback(
    (card: CardSummary) => hasPermission(`card.interact_in.${card.status}`),
    [hasPermission],
  );

  const resetConclusionFields = () => {
    setConclusionText('');
    setConclusionCompleteness(100);
    setConclusionCompletenessJustification('');
    setConclusionDrift(0);
    setConclusionDriftJustification('');
    setConclusionImpactDraft(emptyImpactEvidenceDraft());
    setConclusionGateError(null);
  };

  const requiresExecutionReport = (
    card: CardSummary | undefined,
    targetStatus: CardStatus,
    sourceStatus: CardStatus | null,
  ) => {
    if (targetStatus === 'done') return true;
    if (targetStatus !== 'validation') return false;
    if (card?.card_type === 'test') return false;
    return sourceStatus !== null && ['not_started', 'started', 'in_progress', 'on_hold'].includes(sourceStatus);
  };

  // Close spec search on outside click
  useEffect(() => {
    if (!specSearchOpen) return;
    const handler = (e: MouseEvent) => {
      if (specDropdownRef.current && !specDropdownRef.current.contains(e.target as Node)) setSpecSearchOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [specSearchOpen]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedCardSearch(cardSearchQuery), 250);
    return () => window.clearTimeout(timer);
  }, [cardSearchQuery]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSpecSearch(specSearchQuery), 250);
    return () => window.clearTimeout(timer);
  }, [specSearchQuery]);

  const columnsQuery = useMemo<BoardColumnsQuery>(() => {
    const cardTypesByStatus: Partial<Record<CardStatus, CardType[]>> = {};
    for (const status of CARD_STATUSES) {
      const active = cardTypeFilters[status] ?? new Set(CARD_TYPE_FILTERS);
      if (active.size < CARD_TYPE_FILTERS.length) {
        cardTypesByStatus[status] = [...active].map(apiCardType);
      }
    }
    return {
      perColumnLimit: KANBAN_COLUMN_LIMIT,
      specIds: [...specFilter].filter((id) => id !== '__unlinked__'),
      includeUnlinked: specFilter.has('__unlinked__'),
      cardTypesByStatus,
      search: debouncedCardSearch,
      includeArchived: showArchived,
    };
  }, [cardTypeFilters, debouncedCardSearch, showArchived, specFilter]);

  const visibleColumns = useMemo(
    () => CARD_STATUSES.reduce<Record<CardStatus, CardSummary[]>>((result, status) => {
      result[status] = (columns[status] ?? []).slice(0, KANBAN_COLUMN_LIMIT);
      return result;
    }, {} as Record<CardStatus, CardSummary[]>),
    [columns],
  );

  const columnsQueryKey = useMemo(
    () => JSON.stringify(columnsQuery),
    [columnsQuery],
  );
  const columnViewContextKey = useMemo(
    () => JSON.stringify([boardId, columnsQueryKey, columnsReloadToken, refreshKey]),
    [boardId, columnsQueryKey, columnsReloadToken, refreshKey],
  );
  const activeViewAllStatus = viewAllContextKey === columnViewContextKey
    ? viewAllStatus
    : null;
  const renderedColumns = useMemo(() => {
    if (!activeViewAllStatus || columnPageView?.status !== activeViewAllStatus) {
      return visibleColumns;
    }
    return {
      ...visibleColumns,
      [activeViewAllStatus]: columnPageView.items,
    };
  }, [activeViewAllStatus, columnPageView, visibleColumns]);

  const refreshColumns = useCallback(() => {
    setColumnsReloadToken((value) => value + 1);
  }, []);

  const handleViewAll = useCallback((status: CardStatus) => {
    setColumnPageView(null);
    setViewAllStatus(status);
    setViewAllContextKey(columnViewContextKey);
  }, [columnViewContextKey]);

  const handleColumnPageItems = useCallback((status: CardStatus, items: CardSummary[]) => {
    setColumnPageView({ status, items });
  }, []);

  const collapseColumnPage = useCallback(() => {
    setViewAllStatus(null);
    setColumnPageView(null);
    setViewAllContextKey(null);
  }, []);

  useEffect(() => {
    setViewAllStatus(null);
    setColumnPageView(null);
    setViewAllContextKey(null);
  }, [boardId, columnsQueryKey, columnsReloadToken, refreshKey]);

  // The generation belongs to this exact request closure. Abort is best-effort;
  // stale responses are still rejected by applyColumnsBatch.
  useEffect(() => {
    if (!boardId) return undefined;
    const controller = new AbortController();
    const generation = beginColumnsGeneration();
    setColumnsLoading(true);
    setColumnsError(null);

    void apiRef.current.getBoardColumns(boardId, {
      ...columnsQuery,
      signal: controller.signal,
    }).then((response) => {
      if (applyColumnsBatch(generation, response)) setColumnsLoading(false);
    }).catch((error: unknown) => {
      if (isAbortError(error)) return;
      if (generation === useDashboardStore.getState().columnsGeneration) {
        setColumnsLoading(false);
        setColumnsError(error instanceof Error ? error.message : 'Failed to load cards');
      }
    });

    return () => controller.abort();
  }, [
    applyColumnsBatch,
    beginColumnsGeneration,
    boardId,
    columnsQuery,
    columnsQueryKey,
    columnsReloadToken,
    refreshKey,
  ]);

  // Lookup is intentionally independent from loaded Kanban pages, so options
  // remain complete even when only the first card page is visible.
  useEffect(() => {
    if (!boardId || !specSearchOpen) return undefined;
    const controller = new AbortController();
    void apiRef.current.lookupSpecs(boardId, {
      search: debouncedSpecSearch,
      limit: 50,
      linkedToCards: true,
      includeArchivedCards: showArchived,
      signal: controller.signal,
    }).then((response) => {
      setSpecs((previous) => {
        const merged = new Map<string, LookupOption>();
        for (const option of previous) {
          if (specFilter.has(option.id)) merged.set(option.id, option);
        }
        for (const option of response.items) merged.set(option.id, option);
        return [...merged.values()];
      });
    }).catch((error: unknown) => {
      if (!isAbortError(error)) {
        toast.error(error instanceof Error ? error.message : 'Failed to load spec options');
      }
    });
    return () => controller.abort();
  }, [boardId, debouncedSpecSearch, showArchived, specFilter, specSearchOpen]);

  const toggleSpecFilter = (id: string) => {
    setSpecFilter((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleCardTypeFilter = (status: CardStatus, type: KanbanCardFilterType) => {
    setCardTypeFilters((prev) => {
      const next = new Set(prev[status] ?? CARD_TYPE_FILTERS);
      if (next.has(type)) {
        // The transport omits an empty type set, which means "all". Keep at
        // least one type selected so the UI never sends a misleading filter.
        if (next.size === 1) return prev;
        next.delete(type);
      } else {
        next.add(type);
      }
      return {
        ...prev,
        [status]: next,
      };
    });
  };

  useEscapeToClose(() => {
    setConclusionPending(null);
    resetConclusionFields();
  }, { enabled: Boolean(conclusionPending) });

  // KG-03.6 — batch cognitive pending badges for visible task/test/bug
  // surfaces. ONE HTTP request per (board, visible card-id) change;
  // never one per card (api_28a22fec batch semantics).
  const visibleCardSourceRefs = useMemo(() => {
    const refs: string[] = [];
    for (const status of CARD_STATUSES) {
      for (const card of renderedColumns[status] || []) {
        if (card.card_type === 'test') {
          refs.push(`test:${card.id}`);
        } else if (card.card_type === 'bug') {
          refs.push(`bug:${card.id}`);
        } else if (!card.card_type || card.card_type === 'normal') {
          // ``normal`` (default) cards represent tasks.
          refs.push(`task:${card.id}`);
        }
      }
    }
    return refs;
  }, [renderedColumns]);
  const { badges: cognitiveBadges } = useCognitivePendingBadges(
    boardId,
    visibleCardSourceRefs,
  );

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8,
      },
    })
  );

  const handleDragStart = (event: DragStartEvent) => {
    const { active } = event;
    const cardId = active.id as string;

    // Find the card being dragged and remember its origin
    for (const status of CARD_STATUSES) {
      const card = (renderedColumns[status] || []).find((c) => c.id === cardId);
      if (card) {
        if (!canStartCardDrag(card)) return;
        setActiveCard(card);
        setDragFromStatus(status);
        break;
      }
    }
  };

  const handleDragOver = (_event: DragOverEvent) => {
    // No-op: optimistic moves only happen in handleDragEnd
    // Doing them here corrupts column state and prevents the API call
  };

  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveCard(null);
    const fromStatus = dragFromStatus;
    setDragFromStatus(null);

    if (!over) return;
    if (!fromStatus) {
      refreshColumns();
      return;
    }

    const cardId = active.id as string;
    const overId = over.id as string;
    const destination = resolveKanbanDropDestination(renderedColumns, cardId, overId);
    if (!destination) {
      // The rendered page may have changed while dragging. Fail closed and
      // refetch instead of ever deriving a sentinel/negative position.
      refreshColumns();
      return;
    }
    const { targetStatus, targetIndex } = destination;
    if (!canMoveCard(fromStatus, targetStatus)) return;

    const card = Object.values(renderedColumns).flat().find((c) => c.id === cardId);

    // ITEM 17: cancelling requires a justification — intercept the drop.
    if (targetStatus === 'cancelled' && fromStatus !== 'cancelled') {
      setCancelPending({ cardId, sourceStatus: fromStatus, destination });
      return;
    }

    // Require the executor's report before a reviewer sees the card in Validation.
    if (requiresExecutionReport(card, targetStatus, fromStatus)) {
      setConclusionPending({ cardId, sourceStatus: fromStatus, destination });
      resetConclusionFields();
      return;
    }

    // Optimistic update
    optimisticMoveCard(cardId, targetStatus, targetIndex);

    // API call + refresh from server. Paginated DnD is always anchor-based;
    // position is deliberately absent from the wire request.
    try {
      await apiRef.current.moveCard(cardId, destination.request);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to move card');
    } finally {
      refreshColumns();
    }
  };

  const handleAddCard = (status: CardStatus) => {
    if (!canCreateCard) return;
    setCreateCardStatus(status);
  };

  const handleConclusionSubmit = async () => {
    if (!conclusionPending || !conclusionText.trim() || !conclusionCompletenessJustification.trim() || !conclusionDriftJustification.trim()) return;
    const { cardId, sourceStatus, destination } = conclusionPending;
    const { targetStatus, targetIndex } = destination;
    if (!canMoveCard(sourceStatus, targetStatus)) return;

    // AC-16: the modal only closes after the move SUCCEEDS. A gate rejection
    // (409 impact_evidence_required) renders its remediation inline and every
    // typed field/row stays intact for correction.
    setConclusionGateError(null);
    try {
      await apiRef.current.moveCard(cardId, {
        ...destination.request,
        conclusion: conclusionText.trim(),
        completeness: conclusionCompleteness,
        completeness_justification: conclusionCompletenessJustification.trim(),
        drift: conclusionDrift,
        drift_justification: conclusionDriftJustification.trim(),
        ...(buildImpactEvidencePayload(conclusionImpactDraft)
          ? { impact_evidence: buildImpactEvidencePayload(conclusionImpactDraft) }
          : {}),
      });
      optimisticMoveCard(cardId, targetStatus, targetIndex);
      setConclusionPending(null);
      resetConclusionFields();
      toast.success(`Card moved to ${STATUS_LABELS[targetStatus]}`);
      refreshColumns();
    } catch (err) {
      // The modal holds a long hand-typed report plus every declared evidence
      // row: NO rejection may discard it. Show the reason in place and keep
      // the form open — the author corrects and resubmits, or cancels
      // explicitly. (A rejected move never mutated anything server-side.)
      const message = err instanceof Error ? err.message : `Failed to move card to ${STATUS_LABELS[targetStatus]}`;
      setConclusionGateError({ message, remediation: extractRemediation(err) });
    }
  };

  const handleCancelSubmit = async (reason: string) => {
    if (!cancelPending) return;
    const { cardId, sourceStatus, destination } = cancelPending;
    if (!canMoveCard(sourceStatus, destination.targetStatus)) {
      setCancelPending(null);
      return;
    }
    setCancelPending(null);

    optimisticMoveCard(cardId, 'cancelled', destination.targetIndex);
    try {
      await apiRef.current.moveCard(cardId, {
        ...destination.request,
        cancellation_reason: reason,
      });
      toast.success(`Card moved to ${STATUS_LABELS.cancelled}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to cancel card');
    } finally {
      refreshColumns();
    }
  };

  const handleCardClick = (cardId: string) => {
    openCardModal(cardId);
  };

  const conclusionTargetLabel = conclusionPending
    ? STATUS_LABELS[conclusionPending.destination.targetStatus]
    : 'target column';
  const totalFilteredCards = CARD_STATUSES.reduce(
    (total, status) => total + (columnsMeta[status]?.total_filtered ?? (columns[status] ?? []).length),
    0,
  );
  const totalOverallCards = CARD_STATUSES.reduce(
    (total, status) => total + (columnsMeta[status]?.total_overall ?? (columns[status] ?? []).length),
    0,
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Spec filter bar */}
      <div className="mb-3 flex shrink-0 flex-wrap items-center gap-1.5">
        <SearchInput
          value={cardSearchQuery}
          onChange={setCardSearchQuery}
          placeholder="Search cards…"
          testId="cards-search"
          className="mr-2"
        />
        {cardSearchQuery && (
          <span className="text-[10px] text-gray-400">
            {totalFilteredCards} of {totalOverallCards} cards
          </span>
        )}
        <Filter size={14} className="text-gray-400 shrink-0" />

        {/* Unlinked filter */}
        <button
          onClick={() => toggleSpecFilter('__unlinked__')}
          className={`text-xs px-2 py-1 rounded-full transition-colors ${
            specFilter.has('__unlinked__')
              ? 'bg-gray-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400 dark:hover:bg-gray-600'
          }`}
        >
          Unlinked
        </button>

        {/* Selected spec pills */}
        {specs
          .filter((s) => specFilter.has(s.id))
          .map((s) => (
            <button
              key={s.id}
              onClick={() => toggleSpecFilter(s.id)}
              className="text-xs px-2 py-1 rounded-full bg-violet-600 text-white flex items-center gap-1"
              title={s.title}
            >
              {s.title.length > 25 ? s.title.slice(0, 22) + '...' : s.title}
              <X size={10} />
            </button>
          ))}

        {/* Search dropdown */}
        <div className="relative" ref={specDropdownRef}>
          <button
            onClick={() => { setSpecSearchOpen(!specSearchOpen); setSpecSearchQuery(''); }}
            className="text-xs px-2 py-1 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400 dark:hover:bg-gray-600 transition-colors flex items-center gap-1"
          >
            <Search size={10} />
            Filter specs...
          </button>

          {specSearchOpen && (
            <div className="absolute left-0 top-full mt-1 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-xl z-50 overflow-hidden">
              <div className="p-2 border-b border-gray-200 dark:border-gray-700">
                <input
                  type="text"
                  value={specSearchQuery}
                  onChange={(e) => setSpecSearchQuery(e.target.value)}
                  placeholder="Search specs..."
                  className="w-full px-2 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-violet-500"
                  autoFocus
                />
              </div>
              <div className="max-h-48 overflow-y-auto">
                {specs
                  .map((s) => {
                    const isSelected = specFilter.has(s.id);
                    return (
                      <button
                        key={s.id}
                        onClick={() => toggleSpecFilter(s.id)}
                        className={`w-full text-left px-3 py-2 text-xs flex items-center gap-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors ${
                          isSelected ? 'bg-violet-50 dark:bg-violet-900/20' : ''
                        }`}
                      >
                        <div className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ${
                          isSelected
                            ? 'bg-violet-600 border-violet-600 text-white'
                            : 'border-gray-300 dark:border-gray-600'
                        }`}>
                          {isSelected && <Check size={10} />}
                        </div>
                        <span className="truncate text-gray-700 dark:text-gray-300">{s.title}</span>
                        <span className="text-[10px] text-gray-400 shrink-0 ml-auto">{s.status}</span>
                      </button>
                    );
                  })}
                {specs.length === 0 && (
                  <p className="px-3 py-4 text-xs text-gray-400 text-center">No matching specs</p>
                )}
              </div>
              {specFilter.size > 0 && (
                <div className="p-2 border-t border-gray-200 dark:border-gray-700">
                  <button
                    onClick={() => { setSpecFilter(new Set()); setSpecSearchOpen(false); }}
                    className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    Clear all filters
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {specFilter.size > 0 && (
          <span className="text-[10px] text-gray-400">{specFilter.size} spec{specFilter.size !== 1 ? 's' : ''} selected</span>
        )}

        <button
          onClick={() => setShowArchived(!showArchived)}
          className={`text-xs px-2 py-1 rounded-full transition-colors ml-auto ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      {columnsError && (
        <div role="alert" className="mb-3 flex shrink-0 items-center justify-between rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
          <span>{columnsError}</span>
          <button type="button" onClick={refreshColumns} className="font-medium underline underline-offset-2">
            Retry
          </button>
        </div>
      )}
      {columnsLoading && (
        <p role="status" aria-live="polite" className="mb-2 shrink-0 text-xs text-gray-500">
          Loading cards…
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-hidden" aria-busy={columnsLoading}>
        <DndContext
          sensors={sensors}
          collisionDetection={rectIntersection}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <div className="flex h-full gap-4 overflow-x-auto overflow-y-hidden pb-4">
            {CARD_STATUSES.map((status) => {
              const initialCards = visibleColumns[status] || [];
              const totalCount = columnsMeta[status]?.total_filtered;
              const sharedProps = {
                status,
                totalCount,
                cardTypeFacets: columnsMeta[status]?.facets.card_type,
                activeCardTypes: cardTypeFilters[status],
                onToggleCardType: (type: KanbanCardFilterType) => toggleCardTypeFilter(status, type),
                onCardClick: handleCardClick,
                onAddCard: handleAddCard,
                canAddCard: canCreateCard,
                canDragCard: canStartCardDrag,
                nameMap,
                cognitiveBadges,
              };

              if (activeViewAllStatus === status) {
                return (
                  <KanbanColumnPage
                    key={status}
                    {...sharedProps}
                    boardId={boardId}
                    query={columnsQuery}
                    initialCards={initialCards}
                    onItemsChange={handleColumnPageItems}
                    onCollapse={collapseColumnPage}
                  />
                );
              }

              return (
                <KanbanColumn
                  key={status}
                  {...sharedProps}
                  cards={initialCards}
                  canViewAll={(totalCount ?? initialCards.length) > initialCards.length}
                  onViewAll={() => handleViewAll(status)}
                />
              );
            })}
          </div>

          <DragOverlay>
            {activeCard && (
              <div className="kanban-card shadow-lg rotate-2">
                <h4 className="font-medium text-sm">{activeCard.title}</h4>
              </div>
            )}
          </DragOverlay>
        </DndContext>
      </div>

      {/* Card Detail Modal */}
      <CardModal boardId={boardId} />

      {/* Create Card Modal */}
      {createCardStatus && (
        <CreateCardModal
          boardId={boardId}
          initialStatus={createCardStatus}
          onClose={() => setCreateCardStatus(null)}
        />
      )}

      {/* Cancellation justification modal (ITEM 17) — drop on the Cancelled column */}
      <CancellationReasonDialog
        open={!!cancelPending}
        entityLabel="card"
        onConfirm={handleCancelSubmit}
        onCancel={() => setCancelPending(null)}
      />

      {/* Execution report modal — shown when moving execution work to Validation/Done */}
      {conclusionPending && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-lg flex flex-col">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Execution Report Required</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Provide the executor report before moving this card to {conclusionTargetLabel}.
              </p>
            </div>
            <div className="px-6 py-4 max-h-[60vh] overflow-y-auto">
              <textarea
                value={conclusionText}
                onChange={(e) => setConclusionText(e.target.value)}
                placeholder={"## Implementation Summary\n\n### Changes\n- ...\n\n### Decisions\n- ...\n\n### Testing\n- ...\n\n### Follow-ups\n- ..."}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
                rows={8}
                autoFocus
              />
              {!conclusionText.trim() && (
                <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">Conclusion is required to move to {conclusionTargetLabel}</p>
              )}
              {/* Completeness metric */}
              <div className="mt-4">
                <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2">
                  Completeness
                  <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${
                    conclusionCompleteness >= 90 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                    : conclusionCompleteness >= 70 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                    : conclusionCompleteness >= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                    : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                  }`}>{conclusionCompleteness}%</span>
                </label>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={conclusionCompleteness}
                  onChange={(e) => setConclusionCompleteness(Number(e.target.value))}
                  className="w-full mt-1"
                />
                <textarea
                  value={conclusionCompletenessJustification}
                  onChange={(e) => setConclusionCompletenessJustification(e.target.value)}
                  placeholder="Justify the completeness score..."
                  className="w-full mt-1 px-3 py-2 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
                  rows={2}
                />
              </div>
              {/* Drift metric */}
              <div className="mt-3">
                <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2">
                  Drift
                  <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${
                    conclusionDrift <= 10 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                    : conclusionDrift <= 25 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                    : conclusionDrift <= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                    : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                  }`}>{conclusionDrift}%</span>
                </label>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={conclusionDrift}
                  onChange={(e) => setConclusionDrift(Number(e.target.value))}
                  className="w-full mt-1"
                />
                <textarea
                  value={conclusionDriftJustification}
                  onChange={(e) => setConclusionDriftJustification(e.target.value)}
                  placeholder="Justify the drift score..."
                  className="w-full mt-1 px-3 py-2 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
                  rows={2}
                />
              </div>
              {/* AC-16: the shared editor renders collapsed with its own
                  internal scroll — the modal keeps max-w-lg. */}
              <ImpactEvidenceEditor
                draft={conclusionImpactDraft}
                onChange={setConclusionImpactDraft}
              />
              {conclusionGateError && (
                <div
                  className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
                  data-testid="impact-evidence-gate-error"
                >
                  <p>{conclusionGateError.message}</p>
                  {conclusionGateError.remediation && (
                    // The backend already says exactly what to do; showing only
                    // the rejection wastes it and makes the author guess.
                    <p
                      className="mt-1.5 border-t border-red-200 pt-1.5 dark:border-red-800/70"
                      data-testid="impact-evidence-gate-remediation"
                    >
                      <span className="font-semibold">How to fix: </span>
                      {conclusionGateError.remediation}
                    </p>
                  )}
                </div>
              )}
            </div>
            <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2">
              <button
                onClick={() => { setConclusionPending(null); resetConclusionFields(); }}
                className="btn btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleConclusionSubmit}
                disabled={!conclusionText.trim() || !conclusionCompletenessJustification.trim() || !conclusionDriftJustification.trim()}
                className={`btn ${conclusionText.trim() && conclusionCompletenessJustification.trim() && conclusionDriftJustification.trim() ? 'btn-primary' : 'btn-secondary opacity-50 cursor-not-allowed'}`}
              >
                Complete & Move to {conclusionTargetLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
