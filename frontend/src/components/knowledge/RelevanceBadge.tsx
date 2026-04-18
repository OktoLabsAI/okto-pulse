/**
 * RelevanceBadge — coloured score chip for KG nodes (spec R3, v0.3.0).
 *
 * Replaces the legacy ValidationStatus chip with a continuous score
 * rendering. Buckets match the spec:
 *   * green (≥ 0.7) — "high_relevance"
 *   * amber (0.3 .. 0.7) — "mid"
 *   * red (< 0.3)   — "low"
 *
 * Pure display component — receives the score as a prop and renders the
 * chip inline. Re-used by NodeDetailPanel, NodePreviewPanel and
 * KGValidationTab so the colour coding stays consistent across the IDE.
 */

import React from 'react';

export interface RelevanceBadgeProps {
  score: number | null | undefined;
  /** Compact variant — tighter padding + no label suffix */
  compact?: boolean;
  className?: string;
}

function bucketFor(score: number): 'high' | 'mid' | 'low' {
  if (score >= 0.7) return 'high';
  if (score >= 0.3) return 'mid';
  return 'low';
}

const BUCKET_STYLES: Record<'high' | 'mid' | 'low', { bg: string; text: string; label: string }> = {
  high: {
    bg: 'bg-emerald-100 dark:bg-emerald-900/40',
    text: 'text-emerald-700 dark:text-emerald-300',
    label: 'high',
  },
  mid: {
    bg: 'bg-amber-100 dark:bg-amber-900/40',
    text: 'text-amber-700 dark:text-amber-300',
    label: 'mid',
  },
  low: {
    bg: 'bg-rose-100 dark:bg-rose-900/40',
    text: 'text-rose-700 dark:text-rose-300',
    label: 'low',
  },
};

export function RelevanceBadge({ score, compact = false, className = '' }: RelevanceBadgeProps) {
  const safe = typeof score === 'number' && Number.isFinite(score) ? score : 0.5;
  const bucket = bucketFor(safe);
  const style = BUCKET_STYLES[bucket];
  const pct = `${Math.round(safe * 100)}%`;
  return (
    <span
      className={[
        'inline-flex items-center gap-1 rounded-full font-medium',
        compact ? 'px-2 py-0.5 text-xs' : 'px-2.5 py-1 text-xs',
        style.bg,
        style.text,
        className,
      ].join(' ')}
      title={`Relevance score: ${safe.toFixed(2)} (${style.label})`}
      data-testid="relevance-badge"
    >
      <span>{pct}</span>
      {!compact && <span className="opacity-70">relevance</span>}
    </span>
  );
}
