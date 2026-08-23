import { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Check,
  Plus,
  RefreshCw,
  Shield,
  Trash2,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useDashboardApi } from '@/services/api';
import type {
  BoardSettings,
  ChecklistSpecState,
  QualityFindingAnchorType,
  SpecValidationMetric,
  SpecValidationPinpoint,
  SpecValidationSubmitPayload,
  SpecValidationSubmitResponse,
  ValidationSubmissionFence,
} from '@/types';
import { ValidationErrorDisplay } from './ValidationErrorDisplay';

interface SubmitSpecValidationModalProps {
  specId: string;
  specTitle: string;
  boardId: string;
  specVersion: number;
  specEdition?: number;
  settings: BoardSettings;
  canReadChecklist: boolean;
  canExecuteChecklist: boolean;
  onClose: () => void;
  onSubmitted: (result: SpecValidationSubmitResponse) => void;
}

const MIN_JUSTIFICATION_LENGTH = 10;

const METRIC_DEFINITIONS: Array<{
  metric: SpecValidationMetric;
  label: string;
  description: string;
  direction: 'min' | 'max';
  thresholdKey:
    | 'min_spec_confidence'
    | 'min_spec_clarity'
    | 'min_spec_assertiveness'
    | 'min_spec_decidability'
    | 'max_spec_ambiguity';
  fallback: number;
}> = [
  {
    metric: 'confidence',
    label: 'Confidence',
    description: "The evaluator's confidence in this assessment.",
    direction: 'min',
    thresholdKey: 'min_spec_confidence',
    fallback: 70,
  },
  {
    metric: 'clarity',
    label: 'Clarity',
    description: 'How clearly the Spec defines the problem, solution and requirements.',
    direction: 'min',
    thresholdKey: 'min_spec_clarity',
    fallback: 80,
  },
  {
    metric: 'assertiveness',
    label: 'Assertiveness',
    description: 'How direct, measurable and testable the statements are.',
    direction: 'min',
    thresholdKey: 'min_spec_assertiveness',
    fallback: 80,
  },
  {
    metric: 'decidability',
    label: 'Decidability',
    description: 'How well the Spec provides concrete parameters for implementation decisions.',
    direction: 'min',
    thresholdKey: 'min_spec_decidability',
    fallback: 80,
  },
  {
    metric: 'ambiguity',
    label: 'Ambiguity',
    description: 'How much room remains for conflicting interpretations. Lower is better.',
    direction: 'max',
    thresholdKey: 'max_spec_ambiguity',
    fallback: 30,
  },
];

const ANCHOR_OPTIONS: Array<{ value: QualityFindingAnchorType; label: string }> = [
  { value: 'whole_artifact', label: 'Whole Spec' },
  { value: 'field', label: 'Field' },
  { value: 'structured_child', label: 'Structured item' },
  { value: 'qa', label: 'Q&A item' },
];

interface MetricDraft {
  score: number;
  justification: string;
}

interface PinpointDraft extends SpecValidationPinpoint {
  key: string;
}

function newPinpoint(): PinpointDraft {
  return {
    key: `pinpoint-${crypto.randomUUID()}`,
    metric: 'clarity',
    anchor_type: 'whole_artifact',
    anchor_ref: null,
    detail: '',
  };
}

