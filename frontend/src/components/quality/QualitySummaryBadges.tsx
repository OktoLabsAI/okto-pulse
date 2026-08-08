import type {
  QualityAssessmentKind,
  QualityAssessmentSummary,
  QualitySummaryMap,
} from '@/types';

type VisibleQualityAssessmentKind = Exclude<
  QualityAssessmentKind,
  'spec_validation'
>;

const KIND_LABELS: Record<VisibleQualityAssessmentKind, string> = {
  ambiguity: 'Ambiguity',
  requirement_lint: 'Requirement lint',
};

const KIND_ORDER: VisibleQualityAssessmentKind[] = [
  'ambiguity',
  'requirement_lint',
];

function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function SummaryBadge({
  kind,
  summary,
}: {
  kind: VisibleQualityAssessmentKind;
  summary: QualityAssessmentSummary;
}) {
  const stale = summary.currentness === 'stale';
  const tone = stale
    ? 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200'
    : 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-200';
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${tone}`}
      data-testid={`quality-summary-${kind}`}
      title={`${KIND_LABELS[kind]}: ${formatScore(summary.score)} (${summary.scale.min}–${summary.scale.max}); ${summary.currentness}; subject v${summary.subject_version}; head r${summary.head_revision}`}
    >
      <span>{KIND_LABELS[kind]}</span>
      <strong>{formatScore(summary.score)}</strong>
      {stale && <span aria-label="stale quality assessment">stale</span>}
    </span>
  );
}

export interface QualitySummaryBadgesProps {
  summaries?: QualitySummaryMap;
  className?: string;
}

/**
 * Renders only server-projected summaries. An omitted map is an authorization
 * signal and must not trigger an N+1 fallback request from an entity card.
 */
export function QualitySummaryBadges({
  summaries,
  className = '',
}: QualitySummaryBadgesProps) {
  if (!summaries) return null;
  const entries = KIND_ORDER.flatMap((kind) => {
    const summary = summaries[kind];
    return summary ? [{ kind, summary }] : [];
  });
  if (entries.length === 0) return null;

  return (
    <div
      className={`flex flex-wrap items-center gap-1.5 ${className}`}
      data-testid="quality-summary-badges"
    >
      {entries.map(({ kind, summary }) => (
        <SummaryBadge key={kind} kind={kind} summary={summary} />
      ))}
    </div>
  );
}

export default QualitySummaryBadges;
