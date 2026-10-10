import type { DeliveryCompleteness } from '@/types/delivery-evidence';

export interface DeliveryCompletenessState { loading?: boolean; value?: DeliveryCompleteness | null }

export function DeliveryCompletenessView({ value, loading, compact = false }: DeliveryCompletenessState & { compact?: boolean }) {
  const valid = value && Number.isInteger(value.percent) && Number.isInteger(value.total)
    && value.total > 0 && [value.planned, value.implemented, value.verified, value.accepted].every(n => Number.isInteger(n) && n >= 0)
    && value.planned + value.implemented + value.verified + value.accepted === value.total
    && value.percent === Math.floor((value.implemented * 50 + value.verified * 80 + value.accepted * 100) / value.total)
    && typeof value.scope_sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.scope_sha256)
    && value.reason === null;
  // Maturity stages are exclusive: verified/accepted obligations already have
  // implementation proof. Status Done and narrative progress add no credit.
  const implemented = valid ? value.implemented + value.verified + value.accepted : 0;
  const implementationPercent = valid ? Math.floor(implemented * 100 / value.total) : null;
  const unavailable = loading ? 'Loading…' : value?.reason ? 'Not calculable' : 'Unavailable';
  const explanation = valid ? `${value.planned} planned · ${value.implemented} implemented · ${value.verified} verified · ${value.accepted} accepted`
    : value?.reason === 'scope_missing' ? 'Link explicit delivery obligations to this task to calculate completeness.'
    : value?.reason === 'scope_incomplete' ? 'The delivery scope is incomplete or could not be fully verified.'
    : 'Current delivery evidence could not be verified. Refresh to retry.';
  const progress = <>
    <div className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-1 ${compact ? 'text-[10px]' : 'text-xs'}`}>
      <span className="text-sky-700 dark:text-sky-300">Implementation <strong>{!loading && valid ? `${implementationPercent}%` : unavailable}</strong></span>
      <span className="text-emerald-700 dark:text-emerald-300">Verified delivery <strong>{!loading && valid ? `${value.percent}%` : unavailable}</strong></span>
    </div>
    {!loading && valid && <div className="relative h-3 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
      <div role="progressbar" aria-label="Implementation" aria-valuemin={0} aria-valuemax={100} aria-valuenow={implementationPercent!} aria-valuetext={`${implementationPercent}%. ${implemented} of ${value.total} obligations have current implementation proof`} className="absolute inset-y-0 left-0 rounded-full bg-sky-500 transition-all" style={{ width: `${implementationPercent}%` }} />
      <div role="progressbar" aria-label="Verified delivery" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!} aria-valuetext={`${value.percent}%. ${explanation}`} className="absolute inset-y-1 left-0 rounded-full bg-emerald-500 transition-all" style={{ width: `${value.percent}%` }} />
    </div>}
  </>;
  if (compact) return <div className="mt-2 space-y-1 rounded bg-sky-50 px-2 py-1.5 dark:bg-sky-950/30" title={explanation}>{progress}</div>;
  return <section aria-label="Delivery progress" className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Delivery progress</h3>
    {progress}
    <p className="text-xs text-gray-600 dark:text-gray-400">{loading ? 'Reading current delivery evidence…' : explanation}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">Blue: obligations with current implementation proof. Green: delivery maturity, including verification and acceptance — planned 0%, implemented 50%, verified 80%, accepted 100%, with equal weight per obligation. Notes and partial claims do not add credit. Gates remain independent.</p>
  </section>;
}
