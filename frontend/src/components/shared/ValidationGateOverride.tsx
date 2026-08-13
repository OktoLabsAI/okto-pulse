import { useEffect, useId, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import type { TaskValidationGateOverride } from '@/types';

type GateState = 'inherit' | 'required' | 'disabled';

interface ValidationGateOverrideProps {
  title?: string;
  description?: string;
  requireValue: boolean | null;
  minConfidence: number | null;
  minCompleteness: number | null;
  maxDrift: number | null;
  parentLabel: string; // "Board default" or "Spec/Board"
  onUpdate: (patch: TaskValidationGateOverride) => Promise<void>;
  disabled?: boolean;
}

function toState(value: boolean | null): GateState {
  if (value === null || value === undefined) return 'inherit';
  return value ? 'required' : 'disabled';
}

function fromState(state: GateState): boolean | null {
  if (state === 'inherit') return null;
  return state === 'required';
}

export function ValidationGateOverride({
  title = 'Validation Gate',
  description,
  requireValue,
  minConfidence,
  minCompleteness,
  maxDrift,
  parentLabel,
  onUpdate,
  disabled = false,
}: ValidationGateOverrideProps) {
  const inputIdPrefix = useId();
  const currentState = toState(requireValue);
  const [localConf, setLocalConf] = useState<string>(minConfidence !== null ? String(minConfidence) : '');
  const [localCompl, setLocalCompl] = useState<string>(minCompleteness !== null ? String(minCompleteness) : '');
  const [localDrift, setLocalDrift] = useState<string>(maxDrift !== null ? String(maxDrift) : '');

  useEffect(() => {
    setLocalConf(minConfidence !== null ? String(minConfidence) : '');
  }, [minConfidence]);
  useEffect(() => {
    setLocalCompl(minCompleteness !== null ? String(minCompleteness) : '');
  }, [minCompleteness]);
  useEffect(() => {
    setLocalDrift(maxDrift !== null ? String(maxDrift) : '');
  }, [maxDrift]);

  const handleStateChange = async (newState: GateState) => {
    if (disabled) return;
    if (newState === currentState) return;
    const newRequire = fromState(newState);
    await onUpdate({ require_task_validation: newRequire });
  };

  const handleThresholdBlur = async (field: 'conf' | 'compl' | 'drift', rawValue: string) => {
    if (disabled) return;
    const trimmed = rawValue.trim();
    const parsed = trimmed === '' ? null : Number(trimmed);
    if (parsed !== null && (isNaN(parsed) || parsed < 0 || parsed > 100)) return;
    const patch: TaskValidationGateOverride = {};
    if (field === 'conf') patch.validation_min_confidence = parsed;
    if (field === 'compl') patch.validation_min_completeness = parsed;
    if (field === 'drift') patch.validation_max_drift = parsed;
    await onUpdate(patch);
  };

  const resolvedLabel =
    currentState === 'inherit'
      ? `Inherited from ${parentLabel}`
      : currentState === 'required'
      ? 'Required (override)'
      : 'Disabled (override)';

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <ShieldCheck size={14} className="text-violet-500" />
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">{title}</h4>
      </div>
      {description && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {description}
        </p>
      )}

      {/* Segmented control */}
      <div className="flex bg-gray-100 dark:bg-gray-700 rounded-lg p-0.5">
        {(['inherit', 'required', 'disabled'] as const).map((opt) => {
          const isActive = currentState === opt;
          const activeClass =
            opt === 'required'
              ? 'bg-violet-500 text-white shadow-sm'
              : opt === 'disabled'
              ? 'bg-gray-500 text-white shadow-sm'
              : 'bg-white dark:bg-gray-600 text-gray-700 dark:text-white shadow-sm';
          return (
            <button
              key={opt}
              type="button"
              disabled={disabled}
              onClick={() => handleStateChange(opt)}
              className={`flex-1 py-1.5 px-3 text-xs rounded-md font-medium transition-all disabled:cursor-not-allowed disabled:opacity-50 ${
                isActive ? activeClass : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
              }`}
            >
              {opt.charAt(0).toUpperCase() + opt.slice(1)}
            </button>
          );
        })}
      </div>

      {/* Resolved source indicator */}
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] text-gray-400">Resolved:</span>
        <span className="text-[10px] font-medium text-violet-600 dark:text-violet-400">{resolvedLabel}</span>
      </div>

      {/* Threshold inheritance is independent from the required/disabled flag. */}
      <div className="bg-violet-50 dark:bg-violet-900/10 border border-violet-200 dark:border-violet-800 rounded-lg p-3 space-y-2.5">
        <p className="text-[10px] text-violet-500 font-medium">
          Override thresholds independently (leave empty to inherit from {parentLabel.toLowerCase()})
        </p>

        <div className="grid grid-cols-3 gap-3">
          <div className="space-y-1">
            <label
              htmlFor={`${inputIdPrefix}-confidence`}
              className="text-[10px] text-gray-500 dark:text-gray-400"
            >
              Min Confidence
            </label>
            <input
                id={`${inputIdPrefix}-confidence`}
                type="number"
                min={0}
                max={100}
                disabled={disabled}
                value={localConf}
                onChange={(e) => setLocalConf(e.target.value)}
                onBlur={(e) => handleThresholdBlur('conf', e.target.value)}
                placeholder="70"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-1">
            <label
              htmlFor={`${inputIdPrefix}-completeness`}
              className="text-[10px] text-gray-500 dark:text-gray-400"
            >
              Min Completeness
            </label>
            <input
                id={`${inputIdPrefix}-completeness`}
                type="number"
                min={0}
                max={100}
                disabled={disabled}
                value={localCompl}
                onChange={(e) => setLocalCompl(e.target.value)}
                onBlur={(e) => handleThresholdBlur('compl', e.target.value)}
                placeholder="80"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-1">
            <label
              htmlFor={`${inputIdPrefix}-drift`}
              className="text-[10px] text-gray-500 dark:text-gray-400"
            >
              Max Drift
            </label>
            <input
                id={`${inputIdPrefix}-drift`}
                type="number"
                min={0}
                max={100}
                disabled={disabled}
                value={localDrift}
                onChange={(e) => setLocalDrift(e.target.value)}
                onBlur={(e) => handleThresholdBlur('drift', e.target.value)}
                placeholder="50"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
        </div>
        <p className="text-[10px] text-gray-400">
          Thresholds remain configured independently of the gate requirement.
        </p>
      </div>

      {currentState === 'disabled' && (
        <p className="text-[10px] text-gray-500 dark:text-gray-400 italic">
          Validation gate is explicitly disabled for this level. Cards bypass validation and move directly to Done.
        </p>
      )}
    </div>
  );
}
