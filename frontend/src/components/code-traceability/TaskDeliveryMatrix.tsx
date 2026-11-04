import { CircleCheck, CircleHelp, CircleX, Clock3, MinusCircle, TriangleAlert } from 'lucide-react';
import type { Spec } from '@/types';
import type { PersistedReadinessProjection, ReadinessMetric } from '@/types/delivery-readiness';
import { deliveryItemLabel } from './deliveryItemLabel';

function PhaseState({ state, phase, current, unavailable }: {
  state: string; phase: 'implementation' | 'verification'; current: boolean; unavailable: boolean;
}) {
  const effective = current ? state : unavailable ? 'unavailable' : 'updating';
  const labels: Record<string, string> = { satisfied: phase === 'implementation' ? 'Implemented' : 'Verified',
    missing: 'Pending', partial: 'Partial', failed: 'Failed', stale: 'Needs recheck', conflict: 'Needs review',
    waived: 'Waived', not_applicable: 'Not applicable', unknown: 'Unknown', unavailable: 'Unavailable', updating: 'Updating' };
  const Icon = effective === 'satisfied' ? CircleCheck : effective === 'failed' ? CircleX
    : ['waived', 'not_applicable'].includes(effective) ? MinusCircle
      : ['conflict', 'stale'].includes(effective) ? TriangleAlert
        : ['missing', 'partial', 'updating'].includes(effective) ? Clock3 : CircleHelp;
  return <span className={`inline-flex items-center gap-2 ${effective === 'satisfied' ? 'text-green-700 dark:text-green-400'
    : effective === 'failed' ? 'text-red-700 dark:text-red-400' : 'text-gray-600 dark:text-gray-300'}`}>
    <Icon size={17} aria-hidden="true" className="shrink-0" />{labels[effective] || 'Unknown'}
  </span>;
}

function Metric({ label, metric, current, unavailable }: {
  label: string; metric?: ReadinessMetric; current: boolean; unavailable: boolean;
}) {
  const percent = current && metric?.status === 'known' && typeof metric.percent === 'number'
    && Number.isFinite(metric.percent) && metric.percent >= 0 && metric.percent <= 100 ? metric.percent : null;
  const text = !current ? unavailable ? 'Unavailable' : 'Updating'
    : metric?.status === 'not_applicable' ? 'Not applicable' : percent === null ? 'Unknown' : `${percent}%`;
  return <div className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <dt className="text-gray-600 dark:text-gray-300">{label}</dt>
    <dd className="text-xl font-semibold">{text}</dd>
    {percent !== null && <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={percent} className="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
      <div className="h-full rounded-full bg-sky-500" style={{ width: `${percent}%` }} />
    </div>}
  </div>;
}

export function TaskDeliveryMatrix({ value, cardId, spec, current, unavailable, onNextPage, onFirstPage }: {
  value: PersistedReadinessProjection | null; cardId: string; spec?: Spec | null;
  current: boolean; unavailable: boolean; onNextPage: () => void; onFirstPage: () => void;
}) {
  const summary = value?.summaries.find(row => row.card_id === cardId);
  // Titles enrich the exact assessed population; the Spec never adds rows or decides proof.
  const labelSpec = spec?.id === value?.scope.spec_id && spec?.version === value?.scope.spec_version
    && spec?.edition === value?.scope.spec_edition ? spec : null;
  const units = value?.units || [];
  return <div className="space-y-4">
    <dl aria-label="Delivery progress" className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Metric label="Implementation" metric={summary?.implementation} current={current} unavailable={unavailable} />
      <Metric label="Verification" metric={summary?.verification} current={current} unavailable={unavailable} />
    </dl>
    {units.length > 0 ? <>
      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table aria-label="Implementation and verification by requirement" className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-gray-600 dark:bg-gray-800/60 dark:text-gray-300"><tr>
            <th scope="col" className="px-4 py-3">Requirement / Objective</th>
            <th scope="col" className="px-4 py-3">Implementation</th>
            <th scope="col" className="px-4 py-3">Verification</th>
          </tr></thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">{units.map(unit => {
            const label = deliveryItemLabel(unit, labelSpec);
            return <tr key={`${unit.kind}:${unit.ref}:${unit.semantic_sha256}`}>
              <th scope="row" className="min-w-56 max-w-xl break-words px-4 py-3 font-normal">
                <span className="mb-1 block text-xs text-gray-500 dark:text-gray-400">{label.family}</span>
                {label.title}
              </th>
              <td className="px-4 py-3"><PhaseState state={unit.implementation_state} phase="implementation" current={current} unavailable={unavailable} /></td>
              <td className="px-4 py-3"><PhaseState state={unit.verification_state} phase="verification" current={current} unavailable={unavailable} /></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {value && (value.page.has_more || value.page.offset > 0) && <div className="flex flex-wrap items-center gap-3 text-xs">
        <span>{value.page.offset + 1}–{value.page.offset + units.length} of {value.page.total} items</span>
        {value.page.offset > 0 && <button type="button" className="btn btn-secondary" disabled={!current} onClick={onFirstPage}>First items</button>}
        {value.page.has_more && <button type="button" className="btn btn-secondary" disabled={!current} onClick={onNextPage}>Next items</button>}
      </div>}
    </> : current && <p className="text-gray-500 dark:text-gray-400">{summary?.population_known
      ? summary.card_type === 'test' ? 'No test scenarios are assigned to this task.' : 'No requirements or objectives are assigned to this task.'
      : 'The assigned requirements and objectives could not be confirmed.'}</p>}
  </div>;
}
