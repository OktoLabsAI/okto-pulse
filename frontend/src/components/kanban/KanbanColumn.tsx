/**
 * KanbanColumn - Column component for the Kanban board
 */

import {
  SortableContext,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { useDroppable } from '@dnd-kit/core';
import { Bug, FlaskConical, ListChecks, Plus } from 'lucide-react';
import type { CardSummary, CardStatus } from '@/types';
import { STATUS_LABELS } from '@/types';
import type { KGCognitivePendingBadgeView } from '@/services/kg-health-api';
import { KanbanCard } from './KanbanCard';

interface KanbanColumnProps {
  status: CardStatus;
  cards: CardSummary[];
  onCardClick: (cardId: string) => void;
  onAddCard: (status: CardStatus) => void;
  nameMap: Record<string, string>;
  /** KG-03.6 — read-only cognitive badges keyed by source_ref.
   * Resolved at the KanbanBoard level in ONE batch HTTP request and
   * passed down so per-card rendering needs no extra fetch. */
  cognitiveBadges?: Record<string, KGCognitivePendingBadgeView>;
}

const columnColors: Record<CardStatus, string> = {
  not_started: 'border-t-gray-400',
  started: 'border-t-blue-500',
  in_progress: 'border-t-amber-500',
  validation: 'border-t-violet-500',
  on_hold: 'border-t-red-500',
  done: 'border-t-green-500',
  cancelled: 'border-t-gray-500',
};

type KanbanCardCounterType = 'task' | 'test' | 'bug';

interface KanbanCardTypeCounts {
  total: number;
  task: number;
  test: number;
  bug: number;
}

function normalizeKanbanCardType(cardType: CardSummary['card_type'] | { value?: string } | null | undefined): KanbanCardCounterType {
  if (!cardType) return 'task';
  if (typeof cardType === 'object') return normalizeKanbanCardType(cardType.value as CardSummary['card_type']);
  const normalized = String(cardType).replace(/^CardType\./i, '').toLowerCase();
  if (normalized === 'test') return 'test';
  if (normalized === 'bug') return 'bug';
  return 'task';
}

export function deriveKanbanCardTypeCounts(cards: CardSummary[]): KanbanCardTypeCounts {
  return cards.reduce<KanbanCardTypeCounts>(
    (counts, card) => {
      counts.total += 1;
      counts[normalizeKanbanCardType(card.card_type)] += 1;
      return counts;
    },
    { total: 0, task: 0, test: 0, bug: 0 },
  );
}

export function KanbanColumn({ status, cards, onCardClick, onAddCard, nameMap, cognitiveBadges }: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });
  const counts = deriveKanbanCardTypeCounts(cards);

  return (
    <div
      ref={setNodeRef}
      data-tour-id={status === 'validation' ? 'tasks.validation.column' : undefined}
      className={`kanban-column h-full min-h-0 border-t-4 ${columnColors[status]} transition-all duration-200 ${
        isOver ? 'ring-2 ring-blue-400 ring-inset bg-blue-50/50 dark:bg-blue-900/20' : ''
      }`}
    >
      {/* Header */}
      <div className="mb-3 shrink-0">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="truncate font-display text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                {STATUS_LABELS[status]}
              </h3>
              <span
                className="inline-flex shrink-0 items-center rounded bg-gray-200 px-1.5 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-600 dark:text-gray-200"
                title={`${counts.total} total cards`}
                aria-label={`${counts.total} total cards`}
              >
                {counts.total}
              </span>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-1.5 text-[10px] font-semibold">
              <span
                className="inline-flex min-w-0 items-center justify-between gap-1 rounded-md bg-slate-100 px-1.5 py-1 text-slate-600 dark:bg-slate-800/80 dark:text-slate-300"
                title={`${counts.task} task cards`}
                aria-label={`${counts.task} task cards`}
              >
                <span className="inline-flex min-w-0 items-center gap-1">
                  <ListChecks size={11} className="shrink-0" />
                  <span className="truncate">Task</span>
                </span>
                <span>{counts.task}</span>
              </span>
              <span
                className="inline-flex min-w-0 items-center justify-between gap-1 rounded-md bg-purple-100 px-1.5 py-1 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300"
                title={`${counts.test} test cards`}
                aria-label={`${counts.test} test cards`}
              >
                <span className="inline-flex min-w-0 items-center gap-1">
                  <FlaskConical size={11} className="shrink-0" />
                  <span className="truncate">Test</span>
                </span>
                <span>{counts.test}</span>
              </span>
              <span
                className="inline-flex min-w-0 items-center justify-between gap-1 rounded-md bg-red-100 px-1.5 py-1 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                title={`${counts.bug} bug cards`}
                aria-label={`${counts.bug} bug cards`}
              >
                <span className="inline-flex min-w-0 items-center gap-1">
                  <Bug size={11} className="shrink-0" />
                  <span className="truncate">Bug</span>
                </span>
                <span>{counts.bug}</span>
              </span>
            </div>
          </div>
          <button
            onClick={() => onAddCard(status)}
            className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
            title="Add card"
          >
            <Plus size={16} />
          </button>
        </div>
      </div>

      {/* Cards area */}
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        <SortableContext items={cards.map((c) => c.id)} strategy={verticalListSortingStrategy}>
          {cards.map((card) => {
            const sourceRef =
              card.card_type === 'test'
                ? `test:${card.id}`
                : card.card_type === 'bug'
                  ? `bug:${card.id}`
                : !card.card_type || card.card_type === 'normal'
                  ? `task:${card.id}`
                  : null;
            return (
              <KanbanCard
                key={card.id}
                card={card}
                onClick={onCardClick}
                nameMap={nameMap}
                cognitiveBadge={
                  sourceRef ? cognitiveBadges?.[sourceRef] : undefined
                }
              />
            );
          })}
        </SortableContext>

        {/* Empty state / drop placeholder */}
        {cards.length === 0 && (
          <div
            className={`flex items-center justify-center rounded-lg border-2 border-dashed py-10 text-sm transition-colors ${
              isOver
                ? 'border-blue-400 bg-blue-50 text-blue-500 dark:bg-blue-900/30 dark:border-blue-500 dark:text-blue-400'
                : 'border-gray-300 text-gray-400 dark:border-gray-600 dark:text-gray-500'
            }`}
          >
            {isOver ? 'Drop here' : 'No cards'}
          </div>
        )}

        {/* Drop indicator when column has cards and is being hovered */}
        {cards.length > 0 && isOver && (
          <div className="flex items-center justify-center rounded-lg border-2 border-dashed border-blue-400 bg-blue-50 dark:bg-blue-900/30 dark:border-blue-500 py-3 text-sm text-blue-500 dark:text-blue-400">
            Drop here
          </div>
        )}
      </div>
    </div>
  );
}
