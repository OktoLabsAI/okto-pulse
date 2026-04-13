import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';

type GateState = 'inherit' | 'required' | 'disabled';

interface ValidationGateOverrideProps {
  title?: string;
  requireValue: boolean | null;
  minConfidence: number | null;
  minCompleteness: number | null;
  maxDrift: number | null;
  parentLabel: string; // "Board default" or "Spec/Board"
  onUpdate: (patch: {
    require_task_validation?: boolean | null;
    validation_min_confidence?: number | null;
    validation_min_completeness?: number | null;
    validation_max_drift?: number | null;
  }) => Promise<void>;
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
  requireValue,
  minConfidence,
  minCompleteness,
  maxDrift,
  parentLabel,
  onUpdate,
}: ValidationGateOverrideProps) {
  const currentState = toState(requireValue);
  const [localConf, setLocalConf] = useState<string>(minConfidence !== null ? String(minConfidence) : '');
  const [localCompl, setLocalCompl] = useState<string>(minCompleteness !== null ? String(minCompleteness) : '');
  const [localDrift, setLocalDrift] = useState<string>(maxDrift !== null ? String(maxDrift) : '');

  const handleStateChange = async (newState: GateState) => {
    if (newState === currentState) return;
    const newRequire = fromState(newState);
    const patch: any = { require_task_validation: newRequire };
    // When moving away from "required", clear threshold overrides
    if (newState !== 'required') {
      patch.validation_min_confidence = null;
      patch.validation_min_completeness = null;
      patch.validation_max_drift = null;
      setLocalConf('');
      setLocalCompl('');
      setLocalDrift('');
    }
    await onUpdate(patch);
  };

  const handleThresholdBlur = async (field: 'conf' | 'compl' | 'drift', rawValue: string) => {
    const trimmed = rawValue.trim();
    const parsed = trimmed === '' ? null : Number(trimmed);
    if (parsed !== null && (isNaN(parsed) || parsed < 0 || parsed > 100)) return;
    const patch: any = {};
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
              onClick={() => handleStateChange(opt)}
              className={`flex-1 py-1.5 px-3 text-xs rounded-md font-medium transition-all ${
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

      {/* Threshold overrides when Required */}
      {currentState === 'required' && (
        <div className="bg-violet-50 dark:bg-violet-900/10 border border-violet-200 dark:border-violet-800 rounded-lg p-3 space-y-2.5">
          <p className="text-[10px] text-violet-500 font-medium">
            Override thresholds (leave empty to inherit from {parentLabel.toLowerCase()})
          </p>

          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1">
              <label className="text-[10px] text-gray-500 dark:text-gray-400">Min Confidence</label>
              <input
                type="number"
                min={0}
                max={100}
                value={localConf}
                onChange={(e) => setLocalConf(e.target.value)}
                onBlur={(e) => handleThresholdBlur('conf', e.target.value)}
                placeholder="70"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white"
              />
            </div>
            <div className="space-y-1">
              <label className="text-[10px] text-gray-500 dark:text-gray-400">Min Completeness</label>
              <input
                type="number"
                min={0}
                max={100}
                value={localCompl}
                onChange={(e) => setLocalCompl(e.target.value)}
                onBlur={(e) => handleThresholdBlur('compl', e.target.value)}
                placeholder="80"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white"
              />
            </div>
            <div className="space-y-1">
              <label className="text-[10px] text-gray-500 dark:text-gray-400">Max Drift</label>
              <input
                type="number"
                min={0}
                max={100}
                value={localDrift}
                onChange={(e) => setLocalDrift(e.target.value)}
                onBlur={(e) => handleThresholdBlur('drift', e.target.value)}
                placeholder="50"
                className="w-full text-center text-xs font-mono border border-violet-200 dark:border-violet-700 rounded px-1.5 py-1 bg-white dark:bg-gray-800 text-gray-800 dark:text-white"
              />
            </div>
          </div>
          <p className="text-[10px] text-gray-400">Empty = inherit (70 / 80 / 50)</p>
        </div>
      )}

      {currentState === 'disabled' && (
        <p className="text-[10px] text-gray-500 dark:text-gray-400 italic">
          Validation gate is explicitly disabled for this level. Cards bypass validation and move directly to Done.
        </p>
      )}
    </div>
  );
}
