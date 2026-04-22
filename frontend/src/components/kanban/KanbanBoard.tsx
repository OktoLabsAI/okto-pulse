/**
 * KanbanBoard - Main board component with drag and drop
 */

import { useEffect, useMemo, useRef, useState } from 'react';
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
import { useDashboardApi } from '@/services/api';
import {
  useDashboardStore,
  useColumns,
  useCurrentBoard,
} from '@/store/dashboard';
import { CARD_STATUSES, type CardStatus, type CardSummary, type SpecSummary } from '@/types';
import { KanbanColumn } from './KanbanColumn';
import { CardModal } from './CardModal';
import { CreateCardModal } from './CreateCardModal';

interface KanbanBoardProps {
  boardId: string;
}

export function KanbanBoard({ boardId }: KanbanBoardProps) {
  const api = useDashboardApi();
  const columns = useColumns();
  const currentBoard = useCurrentBoard();
  const {
    openCardModal,
    optimisticMoveCard,
    setColumns,
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
  // Conclusion modal for Done moves
  const [conclusionPending, setConclusionPending] = useState<{ cardId: string; targetStatus: CardStatus; targetPosition: number } | null>(null);
  const [conclusionText, setConclusionText] = useState('');
  const [conclusionCompleteness, setConclusionCompleteness] = useState(100);
  const [conclusionCompletenessJustification, setConclusionCompletenessJustification] = useState('');
  const [conclusionDrift, setConclusionDrift] = useState(0);
  const [conclusionDriftJustification, setConclusionDriftJustification] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [specFilter, setSpecFilter] = useState<Set<string>>(new Set());
  const specFilterRef = useRef(specFilter);
  specFilterRef.current = specFilter;
  const [specs, setSpecs] = useState<SpecSummary[]>([]);
  const [specSearchOpen, setSpecSearchOpen] = useState(false);
  const [specSearchQuery, setSpecSearchQuery] = useState('');
  const specDropdownRef = useRef<HTMLDivElement>(null);

  // Close spec search on outside click
  useEffect(() => {
    if (!specSearchOpen) return;
    const handler = (e: MouseEvent) => {
      if (specDropdownRef.current && !specDropdownRef.current.contains(e.target as Node)) setSpecSearchOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [specSearchOpen]);

  // Load specs for filter dropdown
  useEffect(() => {
    if (boardId) {
      api.listSpecs(boardId).then(setSpecs).catch(() => {});
    }
  }, [boardId]);

  // Reload columns when showArchived toggle changes
  useEffect(() => {
    if (currentBoard) {
      api.getBoardColumns(currentBoard.id, showArchived).then(setColumns).catch(() => {});
    }
  }, [showArchived]);

  // Collect unique spec_ids from all cards
  const linkedSpecIds = useMemo(() => {
    const ids = new Set<string>();
    Object.values(columns).flat().forEach((c) => { if (c.spec_id) ids.add(c.spec_id); });
    return ids;
  }, [columns]);

  const toggleSpecFilter = (id: string) => {
    setSpecFilter((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // Filter columns by spec
  const filteredColumns = useMemo(() => {
    if (specFilter.size === 0) return columns;
    const hasUnlinked = specFilter.has('__unlinked__');
    const specIds = new Set([...specFilter].filter((s) => s !== '__unlinked__'));
    const filtered: Record<CardStatus, CardSummary[]> = {} as any;
    for (const status of CARD_STATUSES) {
      filtered[status] = (columns[status] || []).filter((c) => {
        if (hasUnlinked && !c.spec_id) return true;
        if (specIds.size > 0 && c.spec_id && specIds.has(c.spec_id)) return true;
        return false;
      });
    }
    return filtered;
  }, [columns, specFilter]);

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
      const card = (columns[status] || []).find((c) => c.id === cardId);
      if (card) {
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

    if (!over || !fromStatus) return;

    const cardId = active.id as string;
    const overId = over.id as string;

    // Determine target status and position
    let targetStatus: CardStatus | undefined;
    let targetPosition: number | undefined;

    if (CARD_STATUSES.includes(overId as CardStatus)) {
      // Dropped on a column header/empty area
      targetStatus = overId as CardStatus;
      targetPosition = (columns[targetStatus] || []).length;
    } else {
      // Dropped on another card — find which column it's in
      for (const status of CARD_STATUSES) {
        const columnCards = columns[status] || [];
        const overIndex = columnCards.findIndex((c) => c.id === overId);
        if (overIndex !== -1) {
          targetStatus = status;
          targetPosition = overIndex;
          break;
        }
      }
    }

    if (targetStatus === undefined || targetPosition === undefined) return;
    if (targetStatus === fromStatus && cardId === overId) return;

    // Validation gate: when card has validation_required and user tries to
    // drag directly to Done, intercept and redirect to Validation column.
    // The backend enforces the gate; this is a UX convenience.
    if (targetStatus === 'done') {
      const card = Object.values(columns).flat().find((c) => c.id === cardId);
      if (card?.validations === undefined || card?.validations === null || card.validations.length === 0) {
        // No validation entries — allow normal Done flow.
        // Future: when `validation_required` field is available from the API,
        // check it here and redirect to 'validation' with a toast:
        // toast('Validation gate active. Move to Validation column first.');
      }
      setConclusionPending({ cardId, targetStatus, targetPosition });
      setConclusionText('');
      setConclusionCompleteness(100);
      setConclusionCompletenessJustification('');
      setConclusionDrift(0);
      setConclusionDriftJustification('');
      return;
    }

    // Optimistic update
    optimisticMoveCard(cardId, targetStatus, targetPosition);

    // API call + refresh from server
    try {
      await api.moveCard(cardId, {
        status: targetStatus,
        position: targetPosition,
      });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to move card');
    }
    // Always refresh to ensure sync
    if (currentBoard) {
      const freshColumns = await api.getBoardColumns(currentBoard.id, showArchived);
      setColumns(freshColumns);
    }
  };

  const handleAddCard = (status: CardStatus) => {
    setCreateCardStatus(status);
  };

  const handleConclusionSubmit = async () => {
    if (!conclusionPending || !conclusionText.trim() || !conclusionCompletenessJustification.trim() || !conclusionDriftJustification.trim()) return;
    const { cardId, targetStatus, targetPosition } = conclusionPending;

    optimisticMoveCard(cardId, targetStatus, targetPosition);
    setConclusionPending(null);

    try {
      await api.moveCard(cardId, {
        status: targetStatus,
        position: targetPosition,
        conclusion: conclusionText.trim(),
        completeness: conclusionCompleteness,
        completeness_justification: conclusionCompletenessJustification.trim(),
        drift: conclusionDrift,
        drift_justification: conclusionDriftJustification.trim(),
      });
      toast.success('Card moved to Done');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to move card to Done');
    }
    if (currentBoard) {
      const freshColumns = await api.getBoardColumns(currentBoard.id, showArchived);
      setColumns(freshColumns);
    }
  };

  const handleCardClick = (cardId: string) => {
    openCardModal(cardId);
  };

  return (
    <>
      {/* Spec filter bar */}
      <div className="flex items-center gap-1.5 mb-3 flex-wrap">
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
                  .filter((s) => linkedSpecIds.has(s.id))
                  .filter((s) => !specSearchQuery || s.title.toLowerCase().includes(specSearchQuery.toLowerCase()))
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
                {specs.filter((s) => linkedSpecIds.has(s.id)).filter((s) => !specSearchQuery || s.title.toLowerCase().includes(specSearchQuery.toLowerCase())).length === 0 && (
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

      <DndContext
        sensors={sensors}
        collisionDetection={rectIntersection}
        onDragStart={handleDragStart}
        onDragOver={handleDragOver}
        onDragEnd={handleDragEnd}
      >
        <div className="flex gap-4 overflow-x-auto pb-4 h-full">
          {CARD_STATUSES.map((status) => (
            <KanbanColumn
              key={status}
              status={status}
              cards={filteredColumns[status] || []}
              onCardClick={handleCardClick}
              onAddCard={handleAddCard}
              nameMap={nameMap}
            />
          ))}
        </div>

        <DragOverlay>
          {activeCard && (
            <div className="kanban-card shadow-lg rotate-2">
              <h4 className="font-medium text-sm">{activeCard.title}</h4>
            </div>
          )}
        </DragOverlay>
      </DndContext>

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

      {/* Conclusion Modal — shown when moving to Done */}
      {conclusionPending && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-lg flex flex-col">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Conclusion Required</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Provide a summary of what was accomplished before marking this card as done.
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
                <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">Conclusion is required to move to Done</p>
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
            </div>
            <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2">
              <button
                onClick={() => { setConclusionPending(null); setConclusionText(''); }}
                className="btn btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleConclusionSubmit}
                disabled={!conclusionText.trim() || !conclusionCompletenessJustification.trim() || !conclusionDriftJustification.trim()}
                className={`btn ${conclusionText.trim() && conclusionCompletenessJustification.trim() && conclusionDriftJustification.trim() ? 'btn-primary' : 'btn-secondary opacity-50 cursor-not-allowed'}`}
              >
                Complete & Move to Done
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
