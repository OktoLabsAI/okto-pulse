import type { DeliveryCompleteness } from '@/types/delivery-evidence';

export interface DeliveryCompletenessState { loading?: boolean; value?: DeliveryCompleteness | null }

export function DeliveryCompletenessView({ value, loading, compact = false }: DeliveryCompletenessState & { compact?: boolean }) {
  const valid = value && Number.isInteger(value.percent) && Number.isInteger(value.total)
    && value.total > 0 && Number.isInteger(value.completed) && value.completed >= 0
    && value.completed <= value.total && value.percent === Math.floor(value.completed * 100 / value.total)
    && value.reason === null;
  const label = loading ? 'Loading…' : valid ? `${value.percent}%` : value?.reason ? 'Not calculable' : 'Unavailable';
  const explanation = valid ? `${value.completed} of ${value.total} obligations supported by current, complete implementation evidence.`
    : value?.reason === 'scope_missing' ? 'Link explicit delivery obligations to this task to calculate completeness.'
    : value?.reason === 'scope_incomplete' ? 'The delivery scope is incomplete or could not be fully verified.'
    : 'Current delivery evidence could not be verified. Refresh to retry.';
  if (compact) return <div className="mt-2 space-y-1 rounded bg-sky-50 px-2 py-1.5 text-[10px] dark:bg-sky-950/30" title={explanation}>
    <div className="flex items-center justify-between gap-2">
      <span className="text-gray-500 dark:text-gray-400">Delivery completeness</span>
      <span className="font-semibold text-sky-700 dark:text-sky-300">{label}</span>
    </div>
    {!loading && valid && <div role="progressbar" aria-label="Task delivery completeness" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!} aria-valuetext={`${value.percent}%, ${value.completed} of ${value.total} obligations`} className="h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"><div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${value.percent}%` }} /></div>}
  </div>;
  return <section aria-label="Delivery completeness" className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Delivery completeness</h3><span className="text-lg font-semibold text-sky-600 dark:text-sky-400">{label}</span></div>
    {valid && <div role="progressbar" aria-label="Delivery completeness" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!} aria-valuetext={`${value.percent}%, ${value.completed} of ${value.total} obligations`} className="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700"><div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${value.percent}%` }} /></div>}
    <p className="text-xs text-gray-600 dark:text-gray-400">{loading ? 'Reading current delivery evidence…' : explanation}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">Equal weight per obligation. Progress notes and partial contributions do not add credit. 100% does not approve or complete the task.</p>
  </section>;
}
