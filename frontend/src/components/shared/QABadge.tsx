import { MessageCircleQuestion } from 'lucide-react';

interface QABadgeProps {
  /** Number of unanswered Q&A items. The badge is omitted when <= 0. */
  count: number | undefined | null;
  /** Kanban cards use a denser chip scale than the list panels. */
  compact?: boolean;
}

/**
 * "N open Q&A" badge — signals that an entity has unanswered questions, so the
 * user knows there are Q&A pending on that card. It disappears once everything is
 * answered (count <= 0). The amber tone marks it as a pending item that wants
 * attention. The default variant matches the list-panel rounded-full pill; the
 * `compact` variant matches the kanban card's smaller chip scale.
 */
export function QABadge({ count, compact = false }: QABadgeProps) {
  if (!count || count <= 0) return null;

  const title = `${count} unanswered question${count === 1 ? '' : 's'}`;

  if (compact) {
    return (
      <span
        title={title}
        data-testid="qa-open-badge"
        className="inline-flex items-center gap-0.5 shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
      >
        <MessageCircleQuestion size={11} className="shrink-0" />
        {count}
      </span>
    );
  }

  return (
    <span
      title={title}
      data-testid="qa-open-badge"
      className="inline-flex items-center gap-1 shrink-0 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
    >
      <MessageCircleQuestion size={12} className="shrink-0" />
      {count} open Q&amp;A
    </span>
  );
}
