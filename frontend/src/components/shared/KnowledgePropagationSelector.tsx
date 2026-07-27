import { AlertTriangle, BookOpen, Check, RefreshCw } from 'lucide-react';
import type {
  KnowledgePropagationAction,
  KnowledgePropagationChoice,
} from './knowledgePropagationChoice';
import type { KnowledgePropagationCandidate } from './knowledgePropagationCandidates';

interface KnowledgePropagationSelectorProps {
  items: KnowledgePropagationCandidate[];
  value: KnowledgePropagationChoice;
  onChange: (value: KnowledgePropagationChoice) => void;
  title?: string;
  description?: string;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  disabled?: boolean;
  testId?: string;
}

const ACTIONS: Array<{
  value: KnowledgePropagationAction;
  label: string;
  help: string;
}> = [
  {
    value: 'omitted',
    label: 'No decision',
    help: 'Preserve omitted: no Knowledge resource is selected or copied.',
  },
  {
    value: 'reference',
    label: 'Reference',
    help: 'Resolve the current source revision without copying its body.',
  },
  {
    value: 'snapshot',
    label: 'Snapshot',
    help: 'Capture an immutable revision that can later become stale.',
  },
  {
    value: 'drop',
    label: 'Drop',
    help: 'Suppress selected resources; with none selected, save explicit empty.',
  },
];

const ORIGIN_CLASS_LABELS: Record<string, string> = {
  v2: 'v2',
  legacy_all: 'legacy all',
  selected_legacy: 'selected legacy',
  legacy_unresolved: 'legacy unresolved',
};

function canonicalIds(ids: string[]): string[] {
  return Array.from(new Set(ids.filter(Boolean))).sort();
}

