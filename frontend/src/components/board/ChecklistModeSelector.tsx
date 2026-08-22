import type { ChecklistMode } from '@/types';

const CHECKLIST_MODE_OPTIONS: Array<{
  value: ChecklistMode;
  label: string;
  description: string;
}> = [
  {
    value: 'off',
    label: 'Off',
    description: 'No checklist execution and no validation gate.',
  },
  {
    value: 'advisory',
    label: 'Advisory',
    description: 'Collects traceable evidence without blocking validation.',
  },
  {
    value: 'blocking',
    label: 'Blocking',
    description: 'Requires a passing result for the current validation edition.',
  },
];

export function ChecklistModeSelector({
  value,
  onChange,
  disabled = false,
  testIdPrefix = 'checklist-mode',
}: {
  value: ChecklistMode;
  onChange: (mode: ChecklistMode) => void;
  disabled?: boolean;
  testIdPrefix?: string;
}) {
  return (
    <div
      className="grid grid-cols-3 gap-2"
      role="radiogroup"
      aria-label="Spec checklist mode"
    >
      {CHECKLIST_MODE_OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          data-testid={`${testIdPrefix}-${option.value}`}
          disabled={disabled}
          onClick={() => onChange(option.value)}
          className={`rounded border px-2 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
            value === option.value
              ? 'border-violet-400 bg-violet-50 text-violet-800 dark:border-violet-500 dark:bg-violet-500/15 dark:text-violet-200'
              : 'border-gray-200 bg-gray-50 text-gray-600 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
          }`}
        >
          <span className="block text-[11px] font-semibold">
            {option.label}
          </span>
          <span className="mt-0.5 block text-[9px] leading-3 opacity-80">
            {option.description}
          </span>
        </button>
      ))}
    </div>
  );
}
