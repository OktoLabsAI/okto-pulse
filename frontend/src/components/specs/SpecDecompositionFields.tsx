import type { SpecDecompositionInput } from '@/types';

export const decompositionFields = [
  { score: 'breakdown_completeness', justification: 'breakdown_justification', label: 'Breakdown completeness' },
  { score: 'granularity', justification: 'granularity_justification', label: 'Granularity' },
  { score: 'dependency_coherence', justification: 'dependency_justification', label: 'Dependency coherence' },
  { score: 'test_coverage_quality', justification: 'test_coverage_justification', label: 'Test coverage quality' },
  { score: 'overall_score', justification: 'overall_justification', label: 'Overall decomposition' },
] as const;

export function emptyDecomposition(): SpecDecompositionInput {
  return { breakdown_completeness: 0, breakdown_justification: '', granularity: 0,
    granularity_justification: '', dependency_coherence: 0, dependency_justification: '',
    test_coverage_quality: 0, test_coverage_justification: '', overall_score: 0,
    overall_justification: '', recommendation: 'request_changes' };
}

export function decompositionReady(value: SpecDecompositionInput) {
  return decompositionFields.every(({ score, justification }) =>
    Number.isInteger(value[score]) && value[score] >= 0 && value[score] <= 100
    && value[justification].trim().length >= 10);
}

export function SpecDecompositionFields({ value, onChange, disabled = false }: {
  value: SpecDecompositionInput; onChange: (value: SpecDecompositionInput) => void; disabled?: boolean;
}) {
  return <fieldset disabled={disabled} className="space-y-3">
    <legend className="mb-2 text-sm font-semibold text-surface-900 dark:text-white">Your decomposition assessment</legend>
    <p className="text-xs text-surface-600 dark:text-surface-300">
      Review the planned Tasks, Tests and their dependencies. Each score needs its own justification; this opinion retains your authorship.
    </p>
    <div className="grid gap-3 sm:grid-cols-2">
      {decompositionFields.map(({ score, justification, label }) => <div key={score}
        className="space-y-2 rounded-lg border border-surface-200 p-3 dark:border-surface-700">
        <label className="block text-xs font-semibold text-surface-800 dark:text-surface-100">
          {label} <span className="float-right tabular-nums">{value[score]}/100</span>
          <input aria-label={`${label} score`} type="range" min={0} max={100} step={1}
            className="mt-2 w-full accent-violet-500" value={value[score]}
            onChange={event => onChange({ ...value, [score]: Number(event.target.value) })} />
        </label>
        <label className="block text-xs text-surface-600 dark:text-surface-300">{label} justification
          <textarea aria-label={`${label} justification`} rows={2} value={value[justification]}
            onChange={event => onChange({ ...value, [justification]: event.target.value })}
            className="mt-1 w-full rounded border border-surface-300 bg-white p-2 text-xs dark:border-surface-600 dark:bg-surface-900" />
        </label>
      </div>)}
    </div>
    <label className="block text-xs font-medium text-surface-700 dark:text-surface-200">Decomposition recommendation
      <select aria-label="Decomposition recommendation" value={value.recommendation}
        onChange={event => onChange({ ...value, recommendation: event.target.value as SpecDecompositionInput['recommendation'] })}
        className="mt-1 block rounded border border-surface-300 bg-white p-2 dark:border-surface-600 dark:bg-surface-900">
        <option value="request_changes">Request changes</option><option value="approve">Approve</option><option value="reject">Reject</option>
      </select>
    </label>
  </fieldset>;
}