export function KnowledgePropagationSelector({
  items,
  value,
  onChange,
  title = 'Knowledge propagation',
  description = 'Choose only the resources that are relevant to this destination.',
  loading = false,
  error = null,
  onRetry,
  disabled = false,
  testId = 'knowledge-propagation-selector',
}: KnowledgePropagationSelectorProps) {
  const selected = new Set(value.knowledgeIds);
  const currentAction = ACTIONS.find((action) => action.value === value.action)!;
  const requiresSelection =
    value.action === 'reference' || value.action === 'snapshot';
  const isExplicitEmpty =
    value.action === 'drop' && value.knowledgeIds.length === 0;

  const setAction = (action: KnowledgePropagationAction) => {
    onChange({
      action,
      knowledgeIds: action === 'omitted' ? [] : value.knowledgeIds,
      justification: action === 'omitted' ? '' : value.justification,
    });
  };

  const toggleItem = (id: string) => {
    const next = new Set(value.knowledgeIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ ...value, knowledgeIds: canonicalIds(Array.from(next)) });
  };

  return (
    <section
      className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/40"
      data-testid={testId}
      aria-labelledby={`${testId}-title`}
    >
      <div>
        <h3
          id={`${testId}-title`}
          className="text-sm font-semibold text-gray-900 dark:text-white"
        >
          {title}
        </h3>
        <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          {description}
        </p>
      </div>

      <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-800 dark:border-indigo-800/60 dark:bg-indigo-950/40 dark:text-indigo-200">
        Knowledge is non-authoritative. Requirements, acceptance criteria,
        contracts and workflows remain the source of truth.
      </div>

      <fieldset className="mt-3" disabled={disabled}>
        <legend className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Decision mode
        </legend>
        <div
          className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-4"
          role="radiogroup"
          aria-label="Knowledge propagation mode"
        >
          {ACTIONS.map((action) => {
            const active = value.action === action.value;
            return (
              <label
                key={action.value}
                className={`cursor-pointer rounded-lg border px-2 py-2 text-center text-xs font-medium transition-colors ${
                  active
                    ? 'border-indigo-400 bg-indigo-100 text-indigo-800 dark:border-indigo-500 dark:bg-indigo-900/40 dark:text-indigo-200'
                    : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'
                }`}
              >
                <input
                  type="radio"
                  name={`${testId}-mode`}
                  value={action.value}
                  checked={active}
                  onChange={() => setAction(action.value)}
                  className="sr-only"
                />
                {action.label}
              </label>
            );
          })}
        </div>
        <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
          {currentAction.help}
        </p>
      </fieldset>

      {loading ? (
        <div
          className="mt-3 flex items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 py-5 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400"
          role="status"
        >
          <RefreshCw size={14} className="animate-spin" />
          Loading Knowledge resources…
        </div>
      ) : error ? (
        <div
          className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300"
          role="alert"
        >
          <div className="flex items-start gap-2">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" />
            <span className="flex-1">{error}</span>
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="font-medium underline underline-offset-2"
              >
                Retry
              </button>
            )}
          </div>
        </div>
      ) : items.length === 0 ? (
        <div
          className="mt-3 rounded-lg border border-dashed border-gray-300 py-5 text-center dark:border-gray-700"
          role="status"
        >
          <BookOpen
            size={22}
            className="mx-auto text-gray-300 dark:text-gray-600"
          />
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            No Knowledge resources are available
          </p>
          <p className="mt-0.5 text-xs text-gray-400">
            The omitted decision remains explicit and safe.
          </p>
        </div>
      ) : (
        <fieldset className="mt-3" disabled={disabled || value.action === 'omitted'}>
          <legend className="text-xs font-medium text-gray-600 dark:text-gray-300">
            Resources
          </legend>
          <div className="mt-1.5 max-h-44 space-y-1.5 overflow-y-auto">
            {items.map((item) => {
              const checked = selected.has(item.id);
              const originClass = item.origin_class
                ? ORIGIN_CLASS_LABELS[item.origin_class] || item.origin_class
                : null;
              return (
                <label
                  key={item.id}
                  className={`flex cursor-pointer items-start gap-2 rounded-lg border px-2.5 py-2 ${
                    checked
                      ? 'border-indigo-300 bg-indigo-50 dark:border-indigo-700 dark:bg-indigo-950/30'
                      : 'border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800'
                  } ${
                    value.action === 'omitted' ? 'cursor-not-allowed opacity-60' : ''
                  }`}
                >
                  <span
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                      checked
                        ? 'border-indigo-500 bg-indigo-500 text-white'
                        : 'border-gray-300 dark:border-gray-600'
                    }`}
                    aria-hidden="true"
                  >
                    {checked && <Check size={11} />}
                  </span>
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={checked}
                    onChange={() => toggleItem(item.id)}
                    aria-label={`Select ${item.title}`}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
                        {item.title}
                      </span>
                      {item.stale && (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                          stale
                        </span>
                      )}
                      {originClass && (
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-700 dark:text-slate-200">
                          {originClass}
                        </span>
                      )}
                    </span>
                    {item.description && (
                      <span className="mt-0.5 block line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                        {item.description}
                      </span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>
      )}

      {value.action !== 'omitted' && (
        <div className="mt-3">
          <label
            htmlFor={`${testId}-justification`}
            className="block text-xs font-medium text-gray-600 dark:text-gray-300"
          >
            Relevance justification <span className="text-red-500">*</span>
          </label>
          <textarea
            id={`${testId}-justification`}
            value={value.justification}
            onChange={(event) =>
              onChange({ ...value, justification: event.target.value })
            }
            disabled={disabled}
            rows={2}
            placeholder={
              value.action === 'drop'
                ? 'Explain why these resources must not propagate…'
                : 'Explain why the selected resources are relevant…'
            }
            className="mt-1 w-full resize-none rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
          />
        </div>
      )}

      <div className="mt-2 min-h-5 text-xs">
        {isExplicitEmpty ? (
          <p className="font-medium text-amber-700 dark:text-amber-300">
            Explicit empty will be saved: all current assignments are disabled,
            and later refreshes cannot restore them.
          </p>
        ) : requiresSelection && value.knowledgeIds.length === 0 ? (
          <p className="text-amber-700 dark:text-amber-300">
            Select at least one resource for this mode.
          </p>
        ) : value.action === 'omitted' ? (
          <p className="text-gray-500 dark:text-gray-400">
            No resource starts selected. Omitted never means “select all”.
          </p>
        ) : !value.justification.trim() ? (
          <p className="text-amber-700 dark:text-amber-300">
            A justification is required for this decision.
          </p>
        ) : (
          <p className="text-emerald-700 dark:text-emerald-300">
            Explicit decision ready to save.
          </p>
        )}
      </div>
    </section>
  );
}
