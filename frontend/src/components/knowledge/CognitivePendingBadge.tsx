/**
 * CognitivePendingBadge — read-only first-line card badge (KG-03.6).
 *
 * Renders "Pending cognitive consolidation" when the resolver returned
 * show_badge=true for the source_ref. Renders nothing when the badge
 * is hidden (ineligible entity type, terminal status, not found).
 *
 * NEVER renders mutation affordances. The badge is purely a visual
 * indicator that links to the KG Health panel for context. Cognitive
 * mutation flows through the MCP update tool only (br_2065f80b).
 */

import { BookOpen } from 'lucide-react';

import {
  KG_BADGE_LABEL_ACTIVE,
  type KGCognitivePendingBadgeView,
} from '@/services/kg-health-api';

interface CognitivePendingBadgeProps {
  badge: KGCognitivePendingBadgeView | undefined;
  /** Compact mode reduces the badge to an icon-only chip for narrow
   * kanban surfaces. The label remains in the title attribute for
   * accessibility. */
  compact?: boolean;
  /** Optional className passthrough for parent-driven spacing. */
  className?: string;
}

const STATUS_TONE: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
  in_progress: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
};

export function CognitivePendingBadge({
  badge,
  compact = false,
  className = '',
}: CognitivePendingBadgeProps) {
  if (!badge || !badge.show_badge) {
    return null;
  }
  const tone =
    (badge.status && STATUS_TONE[badge.status]) || STATUS_TONE.pending;
  const ariaLabel = `${KG_BADGE_LABEL_ACTIVE} — ${badge.status ?? 'pending'}`;
  if (compact) {
    return (
      <span
        data-testid="cognitive-pending-badge"
        data-status={badge.status ?? 'pending'}
        aria-label={ariaLabel}
        title={ariaLabel}
        role="status"
        className={`inline-flex items-center justify-center rounded-full px-1.5 py-0.5 ${tone} ${className}`}
      >
        <BookOpen className="h-3 w-3" aria-hidden />
      </span>
    );
  }
  return (
    <span
      data-testid="cognitive-pending-badge"
      data-status={badge.status ?? 'pending'}
      role="status"
      aria-label={ariaLabel}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone} ${className}`}
    >
      <BookOpen className="h-3 w-3" aria-hidden />
      <span>{KG_BADGE_LABEL_ACTIVE}</span>
    </span>
  );
}
