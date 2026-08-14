import { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronUp, X } from 'lucide-react';
import toast from 'react-hot-toast';

import { ValidationCycleStatusBadge } from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useDashboardApi } from '@/services/api';
import { measureValidationWorkspaceInteraction } from '@/services/validation-workspace-telemetry';
import type {
  SpecValidation,
  SpecValidationList,
  SpecValidationPinpoint,
} from '@/types';

interface SpecValidationHistoryPanelProps {
  specId: string;
  refreshKey?: number;
  currentEdition?: number;
  view?: 'all' | 'current' | 'previous';
  /** Avoids a history request when the bounded current summary is available. */
  currentValidation?: SpecValidation | null;
  /** Human-readable Spec content keyed by stable field, child, or Q&A id. */
  anchorTexts?: Readonly<Record<string, string>>;
}

function belongsToCurrentEdition(
  validation: SpecValidation,
  currentEdition?: number,
): boolean {
  if (validation.edition == null) return false;
  if (currentEdition === undefined) return validation.active === true;
  return validation.edition === currentEdition
    && (
      validation.lifecycle_state === 'current'
      || validation.active === true
    );
}

export function SpecValidationHistoryPanel({
  specId,
  refreshKey = 0,
  currentEdition,
  view = 'all',
  currentValidation,
  anchorTexts,
}: SpecValidationHistoryPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [data, setData] = useState<SpecValidationList | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    setOffset(0);
    setData(null);
  }, [currentEdition, specId, view]);

  useEffect(() => {
    if (view === 'current' && currentValidation !== undefined) {
      setData({
        spec_id: specId,
        current_validation_id: currentValidation?.id ?? null,
        validations: currentValidation ? [currentValidation] : [],
      });
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    const request = view === 'current'
      ? apiRef.current.getCurrentSpecValidation(specId, controller.signal)
          .then((summary) => ({
            spec_id: specId,
            current_validation_id: summary.current_validation?.id ?? null,
            validations: summary.current_validation
              ? [summary.current_validation]
              : [],
          }))
      : apiRef.current.listSpecValidations(
          specId,
          view === 'previous'
            ? {
                lifecycleState: 'previous',
                offset,
                limit: 25,
                signal: controller.signal,
              }
            : undefined,
        );
    request
      .then((result) => {
        if (!cancelled) {
          setData((previousData) => offset > 0 && previousData
            ? {
                ...result,
                validations: [
                  ...previousData.validations,
                  ...result.validations,
                ],
              }
            : result);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          toast.error(
            error instanceof Error
              ? error.message
              : 'Failed to load validation results',
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [currentEdition, currentValidation, offset, refreshKey, specId, view]);

  if (loading && !data) {
    return (
      <p role="status" className="p-3 text-xs text-surface-500 dark:text-surface-400">
        Loading validation results…
      </p>
    );
  }

  const validations = data?.validations ?? [];
  const current = validations.filter((validation) =>
    belongsToCurrentEdition(validation, currentEdition)
  );
  const previous = validations.filter((validation) =>
    !belongsToCurrentEdition(validation, currentEdition)
  );
  const visible = view === 'current'
    ? current
    : view === 'previous'
      ? previous
      : [...current, ...previous];

  if (visible.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        {view === 'current'
          ? `No current validation result${currentEdition ? ` for Edition ${currentEdition}` : ''}.`
          : view === 'previous'
            ? 'No previous validation results are available.'
            : 'No validation results are available yet.'}
      </p>
    );
  }

  return (
    <div className="space-y-2" data-testid={`spec-validation-results-${view}`}>
      {visible.map((validation, index) => (
        <ValidationRecord
          key={validation.id}
          validation={validation}
          current={belongsToCurrentEdition(validation, currentEdition)}
          currentEdition={currentEdition}
          attemptNumber={visible.length - index}
          expanded={expandedId === validation.id}
          onToggleExpand={() => setExpandedId(
            expandedId === validation.id ? null : validation.id,
          )}
          anchorTexts={anchorTexts}
        />
      ))}
      {view === 'previous' && data?.has_more && (
        <button
          type="button"
          onClick={() => setOffset((value) => value + 25)}
          disabled={loading}
          className="mt-2 inline-flex min-h-8 items-center rounded-lg border border-surface-300 bg-white px-3 text-xs font-medium text-surface-700 hover:bg-surface-50 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
        >
          {loading ? 'Loading…' : 'Load more previous validations'}
        </button>
      )}
    </div>
  );
}

interface ValidationRecordProps {
  validation: SpecValidation;
  current: boolean;
  currentEdition?: number;
  attemptNumber: number;
  expanded: boolean;
  onToggleExpand: () => void;
  anchorTexts?: Readonly<Record<string, string>>;
}

function ValidationRecord({
  validation,
  current,
  currentEdition,
  attemptNumber,
  expanded,
  onToggleExpand,
  anchorTexts,
}: ValidationRecordProps) {
  const detailsId = useId();
  const isSuccess = validation.outcome !== 'failed';
  const thresholds = validation.resolved_thresholds;
  const formalResult = typeof validation.score === 'number'
    && Boolean(validation.summary?.trim());
  const canonicalDimensions = typeof validation.confidence === 'number'
    && typeof validation.clarity === 'number'
    && typeof validation.assertiveness === 'number'
    && typeof validation.decidability === 'number'
    && typeof validation.ambiguity === 'number';
  const legacyDimensions = typeof validation.completeness === 'number'
    && typeof validation.assertiveness === 'number'
    && typeof validation.ambiguity === 'number';
  const editionLabel = validation.edition == null
    ? 'Legacy'
    : `Edition ${validation.edition}`;
  const historyLabel = current
    ? null
    : validation.edition == null
      ? 'Historical result'
      : currentEdition !== undefined && validation.edition === currentEdition
        ? 'Superseded attempt'
        : 'Previous edition';

  return (
    <article
      className={`rounded-xl border p-4 ${
        current
          ? isSuccess
            ? 'border-emerald-300 bg-emerald-50/60 dark:border-emerald-800 dark:bg-emerald-950/20'
            : 'border-red-300 bg-red-50/60 dark:border-red-800 dark:bg-red-950/20'
          : 'border-surface-200 bg-surface-50/70 dark:border-surface-700 dark:bg-surface-800/40'
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-surface-900 dark:text-white">
              {editionLabel}
            </span>
            {!current && (
              <span className="text-[10px] text-surface-500 dark:text-surface-400">
                {historyLabel} · Attempt {attemptNumber}
              </span>
            )}
            <ValidationCycleStatusBadge
              state={isSuccess ? 'passed' : 'failed'}
              label={isSuccess ? 'Passed' : 'Failed'}
            />
          </div>
          <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
            Evaluated {new Date(validation.created_at).toLocaleString()} by{' '}
            {validation.reviewer_name || validation.reviewer_id}
          </p>
        </div>
        {canonicalDimensions ? (
          <span className="text-xs font-semibold text-violet-700 dark:text-violet-300">
            Five-metric assessment
          </span>
        ) : formalResult ? (
          <span className="text-xs font-semibold text-violet-700 dark:text-violet-300">
            Score {validation.score}/100
          </span>
        ) : validation.recommendation ? (
          <span className={`text-xs font-semibold ${
            validation.recommendation === 'approve'
              ? 'text-emerald-700 dark:text-emerald-300'
              : 'text-red-700 dark:text-red-300'
          }`}>
            {validation.recommendation === 'approve' ? 'Approved' : 'Rejected'}
          </span>
        ) : null}
      </div>

      {canonicalDimensions ? (
        <div className="my-4 grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-5">
          <ScoreCell
            dimension="confidence"
            label="Confidence"
            value={validation.confidence!}
            threshold={thresholds?.min_spec_confidence}
            direction="min"
          />
          <ScoreCell
            dimension="clarity"
            label="Clarity"
            value={validation.clarity!}
            threshold={thresholds?.min_spec_clarity}
            direction="min"
          />
          <ScoreCell
            dimension="assertiveness"
            label="Assertiveness"
            value={validation.assertiveness!}
            threshold={thresholds?.min_spec_assertiveness}
            direction="min"
          />
          <ScoreCell
            dimension="decidability"
            label="Decidability"
            value={validation.decidability!}
            threshold={thresholds?.min_spec_decidability}
            direction="min"
          />
          <ScoreCell
            dimension="ambiguity"
            label="Ambiguity"
            value={validation.ambiguity!}
            threshold={thresholds?.max_spec_ambiguity}
            direction="max"
          />
        </div>
      ) : formalResult ? (
        <div className="my-4 flex justify-center">
          <ScoreCell
            dimension="overall"
            label="Validation score"
            value={validation.score!}
            direction="min"
          />
        </div>
      ) : legacyDimensions ? (
        <div className="my-4 grid grid-cols-1 gap-5 sm:grid-cols-3">
          <ScoreCell
            dimension="completeness"
            label="Completeness"
            value={validation.completeness!}
            threshold={thresholds?.min_spec_completeness}
            direction="min"
          />
          <ScoreCell
            dimension="assertiveness"
            label="Assertiveness"
            value={validation.assertiveness!}
            threshold={thresholds?.min_spec_assertiveness}
            direction="min"
          />
          <ScoreCell
            dimension="ambiguity"
            label="Ambiguity"
            value={validation.ambiguity!}
            threshold={thresholds?.max_spec_ambiguity}
            direction="max"
          />
        </div>
      ) : null}

      {(validation.threshold_violations?.length ?? 0) > 0 && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-2.5 dark:border-red-800 dark:bg-red-950/25">
          <p className="text-[11px] font-semibold text-red-700 dark:text-red-300">
            Thresholds needing attention
          </p>
          <ul className="mt-1 list-inside list-disc text-[10px] text-red-600 dark:text-red-400">
            {validation.threshold_violations!.map((violation) => (
              <li key={violation}>{violation}</li>
            ))}
          </ul>
        </div>
      )}

      {!canonicalDimensions && (formalResult || validation.general_justification) && (
        <p className="border-l-2 border-surface-300 pl-3 text-xs italic text-surface-700 dark:border-surface-600 dark:text-surface-300">
          {formalResult ? validation.summary : validation.general_justification}
        </p>
      )}
      {(canonicalDimensions || legacyDimensions) && !current && (
        <>
          <button
            type="button"
            onClick={() => measureValidationWorkspaceInteraction(
              'validation_check',
              expanded,
              onToggleExpand,
            )}
            aria-expanded={expanded}
            aria-controls={detailsId}
            className="mt-3 inline-flex items-center gap-1 rounded text-[11px] font-medium text-violet-600 hover:text-violet-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 dark:text-violet-300 dark:focus-visible:ring-offset-surface-900"
          >
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {expanded ? 'Hide details' : 'View metric justifications'}
          </button>
          {expanded && (
            <dl
              id={detailsId}
              className="mt-3 space-y-2 text-xs text-surface-600 dark:text-surface-300"
            >
              {canonicalDimensions ? (
                <>
                  <div><dt className="font-semibold">Confidence</dt><dd>{validation.confidence_justification}</dd></div>
                  <div><dt className="font-semibold">Clarity</dt><dd>{validation.clarity_justification}</dd></div>
                  <div><dt className="font-semibold">Assertiveness</dt><dd>{validation.assertiveness_justification}</dd></div>
                  <div><dt className="font-semibold">Decidability</dt><dd>{validation.decidability_justification}</dd></div>
                  <div><dt className="font-semibold">Ambiguity</dt><dd>{validation.ambiguity_justification}</dd></div>
                </>
              ) : (
                <>
                  <div><dt className="font-semibold">Completeness</dt><dd>{validation.completeness_justification}</dd></div>
                  <div><dt className="font-semibold">Assertiveness</dt><dd>{validation.assertiveness_justification}</dd></div>
                  <div><dt className="font-semibold">Ambiguity</dt><dd>{validation.ambiguity_justification}</dd></div>
                </>
              )}
            </dl>
          )}
        </>
      )}
      {(canonicalDimensions || legacyDimensions) && current && (
        <MetricJustifications
          validation={validation}
          canonicalDimensions={canonicalDimensions}
        />
      )}

      {(validation.pinpoints?.length ?? 0) > 0 && (
        <section className="mt-4 space-y-2" aria-label="Pinpoint findings">
          <h4 className="text-xs font-semibold text-surface-800 dark:text-surface-100">
            Pinpoint findings
          </h4>
          <ol className="space-y-2">
            {validation.pinpoints!.map((pinpoint, index) => {
              const anchorReference = pinpoint.anchor_ref?.trim() || null;
              const anchorText = resolvePinpointAnchorText(
                pinpoint.anchor_type,
                anchorReference,
                anchorTexts,
              );
              return (
              <li key={`${pinpoint.metric}-${pinpoint.anchor_type}-${pinpoint.anchor_ref ?? index}`} className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50">
                <div className="flex flex-wrap items-start gap-2">
                  <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                    {pinpoint.metric}
                  </span>
                  <p
                    data-testid="spec-validation-pinpoint-target"
                    className="min-w-0 flex-1 whitespace-pre-wrap text-xs font-medium text-surface-800 dark:text-surface-100"
                  >
                    {anchorText}
                    {anchorReference ? (
                      <>
                        {' '}
                        <span className="font-mono text-[11px] font-normal text-surface-500 dark:text-surface-400">
                          ({anchorReference})
                        </span>
                      </>
                    ) : null}
                  </p>
                </div>
                <p className="mt-2 whitespace-pre-wrap text-xs text-surface-700 dark:text-surface-300">
                  {pinpoint.detail}
                </p>
              </li>
              );
            })}
          </ol>
        </section>
      )}
    </article>
  );
}

function MetricJustifications({
  validation,
  canonicalDimensions,
}: {
  validation: SpecValidation;
  canonicalDimensions: boolean;
}) {
  return (
    <dl className="mt-3 space-y-2 text-xs text-surface-600 dark:text-surface-300">
      {canonicalDimensions ? (
        <>
          <div><dt className="font-semibold">Confidence</dt><dd>{validation.confidence_justification}</dd></div>
          <div><dt className="font-semibold">Clarity</dt><dd>{validation.clarity_justification}</dd></div>
          <div><dt className="font-semibold">Assertiveness</dt><dd>{validation.assertiveness_justification}</dd></div>
          <div><dt className="font-semibold">Decidability</dt><dd>{validation.decidability_justification}</dd></div>
          <div><dt className="font-semibold">Ambiguity</dt><dd>{validation.ambiguity_justification}</dd></div>
        </>
      ) : (
        <>
          <div><dt className="font-semibold">Completeness</dt><dd>{validation.completeness_justification}</dd></div>
          <div><dt className="font-semibold">Assertiveness</dt><dd>{validation.assertiveness_justification}</dd></div>
          <div><dt className="font-semibold">Ambiguity</dt><dd>{validation.ambiguity_justification}</dd></div>
        </>
      )}
    </dl>
  );
}

function resolvePinpointAnchorText(
  anchorType: SpecValidationPinpoint['anchor_type'],
  anchorRef: string | null,
  anchorTexts?: Readonly<Record<string, string>>,
): string {
  if (anchorType === 'whole_artifact') return 'Whole Spec';
  if (!anchorRef) return 'Referenced Spec item';
  const direct = anchorTexts?.[anchorRef];
  if (direct) return direct;
  const stableId = anchorRef.split('.').at(-1);
  const qualified = stableId ? anchorTexts?.[stableId] : undefined;
  if (qualified) return qualified;
  if (anchorType === 'field') {
    return anchorRef
      .split('_')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  }
  return 'Referenced item is no longer available in the current Spec';
}

interface ScoreCellProps {
  dimension:
    | 'overall'
    | 'confidence'
    | 'clarity'
    | 'completeness'
    | 'assertiveness'
    | 'decidability'
    | 'ambiguity';
  label: string;
  value: number;
  threshold?: number | null;
  direction: 'min' | 'max';
}

function ScoreCell({
  dimension,
  label,
  value,
  threshold,
  direction,
}: ScoreCellProps) {
  const passes = threshold == null
    ? null
    : direction === 'min'
      ? value >= threshold
      : value <= threshold;
  const ringTone = passes == null
    ? 'border-blue-400 text-blue-700 dark:border-blue-500 dark:text-blue-300'
    : passes
      ? 'border-emerald-400 text-emerald-700 dark:border-emerald-500 dark:text-emerald-300'
      : 'border-red-400 text-red-700 dark:border-red-500 dark:text-red-300';
  const thresholdLabel = threshold == null
    ? 'No board threshold'
    : `${direction === 'min' ? 'Minimum' : 'Maximum'} ${threshold}`;
  const accessibleMetricLabel = label.toLowerCase().endsWith('score')
    ? label
    : `${label} score`;

  return (
    <div className="flex min-w-0 flex-col items-center text-center">
      <div
        role="img"
        aria-label={`${accessibleMetricLabel} ${value} out of 100, ${thresholdLabel}`}
        data-testid={`spec-validation-score-${dimension}`}
        className={`flex h-20 w-20 items-center justify-center rounded-full border-4 ${ringTone}`}
      >
        <span aria-hidden="true" className="text-2xl font-bold leading-none">
          {value}
          <span className="ml-0.5 text-sm font-semibold text-surface-400">/100</span>
        </span>
      </div>
      <p className="mt-2 text-xs font-semibold text-surface-700 dark:text-surface-200">
        {label}
      </p>
      <p className="mt-1 flex items-center gap-1 text-[10px] text-surface-500 dark:text-surface-400">
        {thresholdLabel}
        {passes === true && <Check size={11} className="text-emerald-600 dark:text-emerald-400" aria-hidden="true" />}
        {passes === false && <X size={11} className="text-red-600 dark:text-red-400" aria-hidden="true" />}
      </p>
    </div>
  );
}
