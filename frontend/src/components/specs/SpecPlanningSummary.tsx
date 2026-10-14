import { PulseLoader } from '@/components/shared/PulseLoader';
import type { SpecPlanningReadiness } from '@/types';

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
