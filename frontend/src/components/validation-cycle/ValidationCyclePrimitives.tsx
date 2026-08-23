import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleDashed,
  Clock3,
  History,
  ShieldAlert,
  Wrench,
  XCircle,
} from 'lucide-react';
import {
  measureValidationWorkspaceInteraction,
  type ValidationWorkspaceInteraction,
} from '@/services/validation-workspace-telemetry';

export type ValidationCycleState =
  | 'not_started'
  | 'in_progress'
  | 'completed'
  | 'passed'
  | 'needs_attention'
  | 'failed';

const STATE_LABELS: Record<ValidationCycleState, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  completed: 'Completed',
  passed: 'Passed',
  needs_attention: 'Needs attention',
  failed: 'Failed',
};

const STATE_CLASSES: Record<ValidationCycleState, string> = {
  not_started:
    'border-surface-200 bg-surface-100 text-surface-600 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-300',
  in_progress:
    'border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-800 dark:bg-violet-950/35 dark:text-violet-200',
  completed:
    'border-surface-200 bg-surface-100 text-surface-700 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-200',
  passed:
    'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/35 dark:text-emerald-200',
  needs_attention:
    'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/35 dark:text-amber-200',
  failed:
    'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/35 dark:text-red-200',
};

const STATE_ICONS: Record<ValidationCycleState, typeof CheckCircle2> = {
  not_started: CircleDashed,
  in_progress: Clock3,
  completed: CheckCircle2,
  passed: CheckCircle2,
  needs_attention: ShieldAlert,
  failed: XCircle,
};

export interface ValidationCycleStatusBadgeProps {
  state: ValidationCycleState;
  label?: string;
  testId?: string;
}

export function ValidationCycleStatusBadge({
  state,
  label,
  testId,
}: ValidationCycleStatusBadgeProps) {
  const Icon = STATE_ICONS[state];
  return (
    <span
      data-testid={testId}
      data-state={state}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-bold uppercase tracking-wide ${STATE_CLASSES[state]}`}
    >
      <Icon size={11} aria-hidden="true" />
      {label ?? STATE_LABELS[state]}
    </span>
  );
}

export interface ValidationCycleHeaderProps {
  title: string;
  edition: number;
  description: string;
  icon?: ReactNode;
  actions?: ReactNode;
}

export function ValidationCycleHeader({
  title,
  edition,
  description,
  icon,
  actions,
}: ValidationCycleHeaderProps) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
            {icon}
            {title}
          </h3>
          <span className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[10px] font-semibold text-violet-700 dark:border-violet-800 dark:bg-violet-950/35 dark:text-violet-200">
            Edition {edition}
          </span>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-surface-500 dark:text-surface-400">
          {description}
        </p>
      </div>
      {actions}
    </header>
  );
}

interface ValidationCycleDisclosureProps {
  title: string;
  description: string;
  expanded: boolean;
  onToggle: () => void;
  testId: string;
  icon: ReactNode;
  count?: number | null;
  interaction: ValidationWorkspaceInteraction;
  children: ReactNode;
}

function ValidationCycleDisclosure({
  title,
  description,
  expanded,
  onToggle,
  testId,
  icon,
  count,
  interaction,
  children,
}: ValidationCycleDisclosureProps) {
  const contentId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [hasOpened, setHasOpened] = useState(expanded);
  useEffect(() => {
    if (expanded) setHasOpened(true);
  }, [expanded]);
  const handleToggle = () => {
    measureValidationWorkspaceInteraction(interaction, expanded, onToggle);
  };
  const handleEscape = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape' || !expanded) return;
    event.preventDefault();
    event.stopPropagation();
    handleToggle();
    triggerRef.current?.focus();
  };
  return (
    <section
      onKeyDown={handleEscape}
      className="overflow-hidden rounded-xl border border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/30"
    >
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={handleToggle}
        data-testid={`${testId}-toggle`}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500 dark:hover:bg-surface-800/60"
      >
        <span className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5 text-surface-400 dark:text-surface-500">
            {icon}
          </span>
          <span className="min-w-0">
            <span className="flex flex-wrap items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
              {title}
              {typeof count === 'number' && (
                <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-medium text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                  {count}
                </span>
              )}
            </span>
            <span className="mt-0.5 block text-[11px] text-surface-500 dark:text-surface-400">
              {description}
            </span>
          </span>
        </span>
        {expanded ? (
          <ChevronUp size={16} className="shrink-0 text-surface-500" aria-hidden="true" />
        ) : (
          <ChevronDown size={16} className="shrink-0 text-surface-500" aria-hidden="true" />
        )}
      </button>
      {(expanded || hasOpened) && (
        <div
          id={contentId}
          hidden={!expanded}
          data-testid={`${testId}-content`}
          className="space-y-3 border-t border-surface-200 p-4 dark:border-surface-700"
        >
          {children}
        </div>
      )}
    </section>
  );
}

export interface PreviousResultsSectionProps {
  expanded: boolean;
  onToggle: () => void;
  count?: number | null;
  children: ReactNode;
  title?: string;
  description?: string;
  testId?: string;
}

export function PreviousResultsSection({
  expanded,
  onToggle,
  count,
  children,
  title = 'Previous results',
  description = 'Earlier attempts and completed editions remain available for reference.',
  testId = 'previous-results',
}: PreviousResultsSectionProps) {
  return (
    <ValidationCycleDisclosure
      title={title}
      description={description}
      expanded={expanded}
      onToggle={onToggle}
      count={count}
      testId={testId}
      interaction="previous_results"
      icon={<History size={15} aria-hidden="true" />}
    >
      {children}
    </ValidationCycleDisclosure>
  );
}

export interface TechnicalAuditSectionProps {
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
  testId?: string;
}

export function TechnicalAuditSection({
  expanded,
  onToggle,
  children,
  testId = 'technical-audit',
}: TechnicalAuditSectionProps) {
  return (
    <ValidationCycleDisclosure
      title="Technical audit"
      description="Immutable identifiers and processing metadata for diagnostics."
      expanded={expanded}
      onToggle={onToggle}
      testId={testId}
      interaction="technical_audit"
      icon={<Wrench size={15} aria-hidden="true" />}
    >
      {children}
    </ValidationCycleDisclosure>
  );
}
