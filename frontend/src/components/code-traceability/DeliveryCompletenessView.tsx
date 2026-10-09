import type { DeliveryCompleteness } from '@/types/delivery-evidence';

export interface DeliveryCompletenessState { loading?: boolean; value?: DeliveryCompleteness | null }

export function DeliveryCompletenessView({ value, loading, compact = false }: DeliveryCompletenessState & { compact?: boolean }) {
  const valid = value && Number.isInteger(value.percent) && Number.isInteger(value.total)
    && value.total > 0 && [value.planned, value.implemented, value.verified, value.accepted].every(n => Number.isInteger(n) && n >= 0)
    && value.planned + value.implemented + value.verified + value.accepted === value.total
    && value.percent === Math.floor((value.implemented * 50 + value.verified * 80 + value.accepted * 100) / value.total)
    && typeof value.scope_sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.scope_sha256)
    && value.reason === null;
  const label = loading ? 'Loading…' : valid ? `${value.percent}%` : value?.reason ? 'Not calculable' : 'Unavailable';
  const explanation = valid ? `${value.planned} planned · ${value.implemented} implemented · ${value.verified} verified · ${value.accepted} accepted`
    : value?.reason === 'scope_missing' ? 'Link explicit delivery obligations to this task to calculate completeness.'
    : value?.reason === 'scope_incomplete' ? 'The delivery scope is incomplete or could not be fully verified.'
    : 'Current delivery evidence could not be verified. Refresh to retry.';
  if (compact) return <div className="mt-2 space-y-1 rounded bg-sky-50 px-2 py-1.5 text-[10px] dark:bg-sky-950/30" title={explanation}>
    <div className="flex items-center justify-between gap-2">
      <span className="text-gray-500 dark:text-gray-400">Delivery progress</span>
      <span className="font-semibold text-sky-700 dark:text-sky-300">{label}</span>
    </div>
    {!loading && valid && <div role="progressbar" aria-label="Task delivery progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!} aria-valuetext={`${value.percent}%. ${explanation}`} className="h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"><div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${value.percent}%` }} /></div>}
  </div>;
  return <section aria-label="Delivery progress" className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Verifiable delivery progress</h3><span className="text-lg font-semibold text-sky-600 dark:text-sky-400">{label}</span></div>
    {!loading && valid && <div role="progressbar" aria-label="Delivery progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!} aria-valuetext={`${value.percent}%. ${explanation}`} className="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700"><div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${value.percent}%` }} /></div>}
    <p className="text-xs text-gray-600 dark:text-gray-400">{loading ? 'Reading current delivery evidence…' : explanation}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">Equal weight per obligation: planned 0%, implemented 50%, verified 80%, accepted 100%. Current evidence is required; notes and partial claims do not add credit. Gates remain independent.</p>
  </section>;
}
