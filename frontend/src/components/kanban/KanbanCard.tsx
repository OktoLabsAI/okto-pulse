/**
 * KanbanCard - Individual card component
 */

import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { format } from 'date-fns';
import { enUS } from 'date-fns/locale';
import { Bug, Calendar, GripVertical, FileText, AlertCircle, Check } from 'lucide-react';
import type { CardSummary } from '@/types';
import { PRIORITY_COLORS, PRIORITY_LABELS, BUG_SEVERITY_LABELS, BUG_SEVERITY_COLORS } from '@/types';

interface KanbanCardProps {
  card: CardSummary;
  onClick: (cardId: string) => void;
  nameMap: Record<string, string>;
}

function displayName(id: string, nameMap: Record<string, string>): string {
  if (nameMap[id]) return nameMap[id];
  if (id.startsWith('user_')) return 'User';
  return id.slice(0, 8);
}

export function KanbanCard({ card, onClick, nameMap }: KanbanCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: card.id, disabled: !!card.archived });

  const isBug = card.card_type === 'bug';

  const priorityColor = card.priority && card.priority !== 'none'
    ? PRIORITY_COLORS[card.priority]?.borderColor
    : '';

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    ...(priorityColor && !isBug ? { borderRight: `4px solid ${priorityColor}` } : {}),
    ...(isBug ? { borderLeft: '4px solid #ef4444' } : {}),
  };

  const formattedDueDate = card.due_date
    ? format(new Date(card.due_date), 'dd MMM', { locale: enUS })
    : null;

  const isOverdue =
    card.due_date && new Date(card.due_date) < new Date() && card.status !== 'done';

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`kanban-card ${isDragging ? 'dragging' : ''} ${isBug ? 'border-red-300 dark:border-red-500/40' : ''} ${card.archived ? 'opacity-50' : ''}`}
      {...attributes}
    >
      <div className="flex items-start gap-2">
        <div
          className="mt-0.5 py-2 px-1 -ml-1 cursor-grab text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded hover:bg-gray-100 dark:hover:bg-gray-600 self-stretch flex items-center"
          {...listeners}
        >
          <GripVertical size={14} />
        </div>
        <div className="flex-1 min-w-0 cursor-pointer" onClick={() => onClick(card.id)}>
          <div className="flex items-center gap-1.5">
            {isBug && (
              <span className="shrink-0 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-bold bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 uppercase tracking-wide">
                <Bug size={10} />
                bug
              </span>
            )}
            <h4 className="font-medium text-sm text-gray-900 dark:text-gray-100 truncate">
              {card.title}
            </h4>
            {card.spec_id && (
              <span
                className="shrink-0 inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[10px] font-medium bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300"
                title="Linked to a spec"
              >
                <FileText size={10} />
                spec
              </span>
            )}
            {card.archived && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">archived</span>
            )}
          </div>

          {card.description && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
              {card.description}
            </p>
          )}

          {/* Priority/Severity badge + Labels */}
          {(card.priority && card.priority !== 'none') || (card.labels && card.labels.length > 0) || (isBug && card.severity) ? (
            <div className="flex flex-wrap gap-1 mt-2">
              {isBug && card.severity && (
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${BUG_SEVERITY_COLORS[card.severity].badge} ${BUG_SEVERITY_COLORS[card.severity].dark_badge}`}>
                  {BUG_SEVERITY_LABELS[card.severity]}
                </span>
              )}
              {!isBug && card.priority && card.priority !== 'none' && (
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${PRIORITY_COLORS[card.priority].badge} ${PRIORITY_COLORS[card.priority].dark_badge}`}>
                  {PRIORITY_LABELS[card.priority]}
                </span>
              )}
              {card.labels?.map((label, idx) => (
                <span
                  key={idx}
                  className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                >
                  {label}
                </span>
              ))}
            </div>
          ) : null}

          {/* Bug: test task indicator */}
          {isBug && (
            <div className="flex gap-1 mt-1.5">
              {(card.linked_test_task_ids?.length ?? 0) > 0 ? (
                <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 inline-flex items-center gap-0.5">
                  <Check size={9} />
                  {card.linked_test_task_ids!.length} test task linked
                </span>
              ) : (
                <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 inline-flex items-center gap-0.5">
                  <AlertCircle size={9} />
                  No test task — blocked from in_progress
                </span>
              )}
            </div>
          )}

          {/* Completeness & Drift badges for done cards */}
          {card.conclusions && card.conclusions.length > 0 && (() => {
            const last = card.conclusions[card.conclusions.length - 1];
            const cPct = last.completeness ?? 100;
            const dPct = last.drift ?? 0;
            const cColor = cPct >= 90 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
              : cPct >= 70 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
              : cPct >= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
              : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
            const dColor = dPct <= 10 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
              : dPct <= 25 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
              : dPct <= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
              : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
            return (
              <div className="flex gap-1 mt-1.5">
                <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${cColor}`} title={`Completeness: ${cPct}%`}>
                  {cPct}% complete
                </span>
                <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${dColor}`} title={`Drift: ${dPct}%`}>
                  {dPct}% drift
                </span>
              </div>
            );
          })()}

          {/* Footer: people badges + due date */}
          <div className="flex items-center justify-between mt-2 text-xs">
            {/* People badges — distinct for Creator vs Assignee */}
            <div className="flex items-center gap-1 flex-wrap">
              {card.created_by && (
                <span
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300 text-[10px] font-medium"
                  title={`Creator: ${card.created_by}`}
                >
                  C: {displayName(card.created_by, nameMap)}
                </span>
              )}
              {card.assignee_id && (
                <span
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300 text-[10px] font-medium"
                  title={`Assignee: ${card.assignee_id}`}
                >
                  A: {displayName(card.assignee_id, nameMap)}
                </span>
              )}
            </div>

            <div className="flex items-center gap-2 shrink-0">
              {formattedDueDate && (
                <span
                  className={`flex items-center gap-1 ${
                    isOverdue ? 'text-red-500' : 'text-gray-400'
                  }`}
                >
                  <Calendar size={12} />
                  {formattedDueDate}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
