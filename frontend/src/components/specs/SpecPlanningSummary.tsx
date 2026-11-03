import { PulseLoader } from '@/components/shared/PulseLoader';
import type { SpecPlanningReadiness, SpecValidationSummary } from '@/types';

const labels: Record<string, string> = {
  spec_validation: 'Spec quality', decomposition: 'Task decomposition',
  requirement_lint: 'Requirement lint', curated_checklist: 'Checklist', policy_compliance: 'Policy compliance',
  acceptance_coverage: 'Acceptance criteria', test_card_coverage: 'Test planning',
  business_coverage: 'Functional and business requirements', technical_coverage: 'Technical requirements',
  contract_coverage: 'Contracts', integration_coverage: 'Integration requirements', observability_coverage: 'Observability requirements',
  task_requirement_links: 'Task allocation', decision_verification: 'Decision verification plans',
  code_evidence: 'Code evidence', architecture: 'Architecture', resource_coverage: 'Resource allocation',
};
const humanize = (value: string) => value.replace(/_/g, ' ');

const verdicts = {
  validated: ['Spec validated', 'All required planning checks passed for this edition.'],
  validated_with_exception: ['Spec validated with exceptions', 'Validation is complete with recorded human exceptions.'],
  ready_to_conclude: ['Ready to conclude validation', 'The required checks are satisfied. Validation still needs to be concluded.'],
  not_validated: ['Spec not yet validated', 'Required planning checks or the validation conclusion are still pending.'],
  unavailable: ['Validation could not be confirmed', 'Refresh or check your access to confirm this edition’s validation.'],
  not_required: ['Validation not required', 'Validation is disabled and the other required planning checks are satisfied.'],
} as const;

export function SpecValidationSummaryCard({ summary, edition, loading, onAction, actionLabel }: {
  summary?: SpecValidationSummary;
  edition: number;
  loading: boolean;
  onAction?: () => void;
  actionLabel?: string;
}) {
  if (loading) return <PulseLoader size="sm" label="Checking validation…" className="py-4" />;
  const current = summary?.contract_version === 'validation-summary/v1' && summary.edition === edition
    && Object.prototype.hasOwnProperty.call(verdicts, summary.verdict) ? summary : undefined;
  const verdict = current?.verdict ?? 'unavailable';
  const [title, description] = verdicts[verdict];
  const validated = verdict === 'validated' || verdict === 'validated_with_exception';
  return <section aria-label="Validation result" data-testid="spec-validation-summary"
    className={`rounded-xl border p-4 ${validated
      ? 'border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/20'
      : 'border-surface-200 bg-surface-50 dark:border-surface-700 dark:bg-surface-900/40'}`}>
    <h3 className="text-base font-semibold text-surface-900 dark:text-white">{title}</h3>
    <p className="mt-2 text-sm text-surface-600 dark:text-surface-300">{description}</p>
    {current?.blockers_count != null && current.blockers_count > 0 && <p className="mt-2 text-xs text-surface-600 dark:text-surface-300">
      {current.blockers_count} required {current.blockers_count === 1 ? 'check needs' : 'checks need'} attention.
    </p>}
    {current?.exception_count != null && current.exception_count > 0 && <p className="mt-2 text-xs text-surface-600 dark:text-surface-300">
      {current.exception_count} recorded {current.exception_count === 1 ? 'exception' : 'exceptions'}. See Details for their scope.
    </p>}
    {current?.next_action && onAction && <button type="button" onClick={onAction} className="btn btn-primary mt-3 text-sm">
      {actionLabel ?? 'Review pending check'}
    </button>}
  </section>;
}

export function SpecPlanningSummary({ readiness, loading, onReview }: {
  readiness?: SpecPlanningReadiness;
  loading: boolean;
  onReview?: Partial<Record<string, () => void>>;
}) {
  if (loading) return <PulseLoader size="sm" label="Checking planning readiness…" className="py-4" />;
  const state = readiness?.state ?? 'unavailable';
  const pending = readiness?.components.filter(item => item.state !== 'ready') ?? [];
  const title = state === 'ready' ? 'Planning checks satisfied'
    : state === 'blocked' ? 'Planning needs attention' : 'Planning readiness unavailable';
  return <section className="rounded-xl border border-surface-200 bg-surface-50 p-4 dark:border-surface-700 dark:bg-surface-900/40"
    aria-label="Planning readiness" data-testid="spec-planning-summary">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold text-surface-900 dark:text-white">{title}</h3>
      <span className={`rounded-full px-2 py-1 text-xs font-semibold ${state === 'ready'
        ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
        : 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200'}`}>
        {state === 'ready' ? 'Ready' : state === 'blocked' ? `${pending.length} pending` : 'Unavailable'}
      </span>
    </div>
    <p className="mt-2 text-xs text-surface-600 dark:text-surface-300">
      {state === 'ready' ? 'All applicable planning checks allow promotion. Conclusion still requires authorization.'
        : state === 'blocked' ? 'Resolve the required checks before promotion. Individual assessments remain available below.'
          : 'Some checks could not be verified. This does not count as approval; refresh or review access.'}
    </p>
    {readiness && <details className="mt-3">
      <summary className="cursor-pointer text-xs font-semibold text-surface-700 dark:text-surface-200">Review planning checks</summary>
      <div className="mt-2 divide-y divide-surface-200 dark:divide-surface-700">
        {readiness.components.map(item => <details key={item.key} className="group py-2">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-xs">
            <span className="font-medium text-surface-800 dark:text-surface-100">{labels[item.key] ?? humanize(item.key)}</span>
            <span className={item.state === 'ready' ? 'text-emerald-700 dark:text-emerald-300' : 'text-amber-700 dark:text-amber-300'}>
              {item.mode === 'off' ? 'Off' : item.applicable === false ? 'Not applicable'
                : item.state === 'unavailable' ? 'Unavailable' : item.state === 'blocked' ? 'Required'
                  : item.mode === 'advisory' ? 'Advisory' : 'Satisfied'}
            </span>
          </summary>
          <div className="mt-2 space-y-2 pl-3 text-xs text-surface-600 dark:text-surface-300">
            <p>{item.message || (item.reason_codes.length ? item.reason_codes.map(humanize).join('; ') : humanize(item.native_result ?? item.state))}</p>
            {item.next_action && onReview?.[item.key] && <button type="button" className="btn btn-secondary text-xs"
              onClick={onReview[item.key]}>Review {labels[item.key] ?? humanize(item.key)}</button>}
          </div>
        </details>)}
      </div>
    </details>}
  </section>;
}
