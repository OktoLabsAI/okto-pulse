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
  const displayText = compact && !known ? loading ? '…' : value?.status === 'not_applicable' ? 'N/A' : '—' : text;
  const count = known ? `${value.satisfied}/${value.applicable}` : null;
  const detail = `${text}${count ? ` · ${count} applicable ${unit}` : ''}`;
  // Read the count as words; the visual slash/separator is not spoken context.
  const accessibleValue = `${text}${known ? `, ${value.satisfied} of ${value.applicable} applicable ${unit}` : ''}`;
  const failure = !!value && (value.counts_by_state.failed > 0 || value.counts_by_state.conflict > 0);
  return <div className="min-w-0">
    <div className={`font-medium text-gray-600 dark:text-gray-300 ${compact ? 'text-[10px] leading-4' : 'text-xs leading-4'}`}>{label}</div>
    <div className={`flex items-start gap-1 ${compact ? 'mt-1 h-7' : 'mt-2 h-10'}`}>
      <strong title={text} className={known ? `tabular-nums leading-none ${compact ? 'text-base' : 'text-xl'} ${label === 'Implementation' ? 'text-sky-700 dark:text-sky-400' : 'text-emerald-700 dark:text-emerald-400'}` : 'text-xs font-medium leading-4 text-gray-600 dark:text-gray-300'}>{displayText}</strong>
      {compact && failure && <span title="Failed or conflicting evidence" className="text-red-700 dark:text-red-300"><AlertTriangle size={12} aria-hidden="true" /><span className="sr-only">Failed or conflicting evidence</span></span>}
    </div>
    {loading ? <div aria-hidden="true" className="h-1.5 rounded-full bg-gray-200 motion-safe:animate-pulse dark:bg-gray-700" />
      : known ? <div className="h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value.percent!}
          aria-valuetext={accessibleValue} className={`h-full rounded-full motion-safe:transition-[width] ${label === 'Implementation' ? 'bg-sky-500' : 'bg-emerald-500'}`}
          style={{ width: `${value.percent}%` }} />
      </div> : <div className="h-1.5"><span role="status" className="sr-only">{detail}</span></div>}
    {!compact && count && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{count} applicable {unit}</p>}
    {!compact && !loading && value && <div className="mt-1 flex flex-wrap gap-x-2 text-gray-500 dark:text-gray-400">
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
  const verificationOnly = current?.scope.card_type === 'test' && current.implementation.status === 'not_applicable';
  const incompatible = !loading && !error && value != null && !current;
  const historical = current?.summary.message_code === 'historical_completion_diverged';
  const title = current ? historical ? 'Historical completion · current qualification pending'
    : current.scope.card_type === 'test' && current.lifecycle.stored_status === 'done' && current.delivery_state === 'verification_failed'
      ? 'Execution complete · failures found' : labels[current.delivery_state] : null;
  const explanation = 'Current evidence for the applicable scope; not an agent-declared score. Percentage = satisfied ÷ applicable, rounded down. N/A is excluded; waivers do not add proof credit.';
  const inspectLabel = current?.delivery_state === 'not_applicable' ? 'View scope'
    : current?.delivery_state === 'delivered' || current?.delivery_state === 'delivered_with_exceptions' ? 'View evidence' : 'What is missing?';
  if (compact) return <section aria-label="Delivery readiness" aria-busy={loading || undefined}
    className="relative mt-2 min-w-0 rounded-md py-1" title={title || (loading ? 'Loading delivery readiness' : 'Current readiness is unavailable. Open Delivery to retry.')}>
    {historical && <p role="status" className="mb-1 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
      Execution done · Delivery pending
    </p>}
    <div className={`grid ${verificationOnly ? 'grid-cols-1' : 'grid-cols-2'} gap-3`}>
      {!verificationOnly && <Metric label="Implementation" value={current?.implementation} loading={loading} compact />}
      <Metric label="Verification" value={current?.verification} loading={loading} compact />
    </div>
    {onInspect && <button type="button" aria-label="Open delivery details" title="Open delivery details"
      onClick={event => { event.stopPropagation(); onInspect(); }}
      className="absolute inset-0 rounded-md hover:bg-gray-500/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500" />}
  </section>;
  return <section aria-label="Delivery readiness" aria-busy={loading || undefined}
    className="min-w-0 rounded-lg border border-gray-200 dark:border-gray-700 space-y-3 p-4 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-gray-800 dark:text-gray-100">Delivery readiness</h3>
      {current && <span className="text-xs text-gray-500 dark:text-gray-400">Status: {current.lifecycle.stored_status.replace(/_/g, ' ')}</span>}</div>
    <div className={`grid ${verificationOnly ? 'grid-cols-1' : 'grid-cols-2'} gap-4 py-2`} title={explanation}>
      {!verificationOnly && <Metric label="Implementation" value={current?.implementation} loading={loading} compact={false} />}
      <Metric label="Verification" value={current?.verification} loading={loading} compact={false} />
    </div>
    <div className="space-y-3">
      {title && <p className="break-words font-medium text-gray-700 dark:text-gray-200">{title}</p>}
      {current && <p className="text-gray-500 dark:text-gray-400">{current.summary.unresolved_units === null ? 'Pending units unknown' : `${current.summary.unresolved_units} pending units`} · {current.summary.root_causes} causes</p>}
      {!loading && !current && <p role="status" className="text-amber-800 dark:text-amber-300">{incompatible ? 'Readiness contract unavailable or incompatible. Refresh after updating the backend.' : error || 'Current readiness is unavailable. Refresh to retry.'}</p>}
    </div>
    <details className="text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">How these counts work</summary><p className="mt-2">{explanation}</p></details>
    {(onInspect || onRefresh) && <div className="flex shrink-0 flex-wrap gap-2">
      {onInspect && <button type="button" onClick={event => { event.stopPropagation(); onInspect(); }} className="rounded-md border border-gray-300 px-2 py-1 font-medium hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 dark:border-gray-600 dark:hover:bg-gray-800">{inspectLabel}</button>}
      {onRefresh && <button type="button" onClick={onRefresh} className="rounded-md border border-gray-300 px-2 py-1 dark:border-gray-600">Refresh</button>}
    </div>}
  </section>;
}
