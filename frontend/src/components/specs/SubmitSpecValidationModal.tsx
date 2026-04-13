/**
 * SubmitSpecValidationModal — Spec Validation Gate submission form.
 *
 * Collects 3 scores (completeness, assertiveness, ambiguity), per-dimension
 * justifications (min 10 chars), overall justification (min 20 chars), and
 * a recommendation (approve/reject). Shows real-time outcome preview based
 * on the board's thresholds.
 *
 * Coverage gates MUST have passed server-side before this modal opens.
 * The backend will still run them as a pre-requisite and reject with a
 * contextual error if anything fails.
 */

import { useMemo, useState } from 'react';
import { X, Check, AlertCircle, Shield } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { BoardSettings, SpecValidationSubmitPayload } from '@/types';

interface SubmitSpecValidationModalProps {
  specId: string;
  specTitle: string;
  settings: BoardSettings;
  onClose: () => void;
  onSubmitted: (result: { outcome: 'success' | 'failed'; spec_status?: string | null }) => void;
}

const MIN_JUSTIFICATION_PER_DIM = 10;
const MIN_GENERAL_JUSTIFICATION = 20;

export function SubmitSpecValidationModal({
  specId,
  specTitle,
  settings,
  onClose,
  onSubmitted,
}: SubmitSpecValidationModalProps) {
  const api = useDashboardApi();
  const [completeness, setCompleteness] = useState(80);
  const [assertiveness, setAssertiveness] = useState(80);
  const [ambiguity, setAmbiguity] = useState(30);
  const [completenessJustification, setCompletenessJustification] = useState('');
  const [assertivenessJustification, setAssertivenessJustification] = useState('');
  const [ambiguityJustification, setAmbiguityJustification] = useState('');
  const [generalJustification, setGeneralJustification] = useState('');
  const [recommendation, setRecommendation] = useState<'approve' | 'reject' | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const minCompleteness = settings.min_spec_completeness ?? 80;
  const minAssertiveness = settings.min_spec_assertiveness ?? 80;
  const maxAmbiguity = settings.max_spec_ambiguity ?? 30;

  // Compute threshold violations in real time
  const violations = useMemo(() => {
    const v: string[] = [];
    if (completeness < minCompleteness) v.push(`completeness ${completeness} < min ${minCompleteness}`);
    if (assertiveness < minAssertiveness) v.push(`assertiveness ${assertiveness} < min ${minAssertiveness}`);
    if (ambiguity > maxAmbiguity) v.push(`ambiguity ${ambiguity} > max ${maxAmbiguity}`);
    return v;
  }, [completeness, assertiveness, ambiguity, minCompleteness, minAssertiveness, maxAmbiguity]);

  // Outcome preview mirrors backend rule exactly
  const outcomePreview: 'success' | 'failed' =
    violations.length === 0 && recommendation === 'approve' ? 'success' : 'failed';

  const justificationsValid =
    completenessJustification.trim().length >= MIN_JUSTIFICATION_PER_DIM &&
    assertivenessJustification.trim().length >= MIN_JUSTIFICATION_PER_DIM &&
    ambiguityJustification.trim().length >= MIN_JUSTIFICATION_PER_DIM &&
    generalJustification.trim().length >= MIN_GENERAL_JUSTIFICATION;

  const canSubmit = justificationsValid && recommendation !== null && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    const payload: SpecValidationSubmitPayload = {
      completeness,
      completeness_justification: completenessJustification.trim(),
      assertiveness,
      assertiveness_justification: assertivenessJustification.trim(),
      ambiguity,
      ambiguity_justification: ambiguityJustification.trim(),
      general_justification: generalJustification.trim(),
      recommendation: recommendation!,
    };
    try {
      const result = await api.submitSpecValidation(specId, payload);
      if (result?.outcome === 'success') {
        toast.success('Spec validated and promoted!');
      } else {
        toast.error(`Validation failed: ${result?.threshold_violations?.join(', ') || 'rejected by reviewer'}`);
      }
      onSubmitted(result);
      onClose();
    } catch (e: any) {
      toast.error(e?.message || 'Submission failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-900 rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
              <Shield size={16} /> Validate Spec
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate max-w-md">{specTitle}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X size={18} />
          </button>
        </div>

        <div className="px-6 py-5 space-y-5">
          <DimensionSlider
            label="Completeness"
            value={completeness}
            onChange={setCompleteness}
            threshold={minCompleteness}
            direction="min"
            justification={completenessJustification}
            onJustificationChange={setCompletenessJustification}
          />
          <DimensionSlider
            label="Assertiveness"
            value={assertiveness}
            onChange={setAssertiveness}
            threshold={minAssertiveness}
            direction="min"
            justification={assertivenessJustification}
            onJustificationChange={setAssertivenessJustification}
          />
          <DimensionSlider
            label="Ambiguity"
            hint="lower is better"
            value={ambiguity}
            onChange={setAmbiguity}
            threshold={maxAmbiguity}
            direction="max"
            justification={ambiguityJustification}
            onJustificationChange={setAmbiguityJustification}
          />

          <div className="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-2">
            <label className="text-sm font-medium text-gray-900 dark:text-white">
              Overall justification
              <span className="text-[10px] text-gray-400 ml-2">
                {generalJustification.trim().length}/{MIN_GENERAL_JUSTIFICATION} min
              </span>
            </label>
            <textarea
              value={generalJustification}
              onChange={(e) => setGeneralJustification(e.target.value)}
              placeholder="Provide context for your overall judgment (min 20 chars)"
              rows={3}
              className="w-full text-xs p-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium text-gray-900 dark:text-white">Recommendation</label>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => setRecommendation('approve')}
                className={`flex-1 flex items-center gap-2 px-3 py-2 rounded border-2 transition-colors ${
                  recommendation === 'approve'
                    ? 'border-green-500 bg-green-50 dark:bg-green-900/20'
                    : 'border-gray-300 dark:border-gray-600'
                }`}
              >
                <Check size={14} className={recommendation === 'approve' ? 'text-green-600' : 'text-gray-400'} />
                <span className={`text-sm font-medium ${recommendation === 'approve' ? 'text-green-700 dark:text-green-300' : 'text-gray-600 dark:text-gray-300'}`}>
                  Approve
                </span>
              </button>
              <button
                type="button"
                onClick={() => setRecommendation('reject')}
                className={`flex-1 flex items-center gap-2 px-3 py-2 rounded border-2 transition-colors ${
                  recommendation === 'reject'
                    ? 'border-red-500 bg-red-50 dark:bg-red-900/20'
                    : 'border-gray-300 dark:border-gray-600'
                }`}
              >
                <X size={14} className={recommendation === 'reject' ? 'text-red-600' : 'text-gray-400'} />
                <span className={`text-sm font-medium ${recommendation === 'reject' ? 'text-red-700 dark:text-red-300' : 'text-gray-600 dark:text-gray-300'}`}>
                  Reject
                </span>
              </button>
            </div>
          </div>

          {/* Outcome preview */}
          <div
            className={`rounded p-3 border ${
              outcomePreview === 'success'
                ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
                : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
            }`}
          >
            <div className={`text-xs font-semibold mb-1 ${outcomePreview === 'success' ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'}`}>
              Current outcome preview: {outcomePreview.toUpperCase()}
            </div>
            {violations.length > 0 && (
              <ul className="text-[11px] text-red-600 dark:text-red-400 space-y-0.5 list-disc list-inside">
                {violations.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            )}
            {recommendation === 'reject' && (
              <div className="text-[11px] text-red-600 dark:text-red-400 italic mt-1">
                Reviewer recommendation is reject — outcome will be failed regardless of scores.
              </div>
            )}
            {outcomePreview === 'success' && (
              <div className="text-[11px] text-green-600 dark:text-green-400 italic mt-1">
                Spec will be atomically promoted to validated and enter content lock.
              </div>
            )}
          </div>
        </div>

        <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2 bg-gray-50 dark:bg-gray-800">
          <button
            onClick={onClose}
            className="px-4 py-1.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="px-4 py-1.5 text-sm bg-violet-600 text-white rounded hover:bg-violet-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
          >
            {submitting ? 'Submitting…' : 'Submit Validation'}
          </button>
        </div>
      </div>
    </div>
  );
}

interface DimensionSliderProps {
  label: string;
  hint?: string;
  value: number;
  onChange: (v: number) => void;
  threshold: number;
  direction: 'min' | 'max';
  justification: string;
  onJustificationChange: (v: string) => void;
}

function DimensionSlider({
  label, hint, value, onChange, threshold, direction, justification, onJustificationChange,
}: DimensionSliderProps) {
  const passes = direction === 'min' ? value >= threshold : value <= threshold;
  const counterNeeded = MIN_JUSTIFICATION_PER_DIM - justification.trim().length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium text-gray-900 dark:text-white">
          {label}
          {hint && <span className="text-[10px] text-gray-400 ml-2">({hint})</span>}
        </label>
        <div className="flex items-center gap-2 text-xs">
          <span className="font-mono text-gray-900 dark:text-white">{value}</span>
          <span className="text-gray-400">/100</span>
          {passes ? (
            <span className="flex items-center gap-1 text-green-600 dark:text-green-400">
              <Check size={10} /> {direction === 'min' ? 'min' : 'max'} {threshold}
            </span>
          ) : (
            <span className="flex items-center gap-1 text-red-600 dark:text-red-400">
              <AlertCircle size={10} /> {direction === 'min' ? 'min' : 'max'} {threshold}
            </span>
          )}
        </div>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-violet-500"
      />
      <textarea
        value={justification}
        onChange={(e) => onJustificationChange(e.target.value)}
        placeholder={`Why ${value}? (min ${MIN_JUSTIFICATION_PER_DIM} chars)`}
        rows={2}
        className="w-full text-xs p-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
      />
      <div className="text-[10px] text-gray-400 text-right">
        {counterNeeded > 0 ? `${counterNeeded} more chars needed` : `${justification.trim().length} chars`}
      </div>
    </div>
  );
}
