import { AlertTriangle } from 'lucide-react';
import { isDeliveryReadiness, type DeliveryReadiness, type DeliveryState, type ReadinessMetric } from '@/types/delivery-readiness';

export interface DeliveryReadinessState { loading?: boolean; value?: DeliveryReadiness | null; error?: string }
const labels: Record<DeliveryState, string> = {
  unknown: 'Unable to evaluate', implementation_pending: 'Implementation pending',
  verification_pending: 'Implementation complete · verification pending', verification_failed: 'Verification needs attention',
  ready: 'Proof complete · closure pending', delivered: 'Delivered',
  ready_with_exceptions: 'Ready with exceptions', delivered_with_exceptions: 'Completed with exceptions',
  not_applicable: 'Not applicable',
};

function Metric({ label, value, loading, compact }: { label: string; value?: ReadinessMetric; loading?: boolean; compact: boolean }) {
  const known = value?.status === 'known';
  const unit = value?.unit === 'scenario' ? 'scenarios' : 'obligations';
  const text = loading ? 'Loading…' : known ? `${value.percent}%` : value?.status === 'not_applicable' ? 'Not applicable' : 'Unable to evaluate';
  const count = known ? `${value.satisfied}/${value.applicable}` : null;
  const detail = `${text}${count ? ` · ${count} applicable ${unit}` : ''}`;
  // Read the count as words; the visual slash/separator is not spoken context.
  const accessibleValue = `${text}${known ? `, ${value.satisfied} of ${value.applicable} applicable ${unit}` : ''}`;
  const failure = !!value && (value.counts_by_state.failed > 0 || value.counts_by_state.conflict > 0);
  return <div className={`min-w-0 ${compact ? 'grid h-[62px] grid-rows-[24px_10px_28px]' : 'space-y-1.5 rounded-lg bg-gray-50 p-4 dark:bg-gray-900/60'}`}>
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-1">
      <span className={`font-medium ${label === 'Implementation' ? 'text-sky-700 dark:text-sky-300' : 'text-emerald-700 dark:text-emerald-300'}`}>{label}</span>
      <span className="text-gray-800 dark:text-gray-100"><strong>{text}</strong>{count && <span className="ml-2 text-gray-500 dark:text-gray-400">{count}</span>}</span>
    </div>
    {loading ? <div aria-hidden="true" className="h-2.5 rounded-full bg-gray-200 motion-safe:animate-pulse dark:bg-gray-700" />
      : known ? <div className="h-2.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!}
          aria-valuetext={accessibleValue} className={`h-full rounded-full motion-safe:transition-[width] ${label === 'Implementation' ? 'bg-sky-500' : 'bg-emerald-500'}`}
          style={{ width: `${value.percent}%` }} />
      </div> : compact ? <span role="status" className="sr-only">{detail}</span> : <p role="status" className="text-gray-500 dark:text-gray-400">{value?.status === 'not_applicable' ? 'No applicable work in this phase.' : 'Current proof could not be evaluated.'}</p>}
    {!loading && value && <div className={`flex flex-wrap gap-x-2 text-gray-500 dark:text-gray-400 ${compact ? 'row-start-3 content-start pt-1' : ''}`}>
      {value.not_applicable > 0 && <span>{value.not_applicable} N/A</span>}
      {value.waived > 0 && <span>{value.waived} {compact ? 'waived' : 'waived · no proof credit'}</span>}
      {failure && <span className="inline-flex items-center gap-1 text-red-700 dark:text-red-300"><AlertTriangle size={12} aria-hidden="true" />{compact ? 'Failed / conflict' : 'Failed or conflicting evidence'}</span>}
    </div>}
  </div>;
}

export function DeliveryReadinessView({ value, loading, error, compact = false, onInspect, onRefresh }: DeliveryReadinessState & {
  compact?: boolean; onInspect?: () => void; onRefresh?: () => void;
}) {
  const current = !loading && !error && isDeliveryReadiness(value) ? value : undefined;
  const incompatible = !loading && !error && value != null && !current;
  const historical = current?.summary.message_code === 'historical_completion_diverged';
  const title = current ? historical ? 'Historical completion · current qualification pending'
    : current.scope.card_type === 'test' && current.lifecycle.stored_status === 'done' && current.delivery_state === 'verification_failed'
      ? 'Execution complete · failures found' : labels[current.delivery_state] : null;
  const explanation = 'Current evidence for the applicable scope; not an agent-declared score. Percentage = satisfied ÷ applicable, rounded down. N/A is excluded; waivers do not add proof credit.';
  const inspectLabel = current?.delivery_state === 'not_applicable' ? 'View scope'
    : current?.delivery_state === 'delivered' || current?.delivery_state === 'delivered_with_exceptions' ? 'View evidence' : 'What is missing?';
  return <section aria-label="Delivery readiness" aria-busy={loading || undefined}
    className={`min-w-0 rounded-lg border border-gray-200 dark:border-gray-700 ${compact ? 'mt-2 flex h-[274px] flex-col gap-3 bg-gray-50/70 p-2 text-[10px] dark:bg-gray-900/40' : 'space-y-3 p-4 text-sm'}`}>
    {!compact && <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-gray-800 dark:text-gray-100">Delivery readiness</h3>
      {current && <span className="text-xs text-gray-500 dark:text-gray-400">Status: {current.lifecycle.stored_status.replace(/_/g, ' ')}</span>}</div>}
    <div className={compact ? 'h-[136px] shrink-0 space-y-3' : 'grid gap-4 sm:grid-cols-2'} title={explanation}>
      <Metric label="Implementation" value={current?.implementation} loading={loading} compact={compact} />
      <Metric label="Verification" value={current?.verification} loading={loading} compact={compact} />
    </div>
    <div className={compact ? 'min-h-0 flex-1 space-y-1' : 'space-y-3'}>
      {title && <p className="break-words font-medium text-gray-700 dark:text-gray-200">{title}</p>}
      {current && <p className="text-gray-500 dark:text-gray-400">{current.summary.unresolved_units === null ? 'Pending units unknown' : `${current.summary.unresolved_units} pending units`} · {current.summary.root_causes} causes</p>}
      {!loading && !current && <p role="status" className="text-amber-800 dark:text-amber-300">{compact ? 'Current readiness is unavailable. Open the diagnosis to retry.' : incompatible ? 'Readiness contract unavailable or incompatible. Refresh after updating the backend.' : error || 'Current readiness is unavailable. Refresh to retry.'}</p>}
    </div>
    {!compact && <details className="text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">How these counts work</summary><p className="mt-2">{explanation}</p></details>}
    {(onInspect || onRefresh) && <div className="flex shrink-0 flex-wrap gap-2">
      {onInspect && <button type="button" onClick={event => { event.stopPropagation(); onInspect(); }} className="rounded-md border border-gray-300 px-2 py-1 font-medium hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 dark:border-gray-600 dark:hover:bg-gray-800">{inspectLabel}</button>}
      {onRefresh && <button type="button" onClick={onRefresh} className="rounded-md border border-gray-300 px-2 py-1 dark:border-gray-600">Refresh</button>}
    </div>}
  </section>;
}
