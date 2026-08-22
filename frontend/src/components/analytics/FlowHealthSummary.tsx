import {
  Activity,
  AlertOctagon,
  ArrowRight,
  Clock3,
  History,
  ShieldAlert,
} from 'lucide-react';
import type { FlowHealthResponse } from './analyticsCanonicalTypes';
import {
  deriveFlowHealthMetrics,
  flowHealthAuthorityState,
  formatMetric,
} from './flowHealthMetrics';

interface FlowHealthSummaryProps {
  data: FlowHealthResponse | null;
  loading: boolean;
  error: string | null;
  from: string;
  to: string;
  onRetry: () => void;
  onOpenFullView: () => void;
  subjectTitles?: Record<string, string>;
  onOpenSubject?: (type: string, id: string, title: string) => void;
}

function words(value: string): string {
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function titleKey(type: string, id: string): string {
  return `${type === 'task' ? 'card' : type}:${id}`;
}

export function FlowHealthSummary({
  data,
  loading,
  error,
  from,
  to,
  onRetry,
  onOpenFullView,
  subjectTitles = {},
  onOpenSubject,
}: FlowHealthSummaryProps) {
  const metrics = data ? deriveFlowHealthMetrics(data) : null;
  const authorityState = data ? flowHealthAuthorityState(data) : null;
  const cards = metrics ? [
    {
      label: 'Active blockers',
      value: formatMetric(metrics.blockerOccurrences),
      note: metrics.blockerSubjects === null ? 'Unique entities N/A' : `${metrics.blockerSubjects} unique entities`,
      icon: AlertOctagon,
    },
    {
      label: 'Rejected WIP',
      value: formatMetric(metrics.rejectedWip),
      note: metrics.rejectedP95Hours === null ? 'p95 age N/A' : `p95 age ${formatMetric(metrics.rejectedP95Hours, 'h')}`,
      icon: ShieldAlert,
    },
    {
      label: 'Recovery rate',
      value: metrics.recoveryRate === null ? 'N/A' : `${Math.round(metrics.recoveryRate * 100)}%`,
      note: metrics.recoverySample === null ? 'Sample N/A' : `n = ${metrics.recoverySample}`,
      icon: History,
    },
    {
      label: 'Dependency wait',
      value: formatMetric(metrics.dependencyWaitP50Hours, 'h'),
      note: metrics.dependencyDepth === null ? 'Depth N/A' : `Longest chain ${metrics.dependencyDepth}`,
      icon: Clock3,
    },
    {
      label: 'Open bugs',
      value: formatMetric(metrics.openBugs),
      note: metrics.highSeverityBugs === null ? 'Severity N/A' : `${metrics.highSeverityBugs} high severity`,
      icon: AlertOctagon,
    },
  ] : [];

  return (
    <section
      id="analytics-flow-health"
      aria-labelledby="flow-health-summary-heading"
      className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
      data-testid="flow-health-summary"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-violet-500" aria-hidden="true" />
            <h3 id="flow-health-summary-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">Flow Health</h3>
          </div>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Operational blockers, governed rework, dependency wait, defects and policy provenance.
          </p>
          <p className="mt-1 text-[10px] text-gray-400">Period {from} through {to}{data ? ` · updated ${data.as_of}` : ''}</p>
        </div>
        <button
          type="button"
          onClick={onOpenFullView}
          className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-2 text-xs font-semibold text-white"
        >
          Open full view <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      {loading && <p className="mt-4 text-xs text-gray-500" role="status">Loading Flow Health…</p>}
      {!loading && error && (
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 dark:bg-red-900/20" role="alert">
          <span className="text-xs text-red-700 dark:text-red-300">{error}</span>
          <button type="button" onClick={onRetry} className="text-xs font-semibold text-red-700 dark:text-red-300">Retry</button>
        </div>
      )}
      {!loading && !error && data && (
        <>
          {authorityState !== 'available' && (
            <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-200" role="status">
              Flow Health is {authorityState}. Missing authority is not classified as healthy.
            </p>
          )}
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5" aria-label="Flow Health summary metrics">
            {cards.map(({ label, value, note, icon: Icon }) => (
              <article key={label} className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
                <p className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400"><Icon className="h-3.5 w-3.5" aria-hidden="true" /> {label}</p>
                <p className="mt-1 text-xl font-bold text-gray-900 dark:text-white">{value}</p>
                <p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">{note}</p>
              </article>
            ))}
          </div>
          {data.items.length > 0 && (
            <div className="mt-4 rounded-lg border border-gray-200 dark:border-gray-700">
              <div className="border-b border-gray-200 px-3 py-2 dark:border-gray-700"><h4 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Priority subjects</h4></div>
              <ul className="divide-y divide-gray-100 dark:divide-gray-700">
                {data.items.slice(0, 3).map((item) => {
                  const key = titleKey(item.subject.type, item.subject.id);
                  const title = item.subject.title
                    ?? subjectTitles[key]
                    ?? `${words(item.subject.type)} ${item.subject.id}`;
                  return (
                    <li key={key} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                      <button type="button" title={`${item.subject.type}:${item.subject.id}`} onClick={() => onOpenSubject?.(item.subject.type, item.subject.id, title)} className="min-w-0 truncate text-left text-xs font-semibold text-violet-700 dark:text-violet-300">{title}</button>
                      <span className="flex items-center gap-2 text-[10px] text-gray-500"><span>{words(item.current_episode?.state ?? item.state)}</span><span>v{data.effective_policy.version}</span></span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          <p className="mt-3 text-[10px] text-gray-400">
            Effective policy v{data.effective_policy.version} · {data.effective_policy.general_stale_hours}h general · {data.effective_policy.rejected_stale_hours}h rejected · {String(data.effective_policy.source ?? data.effective_policy.authority_ref ?? 'default authority')}
          </p>
        </>
      )}
    </section>
  );
}
