interface AmbiguityGateSkipToggleProps {
  subjectLabel: 'ideation' | 'refinement';
  checked: boolean;
  disabled?: boolean;
  onCheckedChange: (checked: boolean) => void;
}

/**
 * Shared UI for the per-entity Max ambiguity gate override.
 *
 * The write contracts remain owned by each entity. Keeping the control here
 * prevents the ideation and refinement modals from drifting into different
 * interaction and accessibility patterns.
 */
export function AmbiguityGateSkipToggle({
  subjectLabel,
  checked,
  disabled = false,
  onCheckedChange,
}: AmbiguityGateSkipToggleProps) {
  return (
    <div
      className="flex items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2 dark:border-amber-500/30 dark:bg-amber-500/10"
      data-testid="ambiguity-gate-skip-control"
    >
      <div>
        <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
          Skip Max ambiguity gate
        </span>
        <p className="text-[10px] text-gray-500 dark:text-gray-400">
          Allow this {subjectLabel} to complete without the board ambiguity gate.
        </p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={`Skip the Max ambiguity gate for this ${subjectLabel}`}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        data-testid="toggle-skip-ambiguity-gate"
        className={`relative inline-flex h-5 w-10 shrink-0 items-center rounded-full p-0.5 transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
          checked ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'
        }`}
      >
        <span
          className={`h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
            checked ? 'translate-x-5' : 'translate-x-0'
          }`}
        />
      </button>
    </div>
  );
}
