/**
 * SpecValidationHistoryPanel — reverse chronological display of spec validation records.
 *
 * Fetches history via api.listSpecValidations and renders each record with:
 * - outcome badge (SUCCESS / FAILED)
 * - active badge on the current_validation_id pointer
 * - 3 scores with threshold comparison
 * - expand/collapse for full justifications
 * - reviewer + timestamp
 */

import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, Check, X } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { SpecValidation, SpecValidationList } from '@/types';

interface SpecValidationHistoryPanelProps {
  specId: string;
  refreshKey?: number; // bump to force re-fetch
}

export function SpecValidationHistoryPanel({ specId, refreshKey = 0 }: SpecValidationHistoryPanelProps) {
  const api = useDashboardApi();
  // `useDashboardApi()` rebuilds its object every render, so keeping `api`
  // in the effect deps would loop forever: effect fires → setLoading(true)
  // → re-render → new api ref → cleanup cancels the in-flight request →
  // effect fires again → repeat. Pin the latest api to a ref and depend
  // only on the stable inputs (specId, refreshKey).
  const apiRef = useRef(api);
  apiRef.current = api;

  const [data, setData] = useState<SpecValidationList | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    apiRef.current.listSpecValidations(specId)
      .then((res: SpecValidationList) => {
        if (!cancelled) setData(res);
      })
      .catch((e: any) => {
        if (!cancelled) toast.error(e?.message || 'Failed to load validation history');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [specId, refreshKey]);

  if (loading) {
    return <div className="text-xs text-gray-500 dark:text-gray-400 p-3">Loading validation history…</div>;
  }

  if (!data || data.validations.length === 0) {
    return (
      <div className="text-xs text-gray-400 dark:text-gray-500 p-3 italic">
        No validation records yet. Submit a spec validation to begin tracking.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-200 uppercase tracking-wide">
          Validation History
        </h4>
        <span className="text-[10px] text-gray-500 dark:text-gray-400">
          {data.validations.length} {data.validations.length === 1 ? 'attempt' : 'attempts'}
          {data.current_validation_id ? ' • 1 active' : ''}
        </span>
      </div>
      <div className="space-y-2 max-h-[500px] overflow-y-auto">
        {data.validations.map((v) => (
          <ValidationRecord
            key={v.id}
            validation={v}
            expanded={expandedId === v.id}
            onToggleExpand={() => setExpandedId(expandedId === v.id ? null : v.id)}
          />
        ))}
      </div>
    </div>
  );
}

interface ValidationRecordProps {
  validation: SpecValidation;
  expanded: boolean;
  onToggleExpand: () => void;
}

function ValidationRecord({ validation, expanded, onToggleExpand }: ValidationRecordProps) {
  const isSuccess = validation.outcome === 'success';
  const isActive = validation.active === true;
  const isRejectedByReviewer = validation.recommendation === 'reject';

  const outcomeBadge = isSuccess
    ? 'bg-green-600 text-white'
    : isRejectedByReviewer
    ? 'bg-gray-600 text-white'
    : 'bg-red-600 text-white';

  const borderColor = isActive
    ? 'border-2 border-green-400 dark:border-green-700 bg-green-50 dark:bg-green-900/10'
    : isSuccess
    ? 'border border-green-200 dark:border-green-800 bg-green-50/30 dark:bg-green-900/5'
    : 'border border-red-200 dark:border-red-800 bg-red-50/30 dark:bg-red-900/5';

  const thresholds = validation.resolved_thresholds;

  return (
    <div className={`rounded-lg p-3 ${borderColor}`}>
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${outcomeBadge}`}>
            {validation.outcome.toUpperCase()}
          </span>
          {isActive && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-600 text-white font-bold">ACTIVE</span>
          )}
          <span className="text-xs font-medium text-gray-900 dark:text-white font-mono">{validation.id}</span>
        </div>
        <span className="text-[10px] text-gray-500 dark:text-gray-400">
          {new Date(validation.created_at).toLocaleString()}
        </span>
      </div>
      <div className="text-[11px] text-gray-600 dark:text-gray-400 mb-2">
        by <span className="font-medium text-gray-800 dark:text-gray-200">{validation.reviewer_name || validation.reviewer_id}</span>
        {' • recommendation='}
        <span className={validation.recommendation === 'approve' ? 'text-green-600 font-medium' : 'text-red-600 font-medium'}>
          {validation.recommendation}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2 mb-2">
        <ScoreCell label="Completeness" value={validation.completeness} threshold={thresholds?.min_spec_completeness} direction="min" />
        <ScoreCell label="Assertiveness" value={validation.assertiveness} threshold={thresholds?.min_spec_assertiveness} direction="min" />
        <ScoreCell label="Ambiguity" value={validation.ambiguity} threshold={thresholds?.max_spec_ambiguity} direction="max" />
      </div>
      {validation.threshold_violations.length > 0 && (
        <div className="bg-red-100 dark:bg-red-900/30 rounded p-1.5 mb-2">
          <div className="text-[10px] font-semibold text-red-700 dark:text-red-300">Threshold violations</div>
          <ul className="text-[10px] text-red-600 dark:text-red-400 list-disc list-inside">
            {validation.threshold_violations.map((v, i) => <li key={i}>{v}</li>)}
          </ul>
        </div>
      )}
      <div className="text-[11px] text-gray-700 dark:text-gray-300 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">
        {validation.general_justification}
      </div>
      <button
        onClick={onToggleExpand}
        className="text-[10px] text-violet-600 hover:text-violet-700 mt-2 flex items-center gap-1"
      >
        {expanded ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
        {expanded ? 'Hide' : 'Show'} per-dimension justifications
      </button>
      {expanded && (
        <div className="mt-2 space-y-1.5 text-[11px] text-gray-600 dark:text-gray-400">
          <div><span className="font-semibold">Completeness:</span> {validation.completeness_justification}</div>
          <div><span className="font-semibold">Assertiveness:</span> {validation.assertiveness_justification}</div>
          <div><span className="font-semibold">Ambiguity:</span> {validation.ambiguity_justification}</div>
        </div>
      )}
    </div>
  );
}

interface ScoreCellProps {
  label: string;
  value: number;
  threshold?: number | null;
  direction: 'min' | 'max';
}

function ScoreCell({ label, value, threshold, direction }: ScoreCellProps) {
  const passes = threshold == null
    ? null
    : direction === 'min'
    ? value >= threshold
    : value <= threshold;
  const color = passes == null
    ? 'text-gray-700 dark:text-gray-200'
    : passes
    ? 'text-green-600 dark:text-green-400'
    : 'text-red-600 dark:text-red-400';
  return (
    <div className="bg-white dark:bg-gray-800 rounded p-2 text-center">
      <div className="text-[9px] text-gray-500 uppercase">{label}</div>
      <div className={`text-sm font-bold ${color}`}>
        {value}
        {threshold != null && (
          <span className="text-[9px] text-gray-400 ml-1">
            {direction === 'min' ? '/' : 'max '}{threshold}
          </span>
        )}
        {passes === true && <Check size={10} className="inline ml-1" />}
        {passes === false && <X size={10} className="inline ml-1" />}
      </div>
    </div>
  );
}
