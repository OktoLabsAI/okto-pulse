import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, MinusCircle, ShieldCheck } from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { getErrorMessage } from '@/lib/getErrorMessage';
import type {
  CurrentQualityAssessment,
  QualityGatePreview,
  QualitySubjectType,
} from '@/types';

type ApplicableGateReason = Exclude<
  QualityGatePreview['reason_code'],
  'not_applicable'
>;

const REASON_LABELS: Record<ApplicableGateReason, string> = {
  ambiguity_gate_disabled: 'Disabled by board policy',
  ambiguity_gate_skipped: 'Skipped by recorded override',
  ambiguity_assessment_stale: 'Blocked — assessment is stale',
  ambiguity_score_exceeds_threshold: 'Blocked — score exceeds threshold',
  ambiguity_gate_ready: 'Ready',
};

function gateReasonLabel(
  reason: QualityGatePreview['reason_code'],
): string | null {
  if (reason === 'not_applicable') return null;
  return REASON_LABELS[reason];
}

function GateIcon({ preview }: { preview: QualityGatePreview }) {
  if (!preview.applicable || !preview.enabled) {
    return <MinusCircle size={16} aria-hidden="true" />;
  }
  if (preview.allowed) {
    return <CheckCircle2 size={16} aria-hidden="true" />;
  }
  return <AlertTriangle size={16} aria-hidden="true" />;
}

export function QualityGatePreviewCard({
  assessment,
  className = '',
}: {
  assessment: CurrentQualityAssessment;
  className?: string;
}) {
  const { gate_preview: preview } = assessment;
  const reasonLabel = gateReasonLabel(preview.reason_code);
  if (!preview.applicable || reasonLabel === null) return null;

  const tone = !preview.enabled
    ? 'border-surface-200 bg-surface-50 text-surface-700 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-200'
    : preview.allowed
      ? 'border-emerald-200 bg-emerald-50/70 text-emerald-800 dark:border-emerald-700/50 dark:bg-emerald-950/30 dark:text-emerald-200'
      : 'border-amber-200 bg-amber-50/70 text-amber-800 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-200';

  return (
    <section
      className={`rounded-lg border p-3 ${tone} ${className}`}
      data-testid="quality-gate-preview"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="flex items-center gap-1.5 text-sm font-semibold">
          <GateIcon preview={preview} />
          Gate preview
        </h4>
        <span
          className="rounded-full bg-white/60 px-2 py-0.5 text-xs font-semibold dark:bg-black/20"
          data-testid="quality-gate-preview-status"
        >
          {reasonLabel}
        </span>
      </div>
      <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
        <div>
          <dt className="inline opacity-70">Score: </dt>
          <dd className="inline font-semibold">{preview.score}</dd>
        </div>
        <div>
          <dt className="inline opacity-70">Threshold: </dt>
          <dd className="inline font-semibold">
            {preview.threshold === null ? '—' : preview.threshold}
          </dd>
        </div>
        <div>
          <dt className="inline opacity-70">Currentness: </dt>
          <dd className="inline font-semibold">{assessment.currentness}</dd>
        </div>
      </dl>
      {assessment.stale_reasons.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-4 text-xs" data-testid="quality-stale-reasons">
          {assessment.stale_reasons.map((reason) => (
            <li key={reason}>{reason.split('_').join(' ')}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export interface QualityGatePreviewPanelProps {
  subjectType: Extract<QualitySubjectType, 'ideation' | 'refinement'>;
  subjectId: string;
  canRead: boolean;
  refreshKey?: string | number | boolean;
  className?: string;
}

/**
 * Details-tab loader for the ambiguity gate. It never derives a gate result
 * from legacy entity fields: every visual decision comes from gate_preview.
 */
export function QualityGatePreviewPanel({
  subjectType,
  subjectId,
  canRead,
  refreshKey,
  className = '',
}: QualityGatePreviewPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [assessment, setAssessment] = useState<CurrentQualityAssessment | null>(null);
  const [loading, setLoading] = useState(canRead);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!canRead) {
      setAssessment(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void apiRef.current
      .getCurrentQualityAssessment(subjectType, subjectId, 'ambiguity', controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setAssessment(value);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(getErrorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [canRead, refreshKey, subjectId, subjectType]);

  if (!canRead) return null;
  if (loading) {
    return (
      <section
        className={`rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400 ${className}`}
        aria-busy="true"
        data-testid="quality-gate-preview-loading"
      >
        Loading server gate preview…
      </section>
    );
  }
  if (error) {
    return (
      <section
        role="alert"
        className={`rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300 ${className}`}
        data-testid="quality-gate-preview-error"
      >
        Could not load the server gate preview. {error}
      </section>
    );
  }
  if (!assessment) {
    return (
      <section
        className={`rounded-lg border border-surface-200 bg-surface-50 p-3 text-surface-700 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-200 ${className}`}
        data-testid="quality-gate-preview-empty"
      >
        <h4 className="flex items-center gap-1.5 text-sm font-semibold">
          <ShieldCheck size={16} aria-hidden="true" /> Gate preview
        </h4>
        <p className="mt-1 text-xs opacity-75">
          No current ambiguity assessment. The gate result is unavailable until an assessment exists.
        </p>
      </section>
    );
  }
  return <QualityGatePreviewCard assessment={assessment} className={className} />;
}

export default QualityGatePreviewPanel;
