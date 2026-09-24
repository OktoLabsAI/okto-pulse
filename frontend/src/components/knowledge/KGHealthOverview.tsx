import { Activity, ArrowDown, CheckCircle2, Database, Inbox, ShieldCheck } from 'lucide-react';
import type { KGHealth } from '@/services/kg-health-api';
import { ReadinessHelp } from './ReadinessHelp';

/** Presentation only: all facts come from the existing health snapshot. */
export function KGHealthOverview({ health, stale }: { health: KGHealth; stale: boolean }) {
  const state = health.overall_state;
  const healthy = state === 'healthy' || state === 'fresh';
  const needsRecovery = ['recovery_needed', 'quarantined', 'corrupted', 'failed'].includes(state ?? '');
  const atRisk = state === 'at_risk' || state === 'backpressure';
  const label = stale ? 'Snapshot needs a refresh'
    : healthy ? 'Operational'
    : needsRecovery ? 'Component unavailable'
    : atRisk ? 'Needs attention' : 'Status unknown';
  const metricsAvailable = health.metric_status === 'available';
  const statusColor = healthy && !stale
    ? 'text-emerald-700 dark:text-emerald-300 bg-emerald-100 dark:bg-emerald-500/15'
    : 'text-amber-800 dark:text-amber-300 bg-amber-100 dark:bg-amber-500/15';
  const guidance = stale
    ? 'The last refresh failed. These are the previous observations, not a new health assessment. Refresh before deciding on an action.'
    : healthy
      ? 'No recovery is indicated by the reported health state. Review processing queues separately: a healthy database can still have pending knowledge work.'
      : needsRecovery
        ? 'The reported component state limits the affected operations. Read the component and reason in diagnostics; this view cannot repair the graph.'
        : atRisk
          ? 'Inspect the reported issues and processing queues first. A warning does not by itself mean the database needs rebuilding.'
          : 'The backend has not supplied a recognized health state. Check diagnostics; missing observations are not proof of a healthy or a corrupt graph.';

  return (
    <section id="kg-health-overview" aria-labelledby="kg-health-overview-title" className="scroll-mt-20 space-y-4" data-testid="kg-health-overview">
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="flex flex-col gap-5 p-5 sm:flex-row sm:items-start sm:p-6">
          <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl ${statusColor}`}>
            {healthy && !stale ? <ShieldCheck className="h-6 w-6" aria-hidden /> : <Activity className="h-6 w-6" aria-hidden />}
          </div>
          <div className="min-w-0 flex-1">
            <p className="mb-1 text-xs font-semibold uppercase tracking-widest text-slate-500 dark:text-slate-400">Board health · overview</p>
            <h2 id="kg-health-overview-title" className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-white">{label}</h2>
            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-600 dark:text-slate-300">{guidance}</p>
          </div>
          <a href="#kg-health-diagnostics" className="inline-flex shrink-0 items-center gap-2 self-start rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800">
            View diagnostics <ArrowDown className="h-4 w-4" aria-hidden />
          </a>
        </div>
        <div className="grid divide-y divide-slate-200 border-t border-slate-200 dark:divide-slate-800 dark:border-slate-800 sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          <SummaryMetric icon={<Database className="h-4 w-4" aria-hidden />} label="Indexed nodes"
            value={metricsAvailable && health.total_nodes != null ? health.total_nodes.toLocaleString() : 'Not measured'}
            detail={metricsAvailable ? 'Reported by the graph snapshot' : 'Graph metrics are unavailable'}
            help="Node counts are shown only when the backend marks graph metrics as available. An unavailable reading is not zero nodes and does not establish data loss." />
          <SummaryMetric icon={<Inbox className="h-4 w-4" aria-hidden />} label="Processing queue"
            value={health.queue_depth.toLocaleString()} detail="Pending work · inspect queues below"
            help="This is the reported queue depth, not a rebuild progress percentage. Cognitive pending items, canonical debt and dead-letter queues are separate populations; do not add them as if they were unique items." />
          <SummaryMetric icon={<CheckCircle2 className="h-4 w-4" aria-hidden />} label="Global discovery"
            value={health.discovery_state ? health.discovery_state.replace(/_/g, ' ') : 'Not reported'} detail="Shared discovery graph · separate health"
            help="Global Discovery is shared across boards. Its health is independent of this board graph and must not be inferred from the board's node count." />
        </div>
      </div>
      <details className="group rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-sm dark:border-sky-900/70 dark:bg-sky-950/25">
        <summary className="cursor-pointer font-medium text-sky-900 dark:text-sky-200">What can I do here?</summary>
        <ol className="mt-3 grid gap-4 text-slate-600 dark:text-slate-300 md:grid-cols-3">
          <li><strong className="block text-slate-900 dark:text-white">1. Check the current state</strong>Read health and processing separately. Missing telemetry is not a confirmed database failure.</li>
          <li><strong className="block text-slate-900 dark:text-white">2. Resolve knowledge work</strong>Use the Cognitive Action Center to inspect supported actions and their impact. Refresh here only reloads observations.</li>
          <li><strong className="block text-slate-900 dark:text-white">3. Understand limitations</strong>Read the affected component and reason. Missing or unavailable observations do not establish a healthy graph.</li>
        </ol>
      </details>
    </section>
  );
}

function SummaryMetric({ icon, label, value, detail, help }: {
  icon: React.ReactNode; label: string; value: string; detail: string; help: string;
}) {
  return (
    <div className="min-w-0 px-5 py-4 sm:px-6">
      <div className="flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
        {icon}<span>{label}</span><ReadinessHelp label={`About ${label.toLowerCase()}`}>{help}</ReadinessHelp>
      </div>
      <p className="mt-2 break-words text-2xl font-semibold capitalize tracking-tight text-slate-900 dark:text-white">{value}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{detail}</p>
    </div>
  );
}

export function KGHealthSectionHeading({ id, eyebrow, title, description }: {
  id: string; eyebrow: string; title: string; description: string;
}) {
  return (
    <div className="mb-4">
      <p className="mb-1 text-xs font-semibold uppercase tracking-widest text-sky-700 dark:text-sky-400">{eyebrow}</p>
      <h2 id={id} className="text-lg font-semibold tracking-tight text-slate-900 dark:text-white">{title}</h2>
      <p className="mt-1 max-w-3xl text-sm text-slate-500 dark:text-slate-400">{description}</p>
    </div>
  );
}