export function SubmitSpecValidationModal({
  specId,
  specTitle,
  boardId,
  specVersion,
  specEdition,
  settings,
  canReadChecklist,
  canExecuteChecklist,
  onClose,
  onSubmitted,
}: SubmitSpecValidationModalProps) {
  const currentSpecEdition = specEdition ?? 1;
  const api = useDashboardApi();
  const thresholds = useMemo(() => Object.fromEntries(
    METRIC_DEFINITIONS.map((definition) => [
      definition.metric,
      settings[definition.thresholdKey] ?? definition.fallback,
    ]),
  ) as Record<SpecValidationMetric, number>, [settings]);
  const [metrics, setMetrics] = useState<Record<SpecValidationMetric, MetricDraft>>(
    () => Object.fromEntries(METRIC_DEFINITIONS.map((definition) => [
      definition.metric,
      { score: settings[definition.thresholdKey] ?? definition.fallback, justification: '' },
    ])) as Record<SpecValidationMetric, MetricDraft>,
  );
  const [pinpoints, setPinpoints] = useState<PinpointDraft[]>([]);
  const [recommendation, setRecommendation] = useState<'approve' | 'reject' | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [checklistState, setChecklistState] =
    useState<ChecklistSpecState | null>(null);
  const [submissionFence, setSubmissionFence] =
    useState<ValidationSubmissionFence | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [readinessRefreshKey, setReadinessRefreshKey] = useState(0);

  useEscapeToClose(onClose, { canClose: !submitting, priority: 10 });

  useEffect(() => {
    let cancelled = false;
    if (!canReadChecklist) {
      setChecklistState(null);
      setSubmissionFence(null);
      setReadinessLoading(false);
      setSubmissionError(
        'Validation readiness cannot be verified with the current permissions.',
      );
      return () => { cancelled = true; };
    }
    setReadinessLoading(true);
    setSubmissionError(null);
    Promise.all([
      api.getSpecChecklistState(boardId, specId),
      api.getValidationCycle('spec', specId, { includePrevious: false }),
    ])
      .then(([resolvedChecklist, cycle]) => {
        if (cancelled) return;
        const fence = cycle.subject_type === 'spec'
          ? cycle.submission_fence
          : undefined;
        if (
          cycle.subject_type !== 'spec'
          || cycle.subject_id !== specId
          || cycle.edition !== currentSpecEdition
          || !cycle.visible_sections.includes('spec_validation')
          || !fence
          || fence.expected_validation_edition
            !== currentSpecEdition
          || fence.expected_subject_version !== specVersion
        ) {
          throw new Error(
            'The validation cycle changed. Refresh the Spec before submitting.',
          );
        }
        setChecklistState(resolvedChecklist);
        setSubmissionFence(fence);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setChecklistState(null);
        setSubmissionFence(null);
        setSubmissionError(
          error instanceof Error
            ? error.message
            : 'Validation readiness could not be verified.',
        );
      })
      .finally(() => {
        if (!cancelled) setReadinessLoading(false);
      });
    return () => { cancelled = true; };
  }, [
    api,
    boardId,
    canReadChecklist,
    currentSpecEdition,
    readinessRefreshKey,
    specId,
    specVersion,
  ]);

  const checklistReady =
    checklistState !== null
    && (
      checklistState.binding.mode !== 'blocking'
      || (
        checklistState.subject.spec_edition === currentSpecEdition
        && checklistState.current_receipt?.spec_edition === currentSpecEdition
        && checklistState.gate.allowed
      )
    );
  const fenceReady = submissionFence !== null;
  const justificationsReady = METRIC_DEFINITIONS.every(
    ({ metric }) => metrics[metric].justification.trim().length
      >= MIN_JUSTIFICATION_LENGTH,
  );
  const pinpointsReady = pinpoints.every((pinpoint) => (
    pinpoint.detail.trim().length > 0
    && (
      pinpoint.anchor_type === 'whole_artifact'
      || Boolean(pinpoint.anchor_ref?.trim())
    )
  ));
  const violations = METRIC_DEFINITIONS.filter(({ metric, direction }) => (
    direction === 'min'
      ? metrics[metric].score < thresholds[metric]
      : metrics[metric].score > thresholds[metric]
  ));
  const outcomePasses = violations.length === 0 && recommendation === 'approve';
  const canSubmit =
    justificationsReady
    && pinpointsReady
    && recommendation !== null
    && checklistReady
    && fenceReady
    && !readinessLoading
    && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit || !submissionFence) return;
    setSubmitting(true);
    setSubmissionError(null);
    const payload: SpecValidationSubmitPayload = {
      expected_validation_edition:
        submissionFence.expected_validation_edition,
      expected_spec_version: submissionFence.expected_subject_version,
      expected_head_revision: submissionFence.expected_head_revision,
      confidence: metrics.confidence.score,
      confidence_justification: metrics.confidence.justification.trim(),
      clarity: metrics.clarity.score,
      clarity_justification: metrics.clarity.justification.trim(),
      assertiveness: metrics.assertiveness.score,
      assertiveness_justification: metrics.assertiveness.justification.trim(),
      decidability: metrics.decidability.score,
      decidability_justification: metrics.decidability.justification.trim(),
      ambiguity: metrics.ambiguity.score,
      ambiguity_justification: metrics.ambiguity.justification.trim(),
      pinpoints: pinpoints.map(({ key: _key, ...pinpoint }) => ({
        ...pinpoint,
        anchor_ref: pinpoint.anchor_type === 'whole_artifact'
          ? null
          : pinpoint.anchor_ref?.trim() || null,
        detail: pinpoint.detail.trim(),
      })),
      recommendation: recommendation!,
    };
    try {
      const result = await api.submitSpecValidation(specId, payload);
      if (!result.is_current || result.validation_edition !== currentSpecEdition) {
        throw new Error(
          'The submitted validation was not accepted as the current edition result.',
        );
      }
      toast.success('Spec validation recorded');
      onSubmitted(result);
      onClose();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Submission failed';
      setSubmissionError(message);
      toast.error('Validation blocked');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-xl bg-white shadow-2xl dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <div>
            <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-white">
              <Shield size={16} aria-hidden="true" />
              Validate Spec
            </h2>
            <p className="mt-0.5 max-w-md truncate text-xs text-gray-500 dark:text-gray-400">
              {specTitle} · Edition {currentSpecEdition}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            aria-label="Close validation form"
            className="rounded p-1 text-gray-400 hover:text-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 disabled:opacity-50 dark:hover:text-gray-300"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="space-y-5 px-6 py-5">
          <section
            className={`rounded-lg border p-3 text-xs ${
              readinessLoading
                ? 'border-surface-200 bg-surface-50 text-surface-500 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-400'
                : checklistReady && fenceReady
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/25 dark:text-emerald-200'
                  : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-200'
            }`}
            data-testid="spec-validation-checklist-readiness"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="font-semibold">
                  {readinessLoading
                    ? 'Checking validation readiness…'
                    : checklistReady && fenceReady
                      ? canExecuteChecklist
                        ? 'Current edition is ready'
                        : 'Current edition result is ready'
                      : 'Current edition needs attention'}
                </p>
                {!readinessLoading && checklistState && (
                  <p className="mt-0.5 text-[11px] opacity-80">
                    {checklistState.binding.mode === 'blocking'
                      ? checklistReady
                        ? 'The required checklist result is available for this edition.'
                        : 'No passing checklist result is available for this edition.'
                      : 'The checklist is advisory for this board.'}
                  </p>
                )}
              </div>
              <button
                type="button"
                aria-label="Refresh validation readiness"
                onClick={() => setReadinessRefreshKey((value) => value + 1)}
                disabled={readinessLoading || submitting}
                className="rounded p-1 opacity-70 hover:bg-black/5 hover:opacity-100 disabled:opacity-40 dark:hover:bg-white/10"
              >
                <RefreshCw
                  size={13}
                  className={readinessLoading ? 'animate-spin' : ''}
                  aria-hidden="true"
                />
              </button>
            </div>
          </section>

          <section className="space-y-3" aria-labelledby="spec-validation-metrics-title">
            <div>
              <h3 id="spec-validation-metrics-title" className="text-sm font-semibold text-gray-900 dark:text-white">
                Assessment scores
              </h3>
              <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                Every score requires a concise justification. All metrics use a 0–100 scale.
              </p>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              {METRIC_DEFINITIONS.map((definition) => (
                <MetricAssessmentField
                  key={definition.metric}
                  definition={definition}
                  value={metrics[definition.metric]}
                  threshold={thresholds[definition.metric]}
                  disabled={submitting}
                  onChange={(patch) => setMetrics((current) => ({
                    ...current,
                    [definition.metric]: {
                      ...current[definition.metric],
                      ...patch,
                    },
                  }))}
                />
              ))}
            </div>
          </section>

          <section className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700" aria-labelledby="spec-validation-pinpoints-title">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h3 id="spec-validation-pinpoints-title" className="text-sm font-semibold text-gray-900 dark:text-white">
                  Pinpoint findings
                </h3>
                <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                  Optionally locate a problem and tag it with the metric it affects.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setPinpoints((current) => [...current, newPinpoint()])}
                disabled={submitting}
                className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1.5 text-xs font-semibold text-violet-700 hover:bg-violet-50 disabled:opacity-50 dark:border-violet-700 dark:bg-gray-900 dark:text-violet-200"
              >
                <Plus size={13} aria-hidden="true" /> Add pinpoint
              </button>
            </div>
            {pinpoints.map((pinpoint, index) => (
              <fieldset key={pinpoint.key} className="space-y-3 rounded-lg border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-800/50">
                <legend className="px-1 text-xs font-semibold text-gray-700 dark:text-gray-200">
                  Pinpoint {index + 1}
                </legend>
                <div className="grid gap-3 sm:grid-cols-3">
                  <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                    Metric
                    <select
                      aria-label={`Pinpoint ${index + 1} metric`}
                      value={pinpoint.metric}
                      onChange={(event) => setPinpoints((current) => current.map((item) => item.key === pinpoint.key
                        ? { ...item, metric: event.target.value as SpecValidationMetric }
                        : item))}
                      className="mt-1 block min-h-9 w-full rounded border border-gray-300 bg-white px-2 text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                    >
                      {METRIC_DEFINITIONS.map(({ metric, label }) => <option key={metric} value={metric}>{label}</option>)}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                    Location type
                    <select
                      aria-label={`Pinpoint ${index + 1} location type`}
                      value={pinpoint.anchor_type}
                      onChange={(event) => setPinpoints((current) => current.map((item) => item.key === pinpoint.key
                        ? {
                            ...item,
                            anchor_type: event.target.value as QualityFindingAnchorType,
                            anchor_ref: event.target.value === 'whole_artifact' ? null : item.anchor_ref,
                          }
                        : item))}
                      className="mt-1 block min-h-9 w-full rounded border border-gray-300 bg-white px-2 text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                    >
                      {ANCHOR_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                    Location reference
                    <input
                      aria-label={`Pinpoint ${index + 1} location reference`}
                      value={pinpoint.anchor_ref ?? ''}
                      disabled={pinpoint.anchor_type === 'whole_artifact'}
                      onChange={(event) => setPinpoints((current) => current.map((item) => item.key === pinpoint.key
                        ? { ...item, anchor_ref: event.target.value }
                        : item))}
                      placeholder={pinpoint.anchor_type === 'structured_child' ? 'Requirement ID' : 'Field or item ID'}
                      className="mt-1 block min-h-9 w-full rounded border border-gray-300 bg-white px-2 text-xs text-gray-900 disabled:bg-gray-100 disabled:text-gray-400 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 dark:disabled:bg-gray-800"
                    />
                  </label>
                </div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
                  Finding detail
                  <textarea
                    aria-label={`Pinpoint ${index + 1} detail`}
                    value={pinpoint.detail}
                    onChange={(event) => setPinpoints((current) => current.map((item) => item.key === pinpoint.key
                      ? { ...item, detail: event.target.value }
                      : item))}
                    rows={2}
                    placeholder="Describe the problem at this location"
                    className="mt-1 block w-full rounded border border-gray-300 bg-white p-2 text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                  />
                </label>
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={() => setPinpoints((current) => current.filter((item) => item.key !== pinpoint.key))}
                    className="inline-flex items-center gap-1 text-xs font-medium text-red-600 hover:text-red-700 dark:text-red-400"
                  >
                    <Trash2 size={13} aria-hidden="true" /> Remove pinpoint
                  </button>
                </div>
              </fieldset>
            ))}
          </section>

          <section className="space-y-2 border-t border-gray-200 pt-4 dark:border-gray-700" aria-labelledby="spec-validation-recommendation-title">
            <h3 id="spec-validation-recommendation-title" className="text-sm font-semibold text-gray-900 dark:text-white">
              Recommendation
            </h3>
            <div className="grid grid-cols-2 gap-3">
              {(['approve', 'reject'] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={recommendation === option}
                  onClick={() => setRecommendation(option)}
                  className={`flex min-h-10 items-center justify-center gap-2 rounded-lg border-2 px-3 text-sm font-semibold transition-colors ${
                    recommendation === option
                      ? option === 'approve'
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-200'
                        : 'border-red-500 bg-red-50 text-red-700 dark:bg-red-950/30 dark:text-red-200'
                      : 'border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800'
                  }`}
                >
                  {option === 'approve' ? <Check size={14} /> : <X size={14} />}
                  {option === 'approve' ? 'Approve' : 'Reject'}
                </button>
              ))}
            </div>
          </section>

          <section className={`rounded-lg border p-3 text-xs ${
            outcomePasses
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/25 dark:text-emerald-200'
              : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-200'
          }`} aria-label="Validation outcome preview">
            <p className="font-semibold">
              Outcome preview: {outcomePasses ? 'Pass' : 'Needs attention'}
            </p>
            {violations.length > 0 && (
              <ul className="mt-1 list-inside list-disc space-y-0.5">
                {violations.map(({ metric, label, direction }) => (
                  <li key={metric}>
                    {label} is {metrics[metric].score}; {direction === 'min' ? 'minimum' : 'maximum'} is {thresholds[metric]}.
                  </li>
                ))}
              </ul>
            )}
            {!justificationsReady && (
              <p className="mt-1">Every metric needs a justification of at least {MIN_JUSTIFICATION_LENGTH} characters.</p>
            )}
            {!pinpointsReady && (
              <p className="mt-1">Complete every pinpoint detail and required location reference.</p>
            )}
          </section>

          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            This becomes the current result for Edition {currentSpecEdition}. Earlier results remain under Previous validations.
          </p>

          {submissionError && <ValidationErrorDisplay error={submissionError} />}
          {!checklistReady && !readinessLoading && (
            <div className="rounded border border-amber-200 bg-amber-50 p-2 text-[10px] text-amber-800 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
              Complete the blocking checklist for this edition before submitting validation.
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-200 bg-gray-50 px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-4 py-1.5 text-sm text-gray-700 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-700"
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={!canSubmit}
            className="inline-flex items-center gap-1.5 rounded bg-violet-600 px-4 py-1.5 text-sm text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-gray-400"
          >
            <Check size={14} aria-hidden="true" />
            {submitting ? 'Submitting…' : 'Submit Validation'}
          </button>
        </div>
      </div>
    </div>
  );
}

