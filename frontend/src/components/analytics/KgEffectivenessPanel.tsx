import { useMemo, useState } from 'react';
import {
  AlertTriangle,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Database,
  Download,
  ExternalLink,
  Filter,
  RefreshCw,
  Search,
  ShieldAlert,
} from 'lucide-react';
import type {
  BoardKgAnalyticsResponse,
  BoardKgDiagnostic,
  BoardKgOperationalDomain,
} from './analyticsCanonicalTypes';

interface KgEffectivenessPanelProps {
  data: BoardKgAnalyticsResponse | null;
  loading: boolean;
  error: string | null;
  exporting: boolean;
  from: string;
  to: string;
  onRetry: () => void;
  onExport: () => Promise<void>;
}

function words(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function tone(value: string | null | undefined): string {
  const state = (value ?? '').toLowerCase();
  if (['available', 'healthy', 'current', 'complete'].includes(state)) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300';
  }
  if (['partial', 'at_risk', 'backpressure', 'stale', 'empty', 'informational'].includes(state)) {
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-300';
  }
  if (['unavailable', 'restricted', 'error', 'recovery_needed', 'quarantined', 'blocking'].includes(state)) {
    return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300';
  }
  return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
}

function StateBadge({ value, title }: { value: string | null | undefined; title?: string }) {
  return <span title={title} className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${tone(value)}`}>{words(value)}</span>;
}

function availabilityFact(value: number | null, state: string): string | number {
  if (value !== null) return value;
  if (state === 'empty') return 'N/A';
  return words(state);
}

function rateFact(value: number | null, state: string): string {
  if (value !== null) return `${Math.round(value * 1000) / 10}%`;
  if (state === 'empty') return 'N/A';
  return words(state);
}

function hoursFact(value: number | null, state: string): string {
  if (value !== null) return `${Math.round(value * 10) / 10}h`;
  if (state === 'empty') return 'N/A';
  return words(state);
}

function safeDrillDown(target: string | null): boolean {
  return Boolean(target && (target.startsWith('/') || target.startsWith('#')));
}

function DomainTable({ domains }: { domains: BoardKgOperationalDomain[] }) {
  if (domains.length === 0) {
    return <div className="rounded-lg border border-dashed border-gray-300 px-4 py-7 text-center text-xs text-gray-400 dark:border-gray-600">No operational domains match the current filters.</div>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <table className="w-full min-w-[900px] text-left text-xs" aria-label="KG operational debt domains">
        <thead><tr className="border-b border-gray-200 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-400 dark:border-gray-700 dark:bg-gray-900/30"><th className="px-3 py-2">Domain</th><th className="px-3 py-2">Availability</th><th className="px-3 py-2 text-right">Count</th><th className="px-3 py-2">Severity</th><th className="px-3 py-2">Age p50 / p95</th><th className="px-3 py-2">Oldest</th><th className="px-3 py-2">Reason / drill-down</th></tr></thead>
        <tbody>{domains.map((domain) => (
          <tr key={domain.domain} className="border-b border-gray-100 align-top last:border-0 dark:border-gray-700/60">
            <th className="px-3 py-3 font-semibold text-gray-800 dark:text-gray-100">{words(domain.domain)}</th>
            <td className="px-3 py-3"><StateBadge value={domain.result_state} title={domain.reason ?? undefined} /></td>
            <td className="px-3 py-3 text-right font-semibold tabular-nums">{availabilityFact(domain.count, domain.result_state)}</td>
            <td className="px-3 py-3"><StateBadge value={domain.severity} /></td>
            <td className="px-3 py-3 tabular-nums">{hoursFact(domain.age.p50_hours, domain.age.result_state)} / {hoursFact(domain.age.p95_hours, domain.age.result_state)}</td>
            <td className="px-3 py-3 tabular-nums">{hoursFact(domain.age.oldest_hours, domain.age.result_state)}</td>
            <td className="px-3 py-3"><p className="text-[10px] text-gray-500">{domain.reason ? words(domain.reason) : 'No diagnostic reason'}</p>{domain.drill_down.allowed && safeDrillDown(domain.drill_down.target) ? <a href={domain.drill_down.target!} className="mt-1 inline-flex items-center gap-1 text-[10px] font-semibold text-indigo-600 dark:text-indigo-300"><ExternalLink className="h-3 w-3" /> Open domain</a> : domain.drill_down.allowed ? <p className="mt-1 font-mono text-[10px] text-gray-400" title={domain.drill_down.target ?? undefined}>{domain.drill_down.target ?? 'Target unavailable'}</p> : <p className="mt-1 text-[10px] text-gray-400">No authorized drill-down</p>}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function DiagnosticList({ diagnostics }: { diagnostics: BoardKgDiagnostic[] }) {
  if (diagnostics.length === 0) return <div className="rounded-lg border border-dashed border-gray-300 px-4 py-6 text-center text-xs text-gray-400 dark:border-gray-600">No canonical diagnostics for this period.</div>;
  return <div className="space-y-2">{diagnostics.map((diagnostic, index) => <article key={`${diagnostic.domain}:${diagnostic.reason}:${index}`} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><div className="flex flex-wrap items-center justify-between gap-2"><h6 className="text-xs font-semibold text-gray-800 dark:text-gray-100">{words(diagnostic.domain)}</h6><StateBadge value={diagnostic.severity} /></div><p className="mt-1 text-[10px] text-gray-500 dark:text-gray-400">{words(diagnostic.reason)}</p>{diagnostic.next_step.allowed && safeDrillDown(diagnostic.next_step.target) && <a href={diagnostic.next_step.target!} className="mt-2 inline-flex items-center gap-1 text-[10px] font-semibold text-indigo-600 dark:text-indigo-300"><ExternalLink className="h-3 w-3" /> Open next step</a>}</article>)}</div>;
}

export function KgEffectivenessPanel({ data, loading, error, exporting, from, to, onRetry, onExport }: KgEffectivenessPanelProps) {
  const [search, setSearch] = useState('');
  const [resultState, setResultState] = useState('all');
  const [severity, setSeverity] = useState('all');
  const domains = useMemo(() => data?.domains ?? [], [data]);
  const resultStates = useMemo(() => Array.from(new Set(domains.map((item) => item.result_state))).sort(), [domains]);
  const severities = useMemo(() => Array.from(new Set(domains.flatMap((item) => item.severity ? [item.severity] : []))).sort(), [domains]);
  const filteredDomains = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return domains.filter((domain) => (resultState === 'all' || domain.result_state === resultState) && (severity === 'all' || domain.severity === severity) && (!needle || [domain.domain, domain.result_state, domain.severity, domain.reason, domain.drill_down.target].filter(Boolean).join(' ').toLowerCase().includes(needle)));
  }, [domains, resultState, search, severity]);
  const effectiveness = data?.effectiveness;
  const inventory = data?.cognitive_inventory;
  const policyDebt = domains.find((item) => item.domain === 'policy_projection_debt');

  return (
    <section id="analytics-kg-effectiveness" aria-labelledby="kg-effectiveness-heading" className="scroll-mt-20 rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800" data-testid="kg-effectiveness-panel">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><div className="flex items-center gap-2"><Database className="h-4 w-4 text-indigo-500" aria-hidden="true" /><h3 id="kg-effectiveness-heading" className="text-sm font-semibold text-gray-800 dark:text-gray-100">Board KG Analytics</h3></div><p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">Canonical KG health, operational debt and cognitive effectiveness. Health and metric availability remain independent facts.</p><p className="mt-1 text-[10px] text-gray-400">Period {from} through {to}{data ? ` · updated ${data.provenance.observed_at}` : ''}</p></div>
        <div className="flex flex-wrap gap-2"><button type="button" disabled={loading} onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><RefreshCw className="h-3.5 w-3.5" /> Refresh</button><button type="button" disabled={exporting || loading || data === null} onClick={() => void onExport()} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium disabled:opacity-50 dark:border-gray-600"><Download className="h-3.5 w-3.5" /> {exporting ? 'Exporting…' : 'Complete CSV'}</button></div>
      </div>

      {loading && <p className="mt-4 text-xs text-gray-500" role="status">Loading Board KG Analytics…</p>}
      {!loading && error && <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-3 py-2 dark:bg-red-950/25" role="alert"><p className="text-xs text-red-700 dark:text-red-300">{error}</p><button type="button" onClick={onRetry} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 dark:text-red-300"><RefreshCw className="h-3.5 w-3.5" /> Retry</button></div>}
      {!loading && !error && !data && <div className="mt-4 rounded-lg border border-dashed border-amber-300 bg-amber-50 px-4 py-5 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200">The canonical Board KG projection is unavailable. No health or effectiveness result was inferred.</div>}

      {!loading && !error && data && (!effectiveness || !inventory) && <div className="mt-4 rounded-lg border border-dashed border-red-300 bg-red-50 px-4 py-5 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/20 dark:text-red-200">The canonical KG payload is incomplete. Effectiveness and inventory were not inferred from other facts.</div>}
      {!loading && !error && data && effectiveness && inventory && <div className="mt-5 space-y-5">
        <div className="flex flex-wrap items-center gap-2" aria-label="KG health and availability"><span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Health</span><StateBadge value={data.health.state} title={data.health.classification_reason} /><span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Result</span><StateBadge value={data.result_state} /><span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Currentness</span><StateBadge value={data.provenance.currentness} title={data.provenance.reason ?? undefined} /><span className="text-xs text-gray-500 dark:text-gray-400">{words(data.health.classification_reason)}</span></div>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-label="KG effectiveness KPIs">{[
          { label: 'Candidate → persisted', value: rateFact(effectiveness.conversion_rate, effectiveness.state), note: `${availabilityFact(effectiveness.candidate_count, effectiveness.state)} candidates · ${availabilityFact(effectiveness.persisted_count, effectiveness.state)} persisted`, icon: BrainCircuit },
          { label: 'Effectiveness', value: rateFact(effectiveness.rate, effectiveness.state), note: `${availabilityFact(effectiveness.numerator, effectiveness.state)} / ${availabilityFact(effectiveness.denominator, effectiveness.state)}`, icon: CheckCircle2 },
          { label: 'Persistence p50', value: hoursFact(effectiveness.timing.p50_hours, effectiveness.timing.state), note: `${effectiveness.timing.sample_count} timing samples`, icon: Clock3 },
          { label: 'Persistence p95', value: hoursFact(effectiveness.timing.p95_hours, effectiveness.timing.state), note: effectiveness.timing.reason ? words(effectiveness.timing.reason) : 'Canonical timing', icon: Clock3 },
          { label: 'Cognitive inventory', value: availabilityFact(inventory.total, inventory.result_state), note: `${availabilityFact(inventory.overdue_revisits, inventory.result_state)} overdue revisits`, icon: Database },
          { label: 'Policy projection debt', value: policyDebt ? availabilityFact(policyDebt.count, policyDebt.result_state) : 'Unavailable', note: 'Independent debt domain', icon: ShieldAlert },
        ].map(({ label, value, note, icon: Icon }) => <div key={label} className="rounded-lg border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40"><div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400"><Icon className="h-3.5 w-3.5" /> {label}</div><p className="mt-1 text-lg font-bold text-gray-900 dark:text-white">{value}</p><p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">{note}</p></div>)}</div>

        <div className="grid gap-3 rounded-lg border border-gray-200 bg-gray-50 p-3 md:grid-cols-[minmax(0,1fr)_180px_180px] dark:border-gray-700 dark:bg-gray-900/30" aria-label="KG Analytics filters"><label className="relative"><span className="sr-only">Search KG domains</span><Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search domain, state, reason or target…" className="min-h-9 w-full rounded-md border border-gray-300 bg-white py-2 pl-9 pr-3 text-xs dark:border-gray-600 dark:bg-gray-800" /></label><label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Availability<select value={resultState} onChange={(event) => setResultState(event.target.value)} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800"><option value="all">All result states</option>{resultStates.map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></label><label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Severity<select value={severity} onChange={(event) => setSeverity(event.target.value)} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-800"><option value="all">All severities</option>{severities.map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></label></div>

        <div><div className="mb-2 flex items-center justify-between"><div><h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">Operational domains</h4><p className="text-[10px] text-gray-500">{filteredDomains.length} of {domains.length} shown</p></div><Filter className="h-4 w-4 text-gray-400" /></div><DomainTable domains={filteredDomains} /></div>

        <div className="grid gap-4 lg:grid-cols-2">
          <div><h4 className="mb-2 text-xs font-semibold text-gray-800 dark:text-gray-100">Cognitive inventory</h4><div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><div className="flex flex-wrap items-center justify-between gap-2"><StateBadge value={inventory.result_state} title={inventory.reason ?? undefined} /><span className="text-[10px] text-gray-400">age p50 {hoursFact(inventory.age.p50_hours, inventory.age.result_state)} · p95 {hoursFact(inventory.age.p95_hours, inventory.age.result_state)}</span></div><dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">{Object.entries(inventory.by_status).map(([status, count]) => <div key={status} className="rounded-md bg-gray-50 p-2 dark:bg-gray-900/40"><dt className="text-[10px] uppercase tracking-wide text-gray-400">{words(status)}</dt><dd className="mt-0.5 text-base font-bold">{count}</dd></div>)}{Object.keys(inventory.by_status).length === 0 && <div className="col-span-full py-3 text-center text-xs text-gray-400">No authorized inventory facts.</div>}</dl></div></div>
          <div><h4 className="mb-2 text-xs font-semibold text-gray-800 dark:text-gray-100">Provenance mix</h4><div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><div className="flex flex-wrap items-center justify-between gap-2"><StateBadge value={data.provenance_mix.result_state} title={data.provenance_mix.reason ?? undefined} /><span className="text-xs font-semibold">{availabilityFact(data.provenance_mix.total, data.provenance_mix.result_state)} total</span></div><dl className="mt-3 space-y-2">{Object.entries(data.provenance_mix.by_kind).map(([kind, value]) => <div key={kind} className="flex items-center justify-between gap-3 rounded-md bg-gray-50 px-3 py-2 text-xs dark:bg-gray-900/40"><dt>{words(kind)}</dt><dd className="font-semibold">{value.count} · {value.rate === null ? 'N/A' : rateFact(value.rate, data.provenance_mix.result_state)}</dd></div>)}</dl></div></div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2"><div><h4 className="mb-2 text-xs font-semibold text-gray-800 dark:text-gray-100">Health components</h4><div className="space-y-2">{data.health.components.map((component) => <article key={component.component} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"><div className="flex flex-wrap items-center justify-between gap-2"><h6 className="text-xs font-semibold">{words(component.component)}</h6><div className="flex gap-1.5"><StateBadge value={component.health_state} /><StateBadge value={component.result_state} /></div></div><p className="mt-1 text-[10px] text-gray-500">{words(component.classification_reason)}</p></article>)}{data.health.components.length === 0 && <div className="rounded-lg border border-dashed border-gray-300 px-4 py-6 text-center text-xs text-gray-400 dark:border-gray-600">No component facts supplied.</div>}</div></div><div><h4 className="mb-2 text-xs font-semibold text-gray-800 dark:text-gray-100">Diagnostics &amp; next steps</h4><DiagnosticList diagnostics={data.diagnostics} /></div></div>

        {data.redactions.length > 0 && <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3 dark:border-amber-800 dark:bg-amber-950/20"><div className="flex items-center gap-2 text-xs font-semibold text-amber-800 dark:text-amber-200"><AlertTriangle className="h-4 w-4" /> Authorized redactions</div><ul className="mt-2 list-inside list-disc text-[10px] text-amber-700 dark:text-amber-300">{data.redactions.map((redaction) => <li key={redaction}>{words(redaction)}</li>)}</ul></div>}

        <div className="rounded-lg border border-gray-200 p-3 text-[10px] text-gray-500 dark:border-gray-700"><p>Population {data.population_scope.accessible_count} accessible · {data.population_scope.excluded_count} excluded · restricted {data.exclusions.restricted_count}</p><p className="mt-1 text-gray-400">as_of {data.as_of} · query {data.query_fingerprint.slice(0, 12)}… · contract {data.contract_version} · method {data.effectiveness.method_version}{data.next_cursor ? ' · more canonical records available' : ''}</p></div>
      </div>}
    </section>
  );
}
