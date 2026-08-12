import { useEffect, useState } from 'react';
import { Check, RefreshCw, Shield, X } from 'lucide-react';
import toast from 'react-hot-toast';

import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useDashboardApi } from '@/services/api';
import type {
  BoardSettings,
  ChecklistSpecState,
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
  /** Retained as a compatibility prop while thresholds leave the human form. */
  settings: BoardSettings;
  canReadChecklist: boolean;
  canExecuteChecklist: boolean;
  onClose: () => void;
  onSubmitted: (result: SpecValidationSubmitResponse) => void;
}

export function SubmitSpecValidationModal({
  specId,
  specTitle,
  boardId,
  specVersion,
  specEdition,
  canReadChecklist,
  canExecuteChecklist,
  onClose,
  onSubmitted,
}: SubmitSpecValidationModalProps) {
  const currentSpecEdition = specEdition ?? 1;
  const api = useDashboardApi();
  const [score, setScore] = useState(80);
  const [summary, setSummary] = useState('');
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
  const canSubmit =
    summary.trim().length > 0
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
      score,
      summary: summary.trim(),
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
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl bg-white shadow-2xl dark:bg-gray-900">
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

          <section className="space-y-3" aria-labelledby="spec-validation-score-label">
            <div className="flex items-center justify-between gap-3">
              <label
                id="spec-validation-score-label"
                htmlFor="spec-validation-score"
                className="text-sm font-medium text-gray-900 dark:text-white"
              >
                Validation score
              </label>
              <span className="font-mono text-sm font-semibold text-gray-900 dark:text-white">
                {score}/100
              </span>
            </div>
            <input
              id="spec-validation-score"
              type="range"
              min={0}
              max={100}
              value={score}
              onChange={(event) => setScore(Number(event.target.value))}
              className="w-full accent-violet-500"
            />
          </section>

          <div className="space-y-2">
            <label
              htmlFor="spec-validation-summary"
              className="text-sm font-medium text-gray-900 dark:text-white"
            >
              Validation summary
            </label>
            <textarea
              id="spec-validation-summary"
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
              placeholder="Summarize the human validation result for this edition"
              rows={5}
              className="w-full rounded border border-gray-300 bg-white p-2 text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
            />
            <p className="text-[11px] text-gray-500 dark:text-gray-400">
              This becomes the current result for Edition {currentSpecEdition}.
              Earlier results remain available under Previous validations.
            </p>
          </div>

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