interface MetricAssessmentFieldProps {
  definition: (typeof METRIC_DEFINITIONS)[number];
  value: MetricDraft;
  threshold: number;
  disabled: boolean;
  onChange: (patch: Partial<MetricDraft>) => void;
}

function MetricAssessmentField({
  definition,
  value,
  threshold,
  disabled,
  onChange,
}: MetricAssessmentFieldProps) {
  const passes = definition.direction === 'min'
    ? value.score >= threshold
    : value.score <= threshold;
  const remaining = Math.max(
    0,
    MIN_JUSTIFICATION_LENGTH - value.justification.trim().length,
  );
  const inputId = `spec-validation-${definition.metric}`;

  return (
    <fieldset
      className={`space-y-2 rounded-lg border p-3 ${
        passes
          ? 'border-emerald-200 bg-emerald-50/40 dark:border-emerald-900 dark:bg-emerald-950/15'
          : 'border-red-200 bg-red-50/40 dark:border-red-900 dark:bg-red-950/15'
      }`}
      data-testid={`spec-validation-metric-${definition.metric}`}
    >
      <legend className="sr-only">{definition.label}</legend>
      <div className="flex items-start justify-between gap-3">
        <div>
          <label htmlFor={inputId} className="text-sm font-semibold text-gray-900 dark:text-white">
            {definition.label}
          </label>
          <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
            {definition.description}
          </p>
        </div>
        <span className="shrink-0 font-mono text-sm font-semibold text-gray-900 dark:text-white">
          {value.score}/100
        </span>
      </div>
      <input
        id={inputId}
        aria-label={`${definition.label} score`}
        type="range"
        min={0}
        max={100}
        value={value.score}
        disabled={disabled}
        onChange={(event) => onChange({ score: Number(event.target.value) })}
        className="w-full accent-violet-500"
      />
      <div className={`flex items-center gap-1 text-[10px] font-medium ${
        passes
          ? 'text-emerald-700 dark:text-emerald-300'
          : 'text-red-700 dark:text-red-300'
      }`}>
        {passes ? <Check size={11} aria-hidden="true" /> : <AlertCircle size={11} aria-hidden="true" />}
        {definition.direction === 'min' ? 'Minimum' : 'Maximum'} {threshold}
      </div>
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
        {definition.label} justification
        <textarea
          aria-label={`${definition.label} justification`}
          value={value.justification}
          disabled={disabled}
          onChange={(event) => onChange({ justification: event.target.value })}
          rows={2}
          placeholder={`Explain the ${definition.label.toLowerCase()} score`}
          className="mt-1 block w-full rounded border border-gray-300 bg-white p-2 text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
        />
      </label>
      <p className={`text-right text-[10px] ${remaining > 0 ? 'text-amber-600 dark:text-amber-300' : 'text-gray-400'}`}>
        {remaining > 0 ? `${remaining} more characters required` : `${value.justification.trim().length} characters`}
      </p>
    </fieldset>
  );
}
