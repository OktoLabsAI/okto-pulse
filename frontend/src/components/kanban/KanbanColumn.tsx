/**
 * KanbanColumn - Column component for the Kanban board
 */

import {
  SortableContext,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { useDroppable } from '@dnd-kit/core';
import { Plus } from 'lucide-react';
import type { CardSummary, CardStatus } from '@/types';
import { STATUS_LABELS } from '@/types';
import { KanbanCard } from './KanbanCard';

interface KanbanColumnProps {
  status: CardStatus;
  cards: CardSummary[];
  onCardClick: (cardId: string) => void;
  onAddCard: (status: CardStatus) => void;
  nameMap: Record<string, string>;
}

const columnColors: Record<CardStatus, string> = {
  not_started: 'border-t-gray-400',
  started: 'border-t-blue-500',
  in_progress: 'border-t-amber-500',
  on_hold: 'border-t-red-500',
  done: 'border-t-green-500',
  cancelled: 'border-t-gray-500',
};

export function KanbanColumn({ status, cards, onCardClick, onAddCard, nameMap }: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });

  return (
    <div
      ref={setNodeRef}
      className={`kanban-column border-t-4 ${columnColors[status]} transition-all duration-200 ${
        isOver ? 'ring-2 ring-blue-400 ring-inset bg-blue-50/50 dark:bg-blue-900/20' : ''
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <h3 className="kanban-column-header text-gray-700 dark:text-gray-200">
            {STATUS_LABELS[status]}
          </h3>
          <span className="text-xs bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 px-1.5 py-0.5 rounded">
            {cards.length}
          </span>
        </div>
        <button
          onClick={() => onAddCard(status)}
          className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
          title="Add card"
        >
          <Plus size={16} />
        </button>
      </div>

      {/* Cards area */}
      <div className="space-y-2 flex-1">
        <SortableContext items={cards.map((c) => c.id)} strategy={verticalListSortingStrategy}>
          {cards.map((card) => (
            <KanbanCard key={card.id} card={card} onClick={onCardClick} nameMap={nameMap} />
          ))}
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
