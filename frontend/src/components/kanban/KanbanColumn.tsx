/**
 * KanbanColumn - Column component for the Kanban board
 */

import type { ReactNode } from 'react';
import {
  SortableContext,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { useDroppable } from '@dnd-kit/core';
import { Bug, FlaskConical, ListChecks, Plus } from 'lucide-react';
import type { CardSummary, CardStatus, CardType } from '@/types';
import { STATUS_LABELS } from '@/types';
import type { KGCognitivePendingBadgeView } from '@/services/kg-health-api';
import { KanbanCard } from './KanbanCard';

const columnColors: Record<CardStatus, string> = {
  not_started: 'border-t-gray-400',
  started: 'border-t-blue-500',
  in_progress: 'border-t-amber-500',
  validation: 'border-t-violet-500',
  rejected: 'border-t-rose-600',
  on_hold: 'border-t-red-500',
  done: 'border-t-green-500',
  cancelled: 'border-t-gray-500',
};

export type KanbanCardFilterType = 'task' | 'test' | 'bug';

interface KanbanCardTypeCounts {
  total: number;
  task: number;
  test: number;
  bug: number;
}

export function normalizeKanbanCardType(cardType: CardSummary['card_type'] | { value?: string } | null | undefined): KanbanCardFilterType {
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

const DEFAULT_ACTIVE_CARD_TYPES = new Set<KanbanCardFilterType>(['task', 'test', 'bug']);

const CARD_TYPE_TOGGLES = [
  {
    type: 'task',
    label: 'Task',
    icon: ListChecks,
    activeClass: 'bg-slate-100 text-slate-600 dark:bg-slate-800/80 dark:text-slate-300',
  },
  {
    type: 'test',
    label: 'Test',
    icon: FlaskConical,
    activeClass: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  },
  {
    type: 'bug',
    label: 'Bug',
    icon: Bug,
    activeClass: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  },
] as const;

export interface KanbanColumnProps {
  status: CardStatus;
  cards: CardSummary[];
  countCards?: CardSummary[];
  totalCount?: number;
  cardTypeFacets?: Partial<Record<CardType, number>>;
  canViewAll?: boolean;
  onViewAll?: () => void;
  activeCardTypes?: ReadonlySet<KanbanCardFilterType>;
  availableCardTypes?: readonly KanbanCardFilterType[];
  onToggleCardType?: (type: KanbanCardFilterType) => void;
  onCardClick: (cardId: string) => void;
  onAddCard: (status: CardStatus) => void;
  canAddCard?: boolean;
  allowCardCreation?: boolean;
  canAcceptDrop?: boolean;
  canDragCard?: (card: CardSummary) => boolean;
  nameMap: Record<string, string>;
  /** Optional controls rendered after the cards, inside the column scroll area. */
  footer?: ReactNode;
  /** KG-03.6 — read-only cognitive badges keyed by source_ref.
   * Resolved at the KanbanBoard level in ONE batch HTTP request and
   * passed down so per-card rendering needs no extra fetch. */
  cognitiveBadges?: Record<string, KGCognitivePendingBadgeView>;
}

export function KanbanColumn({
  status,
  cards,
  countCards,
  totalCount,
  cardTypeFacets,
  canViewAll = false,
  onViewAll,
  activeCardTypes = DEFAULT_ACTIVE_CARD_TYPES,
  availableCardTypes = CARD_TYPE_TOGGLES.map(({ type }) => type),
  onToggleCardType,
  onCardClick,
  onAddCard,
  canAddCard = true,
  allowCardCreation = true,
  canAcceptDrop = true,
  canDragCard,
  nameMap,
  footer,
  cognitiveBadges,
}: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({
    id: status,
    disabled: !canAcceptDrop,
  });
  const localCounts = deriveKanbanCardTypeCounts(countCards ?? cards);
  const counts: KanbanCardTypeCounts = cardTypeFacets
    ? {
        total: totalCount ?? localCounts.total,
        task: cardTypeFacets.normal ?? 0,
        test: cardTypeFacets.test ?? 0,
        bug: cardTypeFacets.bug ?? 0,
      }
    : {
        ...localCounts,
        total: totalCount ?? localCounts.total,
      };

  return (
    <div
      ref={setNodeRef}
      data-tour-id={
        status === 'validation'
          ? 'tasks.validation.column'
          : status === 'rejected'
            ? 'tasks.rejected.column'
            : undefined
      }
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
            <div className={`mt-2 grid gap-1.5 text-[10px] font-semibold ${
              availableCardTypes.length === 2 ? 'grid-cols-2' : 'grid-cols-3'
            }`}>
              {CARD_TYPE_TOGGLES.filter(({ type }) => (
                availableCardTypes.includes(type)
              )).map(({ type, label, icon: Icon, activeClass }) => {
                const active = activeCardTypes.has(type);
                const count = counts[type];
                return (
                  <button
                    key={type}
                    type="button"
                    onClick={() => onToggleCardType?.(type)}
                    aria-pressed={active}
                    aria-label={`${count} ${type} cards`}
                    title={`${active ? 'Hide' : 'Show'} ${label} cards (${count})`}
                    className={`inline-flex min-w-0 items-center justify-between gap-1 rounded-md px-1.5 py-1 transition-colors ${
                      active
                        ? activeClass
                        : 'bg-gray-100 text-gray-400 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-500 dark:hover:bg-gray-700'
                    } ${onToggleCardType ? 'cursor-pointer' : 'cursor-default'}`}
                  >
                    <span className="inline-flex min-w-0 items-center gap-1">
                      <Icon size={11} className="shrink-0" />
                      <span className="truncate">{label}</span>
                    </span>
                    <span>{count}</span>
                  </button>
                );
              })}
            </div>
          </div>
          {allowCardCreation && (
            <button
              onClick={() => onAddCard(status)}
              disabled={!canAddCard}
              className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600 disabled:cursor-not-allowed disabled:opacity-30 dark:hover:bg-gray-700 dark:hover:text-gray-300"
              title={canAddCard ? 'Add card' : 'Missing card creation permission'}
              aria-label={`Add card to ${STATUS_LABELS[status]}`}
            >
              <Plus size={16} />
            </button>
          )}
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
                canDrag={canDragCard?.(card) ?? true}
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
            {isOver
              ? 'Drop here'
              : status === 'rejected'
                ? 'No cards requiring rework'
                : 'No cards'}
          </div>
        )}

        {/* Drop indicator when column has cards and is being hovered */}
        {cards.length > 0 && isOver && (
          <div className="flex items-center justify-center rounded-lg border-2 border-dashed border-blue-400 bg-blue-50 dark:bg-blue-900/30 dark:border-blue-500 py-3 text-sm text-blue-500 dark:text-blue-400">
            Drop here
          </div>
        )}

        {canViewAll && (
          <button
            type="button"
            onClick={onViewAll}
            aria-label={`View all ${counts.total} cards from ${STATUS_LABELS[status]}`}
            className="w-full rounded-lg border border-dashed border-accent-300 bg-white px-3 py-2 text-xs font-medium text-accent-600 transition-colors hover:bg-accent-50 dark:border-accent-700 dark:bg-gray-800 dark:text-accent-300 dark:hover:bg-accent-950/30"
          >
            View all ({counts.total}) →
          </button>
        )}

        {footer}
      </div>
    </div>
  );
}
