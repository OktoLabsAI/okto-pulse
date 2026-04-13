import { useEffect, useState, useCallback } from 'react';
import {
  CheckCircle,
  XCircle,
  Clock,
  ArrowRight,
  FileText,
  Target,
  AlertTriangle,
  Loader2,
  MessageSquare,
  Layers,
  GitBranch,
  Scale,
  Globe,
  Bug,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';

// ---------------------------------------------------------------------------
// Props & Types
// ---------------------------------------------------------------------------

interface EntityDetailProps {
  boardId: string;
  entityType: 'ideation' | 'spec' | 'refinement' | 'sprint';
  entityId: string;
  from: string;
  to: string;
}

interface SpecAnalytics {
  spec_id: string;
  title: string;
  status: string | null;
  total_ac: number;
  covered_ac: number;
  ac_details?: { index: number; text: string; covered: boolean }[];
  total_fr?: number;
  fr_details?: { index: number; text: string; has_rule: boolean; has_contract: boolean }[];
  scenario_statuses: { id: string; title: string; status: string }[];
  cards: {
    id: string;
    title: string;
    status: string | null;
    is_test: boolean;
    card_type?: string;
    completeness: number | null;
    drift: number | null;
    created_at: string | null;
    updated_at: string | null;
  }[];
  avg_cycle_hours: number | null;
  derivation: { ideation_id: string | null; refinement_id: string | null };
  business_rules: any[];
  api_contracts: any[];
  rules_coverage: number;
  contracts_coverage: number;
  bugs_count?: number;
}

interface IdeationAnalytics {
  ideation_id: string;
  title: string;
  status: string | null;
  complexity: string | null;
  scope_assessment: { domains: number; ambiguity: number; dependencies: number } | null;
  refinement_count: number;
  spec_count: number;
  qa_count: number;
  created_at: string | null;
}

interface RefinementData {
  id: string;
  title: string;
  description: string | null;
  status: string;
  version: number;
  ideation_id: string;
  in_scope: string[] | null;
  out_of_scope: string[] | null;
  specs: { id: string; title: string; status: string }[];
  knowledge_bases: { id: string; title: string }[];
}

interface SprintAnalytics {
  sprint_id: string;
  title: string;
  status: string;
  spec_id: string;
  spec_version: number;
  tasks_total: number;
  tasks_done: number;
  tasks_cancelled: number;
  tasks_in_progress: number;
  progress: number;
  avg_completeness: number | null;
  avg_drift: number | null;
  avg_cycle_hours: number | null;
  cards: { id: string; title: string; status: string | null; card_type: string; completeness: number | null; drift: number | null; cycle_hours: number | null }[];
  evaluations_total: number;
  evaluations_non_stale: number;
  approvals: number;
  avg_eval_score: number | null;
  scoped_scenarios: { id: string; title: string; status: string }[];
  scenario_coverage: number;
  comparison: { sprint_id: string; title: string; status: string; tasks_total: number; tasks_done: number; avg_completeness: number | null; avg_drift: number | null; is_current: boolean }[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function completenessColor(v: number | null): string {
  if (v == null) return 'text-gray-400';
  if (v >= 90) return 'text-emerald-600 dark:text-emerald-400';
  if (v >= 70) return 'text-blue-600 dark:text-blue-400';
  if (v >= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function completenessBg(v: number | null): string {
  if (v == null) return 'bg-gray-100 dark:bg-gray-700';
  if (v >= 90) return 'bg-emerald-100 dark:bg-emerald-900/30';
  if (v >= 70) return 'bg-blue-100 dark:bg-blue-900/30';
  if (v >= 50) return 'bg-amber-100 dark:bg-amber-900/30';
  return 'bg-red-100 dark:bg-red-900/30';
}

function driftColor(v: number | null): string {
  if (v == null) return 'text-gray-400';
  if (v <= 10) return 'text-emerald-600 dark:text-emerald-400';
  if (v <= 25) return 'text-blue-600 dark:text-blue-400';
  if (v <= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function driftBg(v: number | null): string {
  if (v == null) return 'bg-gray-100 dark:bg-gray-700';
  if (v <= 10) return 'bg-emerald-100 dark:bg-emerald-900/30';
  if (v <= 25) return 'bg-blue-100 dark:bg-blue-900/30';
  if (v <= 50) return 'bg-amber-100 dark:bg-amber-900/30';
  return 'bg-red-100 dark:bg-red-900/30';
}

function statusBadge(status: string | null): JSX.Element {
  const labels: Record<string, { bg: string; text: string }> = {
    draft: { bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-700 dark:text-gray-300' },
    review: { bg: 'bg-blue-100 dark:bg-blue-900/30', text: 'text-blue-700 dark:text-blue-300' },
    approved: { bg: 'bg-indigo-100 dark:bg-indigo-900/30', text: 'text-indigo-700 dark:text-indigo-300' },
    in_progress: { bg: 'bg-amber-100 dark:bg-amber-900/30', text: 'text-amber-700 dark:text-amber-300' },
    done: { bg: 'bg-emerald-100 dark:bg-emerald-900/30', text: 'text-emerald-700 dark:text-emerald-300' },
    cancelled: { bg: 'bg-red-100 dark:bg-red-900/30', text: 'text-red-700 dark:text-red-300' },
    evaluating: { bg: 'bg-purple-100 dark:bg-purple-900/30', text: 'text-purple-700 dark:text-purple-300' },
    refined: { bg: 'bg-teal-100 dark:bg-teal-900/30', text: 'text-teal-700 dark:text-teal-300' },
    ready: { bg: 'bg-blue-100 dark:bg-blue-900/30', text: 'text-blue-700 dark:text-blue-300' },
    automated: { bg: 'bg-cyan-100 dark:bg-cyan-900/30', text: 'text-cyan-700 dark:text-cyan-300' },
    passed: { bg: 'bg-emerald-100 dark:bg-emerald-900/30', text: 'text-emerald-700 dark:text-emerald-300' },
    failed: { bg: 'bg-red-100 dark:bg-red-900/30', text: 'text-red-700 dark:text-red-300' },
  };
  const s = status || 'draft';
  const style = labels[s] ?? labels.draft;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style.bg} ${style.text}`}>
      {s.replace('_', ' ')}
    </span>
  );
}

function formatHours(h: number | null): string {
  if (h == null) return '--';
  if (h < 1) return `${Math.round(h * 60)}m`;
  if (h < 24) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Card({ className = '', children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={`bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-5 ${className}`}>
      {children}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-semibold text-gray-900 dark:text-white mb-3">{children}</h3>;
}

function KpiMini({ label, value, icon }: { label: string; value: string | number; icon: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 bg-gray-50 dark:bg-gray-700/50 rounded-lg px-3 py-2">
      <div className="text-gray-400">{icon}</div>
      <div>
        <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
        <div className="text-sm font-semibold text-gray-900 dark:text-white">{value}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Spec Detail View
// ---------------------------------------------------------------------------

function SpecDetailView({ data }: { data: SpecAnalytics }) {
  const implCards = data.cards.filter((c) => !c.is_test);
  const testCards = data.cards.filter((c) => c.is_test);
  const avgCompleteness =
    implCards.length > 0
      ? Math.round(implCards.reduce((s, c) => s + (c.completeness ?? 0), 0) / implCards.length)
      : null;
  const avgDrift =
    implCards.length > 0
      ? Math.round(implCards.reduce((s, c) => s + (c.drift ?? 0), 0) / implCards.length)
      : null;

  // Scenario status counts
  const scenarioCounts: Record<string, number> = {};
  for (const sc of data.scenario_statuses) {
    scenarioCounts[sc.status] = (scenarioCounts[sc.status] || 0) + 1;
  }
  const totalScenarios = data.scenario_statuses.length;
  const scenarioColors: Record<string, string> = {
    draft: 'bg-gray-400',
    ready: 'bg-blue-500',
    automated: 'bg-cyan-500',
    passed: 'bg-emerald-500',
    failed: 'bg-red-500',
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <Card>
        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {statusBadge(data.status)}
              <h2 className="text-lg font-bold text-gray-900 dark:text-white truncate">{data.title}</h2>
            </div>
            {(data.derivation.ideation_id || data.derivation.refinement_id) && (
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Provenance:{' '}
                {data.derivation.ideation_id && (
                  <span className="font-mono text-xs">Ideation {data.derivation.ideation_id.slice(0, 8)}</span>
                )}
                {data.derivation.ideation_id && data.derivation.refinement_id && ' / '}
                {data.derivation.refinement_id && (
                  <span className="font-mono text-xs">Refinement {data.derivation.refinement_id.slice(0, 8)}</span>
                )}
              </p>
            )}
          </div>
          {data.avg_cycle_hours != null && (
            <div className="flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-300 shrink-0">
              <Clock className="w-4 h-4" />
              <span>Cycle: {formatHours(data.avg_cycle_hours)}</span>
            </div>
          )}
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <KpiMini label="Tasks" value={data.cards.length} icon={<FileText className="w-4 h-4" />} />
          <KpiMini
            label="Completeness"
            value={avgCompleteness != null ? `${avgCompleteness}%` : '--'}
            icon={<Target className="w-4 h-4" />}
          />
          <KpiMini
            label="Drift"
            value={avgDrift != null ? `${avgDrift}%` : '--'}
            icon={<AlertTriangle className="w-4 h-4" />}
          />
          <KpiMini
            label="Test coverage"
            value={testCards.length > 0 ? `${testCards.length} tests` : '--'}
            icon={<CheckCircle className="w-4 h-4" />}
          />
          <KpiMini
            label="Business Rules"
            value={`${(data.business_rules || []).length} (${data.rules_coverage ?? 0}%)`}
            icon={<Scale className="w-4 h-4" />}
          />
          <KpiMini
            label="API Contracts"
            value={`${(data.api_contracts || []).length} (${data.contracts_coverage ?? 0}%)`}
            icon={<Globe className="w-4 h-4" />}
          />
          {(data.bugs_count ?? 0) > 0 && (
            <KpiMini
              label="Bugs"
              value={`${data.bugs_count} (${data.cards.length > 0 ? Math.round(((data.bugs_count ?? 0) / data.cards.length) * 100) : 0}%)`}
              icon={<Bug className="w-4 h-4 text-red-500" />}
            />
          )}
        </div>
      </Card>

      {/* AC Coverage + Scenario Status */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* AC Coverage */}
        <Card>
          <SectionTitle>AC Coverage ({data.covered_ac}/{data.total_ac})</SectionTitle>
          {data.total_ac === 0 ? (
            <p className="text-xs text-gray-400">No acceptance criteria defined.</p>
          ) : (
            <div className="space-y-1.5 max-h-64 overflow-y-auto">
              {(data.ac_details || Array.from({ length: data.total_ac }, (_, i) => ({
                index: i, text: `AC #${i}`, covered: i < data.covered_ac,
              }))).map((ac: any) => (
                <div key={ac.index} className="flex items-start gap-2 text-xs">
                  {ac.covered ? (
                    <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-gray-300 dark:text-gray-600 shrink-0 mt-0.5" />
                  )}
                  <span className={`${ac.covered ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500'} line-clamp-2`}>
                    {ac.text || `AC #${ac.index}`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Scenario Status */}
        <Card>
          <SectionTitle>Test Scenario Status ({totalScenarios})</SectionTitle>
          {totalScenarios === 0 ? (
            <p className="text-xs text-gray-400">No test scenarios defined.</p>
          ) : (
            <div className="space-y-3">
              {/* Stacked bar */}
              <div className="flex h-5 rounded-full overflow-hidden">
                {Object.entries(scenarioCounts).map(([st, count]) => (
                  <div
                    key={st}
                    className={`${scenarioColors[st] || 'bg-gray-400'}`}
                    style={{ width: `${(count / totalScenarios) * 100}%` }}
                    title={`${st}: ${count}`}
                  />
                ))}
              </div>
              {/* Legend */}
              <div className="flex flex-wrap gap-3">
                {Object.entries(scenarioCounts).map(([st, count]) => (
                  <div key={st} className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
                    <div className={`w-2.5 h-2.5 rounded-full ${scenarioColors[st] || 'bg-gray-400'}`} />
                    <span className="capitalize">{st}</span>
                    <span className="font-semibold">{count}</span>
                  </div>
                ))}
              </div>
              {/* Scenario list */}
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {data.scenario_statuses.map((sc) => (
                  <div key={sc.id} className="flex items-center justify-between text-xs py-1 border-b border-gray-100 dark:border-gray-700 last:border-0">
                    <span className="text-gray-700 dark:text-gray-300 truncate mr-2">{sc.title}</span>
                    {statusBadge(sc.status)}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* FR Coverage */}
      {data.fr_details && data.fr_details.length > 0 && (
        <Card>
          <SectionTitle>FR Coverage ({data.fr_details.filter((f: any) => f.has_rule || f.has_contract).length}/{data.total_fr ?? data.fr_details.length})</SectionTitle>
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {data.fr_details.map((fr: any) => (
              <div key={fr.index} className="flex items-start gap-2 text-xs">
                {fr.has_rule && fr.has_contract ? (
                  <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                ) : fr.has_rule || fr.has_contract ? (
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle className="w-3.5 h-3.5 text-gray-300 dark:text-gray-600 shrink-0 mt-0.5" />
                )}
                <span className={`${fr.has_rule || fr.has_contract ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500'} line-clamp-2 flex-1`}>
                  {fr.text || `FR #${fr.index}`}
                </span>
                <div className="flex gap-1 shrink-0">
                  {fr.has_rule && <span className="px-1 py-0.5 rounded text-[9px] bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300">Rule</span>}
                  {fr.has_contract && <span className="px-1 py-0.5 rounded text-[9px] bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">Contract</span>}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Tasks Table */}
      <Card>
        <SectionTitle>Tasks ({data.cards.length})</SectionTitle>
        {data.cards.length === 0 ? (
          <p className="text-xs text-gray-400">No tasks linked to this spec.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-2 pr-3 font-medium">Title</th>
                  <th className="pb-2 pr-3 font-medium">Status</th>
                  <th className="pb-2 pr-3 font-medium">Type</th>
                  <th className="pb-2 pr-3 font-medium text-right">Completeness</th>
                  <th className="pb-2 pr-3 font-medium text-right">Drift</th>
                  <th className="pb-2 font-medium text-right">Cycle</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {data.cards.map((c) => {
                  const cycle =
                    c.created_at && c.updated_at
                      ? ((new Date(c.updated_at).getTime() - new Date(c.created_at).getTime()) / 3600000)
                      : null;
                  return (
                    <tr key={c.id} className="text-gray-700 dark:text-gray-300">
                      <td className="py-2 pr-3 max-w-[200px] truncate">{c.title}</td>
                      <td className="py-2 pr-3">{statusBadge(c.status)}</td>
                      <td className="py-2 pr-3">
                        <span className={`text-xs font-medium ${
                          c.card_type === 'bug' ? 'text-red-500' :
                          c.is_test ? 'text-cyan-600 dark:text-cyan-400' :
                          'text-gray-500'
                        }`}>
                          {c.card_type === 'bug' ? 'Bug' : c.is_test ? 'Test' : 'Impl'}
                        </span>
                      </td>
                      <td className="py-2 pr-3 text-right">
                        <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${completenessBg(c.completeness)} ${completenessColor(c.completeness)}`}>
                          {c.completeness != null ? `${c.completeness}%` : '--'}
                        </span>
                      </td>
                      <td className="py-2 pr-3 text-right">
                        <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${driftBg(c.drift)} ${driftColor(c.drift)}`}>
                          {c.drift != null ? `${c.drift}%` : '--'}
                        </span>
                      </td>
                      <td className="py-2 text-right font-mono">{formatHours(cycle != null ? Math.round(cycle * 10) / 10 : null)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Derivation Tree */}
      <Card>
        <SectionTitle>Derivation Tree</SectionTitle>
        <div className="flex items-center gap-2 flex-wrap">
          {data.derivation.ideation_id && (
            <>
              <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 text-xs font-medium text-purple-700 dark:text-purple-300">
                <Layers className="w-3.5 h-3.5" />
                Ideation
              </div>
              <ArrowRight className="w-4 h-4 text-gray-400" />
            </>
          )}
          {data.derivation.refinement_id && (
            <>
              <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-teal-50 dark:bg-teal-900/20 border border-teal-200 dark:border-teal-800 text-xs font-medium text-teal-700 dark:text-teal-300">
                <GitBranch className="w-3.5 h-3.5" />
                Refinement
              </div>
              <ArrowRight className="w-4 h-4 text-gray-400" />
            </>
          )}
          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 text-xs font-medium text-blue-700 dark:text-blue-300">
            <FileText className="w-3.5 h-3.5" />
            Spec
          </div>
          <ArrowRight className="w-4 h-4 text-gray-400" />
          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 text-xs font-medium text-amber-700 dark:text-amber-300">
            <FileText className="w-3.5 h-3.5" />
            {data.cards.length} Tasks
          </div>
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ideation Detail View
// ---------------------------------------------------------------------------

function IdeationDetailView({ data }: { data: IdeationAnalytics }) {
  const scope = data.scope_assessment;

  function scoreBar(label: string, value: number | undefined) {
    const v = value ?? 0;
    const pct = Math.min(v * 10, 100);
    let barColor = 'bg-emerald-500';
    if (v > 6) barColor = 'bg-red-500';
    else if (v > 3) barColor = 'bg-amber-500';
    return (
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-600 dark:text-gray-400">{label}</span>
          <span className="font-semibold text-gray-900 dark:text-white">{v}/10</span>
        </div>
        <div className="h-2 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
          <div className={`h-full rounded-full ${barColor} transition-all`} style={{ width: `${pct}%` }} />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <Card>
        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {statusBadge(data.status)}
              <h2 className="text-lg font-bold text-gray-900 dark:text-white truncate">{data.title}</h2>
            </div>
            <div className="flex items-center gap-3 mt-1">
              {data.complexity && (
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  Complexity: <span className="font-medium capitalize">{data.complexity}</span>
                </span>
              )}
              {data.created_at && (
                <span className="text-xs text-gray-400">
                  Created: {new Date(data.created_at).toLocaleDateString()}
                </span>
              )}
            </div>
          </div>
        </div>
      </Card>

      {/* Scope Assessment */}
      <Card>
        <SectionTitle>Scope Assessment</SectionTitle>
        {scope ? (
          <div className="space-y-3 max-w-md">
            {scoreBar('Domains', scope.domains)}
            {scoreBar('Ambiguity', scope.ambiguity)}
            {scoreBar('Dependencies', scope.dependencies)}
          </div>
        ) : (
          <p className="text-xs text-gray-400">No scope assessment available.</p>
        )}
      </Card>

      {/* Derivation Info */}
      <Card>
        <SectionTitle>Derivation Summary</SectionTitle>
        <div className="grid grid-cols-3 gap-4">
          <KpiMini label="Refinements" value={data.refinement_count} icon={<GitBranch className="w-4 h-4" />} />
          <KpiMini label="Specs" value={data.spec_count} icon={<FileText className="w-4 h-4" />} />
          <KpiMini label="Q&A items" value={data.qa_count} icon={<MessageSquare className="w-4 h-4" />} />
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Refinement Detail View
// ---------------------------------------------------------------------------

function RefinementDetailView({ data }: { data: RefinementData }) {
  return (
    <div className="space-y-4">
      {/* Header */}
      <Card>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {statusBadge(data.status)}
              <h2 className="text-lg font-bold text-gray-900 dark:text-white truncate">{data.title}</h2>
            </div>
            <div className="flex items-center gap-3 mt-1 text-xs text-gray-500 dark:text-gray-400">
              <span>Version: {data.version}</span>
              <span>Linked ideation: <span className="font-mono">{data.ideation_id.slice(0, 8)}</span></span>
            </div>
            {data.description && (
              <p className="text-xs text-gray-600 dark:text-gray-300 mt-2 line-clamp-3">{data.description}</p>
            )}
          </div>
        </div>
      </Card>

      {/* Scope */}
      <Card>
        <SectionTitle>Scope</SectionTitle>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">In Scope</p>
            <div className="flex flex-wrap gap-1.5">
              {(data.in_scope && data.in_scope.length > 0) ? (
                data.in_scope.map((item, i) => (
                  <span key={i} className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300">
                    {item}
                  </span>
                ))
              ) : (
                <span className="text-xs text-gray-400">None defined</span>
              )}
            </div>
          </div>
          <div>
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">Out of Scope</p>
            <div className="flex flex-wrap gap-1.5">
              {(data.out_of_scope && data.out_of_scope.length > 0) ? (
                data.out_of_scope.map((item, i) => (
                  <span key={i} className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300">
                    {item}
                  </span>
                ))
              ) : (
                <span className="text-xs text-gray-400">None defined</span>
              )}
            </div>
          </div>
        </div>
      </Card>

      {/* Knowledge Bases */}
      <Card>
        <SectionTitle>Knowledge Bases ({data.knowledge_bases.length})</SectionTitle>
        {data.knowledge_bases.length === 0 ? (
          <p className="text-xs text-gray-400">No knowledge bases attached.</p>
        ) : (
          <div className="space-y-1">
            {data.knowledge_bases.map((kb) => (
              <div key={kb.id} className="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 py-1">
                <FileText className="w-3.5 h-3.5 text-gray-400" />
                {kb.title}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Derived Specs */}
      <Card>
        <SectionTitle>Derived Specs ({data.specs.length})</SectionTitle>
        {data.specs.length === 0 ? (
          <p className="text-xs text-gray-400">No specs derived yet.</p>
        ) : (
          <div className="space-y-1.5">
            {data.specs.map((sp) => (
              <div key={sp.id} className="flex items-center justify-between text-xs py-1 border-b border-gray-100 dark:border-gray-700 last:border-0">
                <span className="text-gray-700 dark:text-gray-300 truncate mr-2">{sp.title}</span>
                {statusBadge(sp.status)}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function SprintDetailView({ data }: { data: SprintAnalytics }) {
  return (
    <div className="space-y-4">
      {/* Header */}
      <Card>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {statusBadge(data.status)}
              <h2 className="text-lg font-bold text-gray-900 dark:text-white truncate">{data.title}</h2>
            </div>
            <div className="flex items-center gap-3 mt-1 text-xs text-gray-500 dark:text-gray-400">
              <span>Spec v{data.spec_version}</span>
              <span>Tasks: {data.tasks_done}/{data.tasks_total}</span>
            </div>
          </div>
          <div className="text-right">
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{data.progress}%</div>
            <div className="text-xs text-gray-500">Progress</div>
          </div>
        </div>
      </Card>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card>
          <p className="text-xs text-gray-500 dark:text-gray-400">Avg Completeness</p>
          <p className={`text-lg font-bold ${completenessColor(data.avg_completeness)}`}>
            {data.avg_completeness != null ? `${data.avg_completeness}%` : '—'}
          </p>
        </Card>
        <Card>
          <p className="text-xs text-gray-500 dark:text-gray-400">Avg Drift</p>
          <p className={`text-lg font-bold ${driftColor(data.avg_drift)}`}>
            {data.avg_drift != null ? `${data.avg_drift}%` : '—'}
          </p>
        </Card>
        <Card>
          <p className="text-xs text-gray-500 dark:text-gray-400">Avg Cycle Time</p>
          <p className="text-lg font-bold text-gray-900 dark:text-white">
            {data.avg_cycle_hours != null ? `${data.avg_cycle_hours}h` : '—'}
          </p>
        </Card>
        <Card>
          <p className="text-xs text-gray-500 dark:text-gray-400">Eval Score</p>
          <p className={`text-lg font-bold ${completenessColor(data.avg_eval_score)}`}>
            {data.avg_eval_score != null ? `${data.avg_eval_score}/100` : '—'}
          </p>
          <p className="text-[10px] text-gray-400">{data.approvals} approval(s)</p>
        </Card>
      </div>

      {/* Test Scenario Coverage */}
      {data.scoped_scenarios.length > 0 && (
        <Card>
          <SectionTitle>Test Coverage ({data.scenario_coverage}%)</SectionTitle>
          <div className="space-y-1">
            {data.scoped_scenarios.map(sc => (
              <div key={sc.id} className="flex items-center gap-2 text-xs py-0.5">
                {sc.status === 'passed' ? (
                  <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
                ) : (
                  <Clock className="w-3.5 h-3.5 text-amber-500" />
                )}
                <span className="text-gray-700 dark:text-gray-300 truncate">{sc.title}</span>
                <span className="ml-auto text-gray-400">{sc.status}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Cards */}
      <Card>
        <SectionTitle>Tasks ({data.tasks_total})</SectionTitle>
        <div className="space-y-1">
          {data.cards.map(c => (
            <div key={c.id} className="flex items-center gap-2 text-xs py-1 border-b border-gray-100 dark:border-gray-700 last:border-0">
              <span className={`w-2 h-2 rounded-full ${
                c.status === 'done' ? 'bg-emerald-500' :
                c.status === 'in_progress' ? 'bg-blue-500' :
                c.status === 'cancelled' ? 'bg-red-500' : 'bg-gray-400'
              }`} />
              <span className="text-gray-700 dark:text-gray-300 truncate flex-1">{c.title}</span>
              {c.completeness != null && (
                <span className={`${completenessColor(c.completeness)}`}>{c.completeness}%</span>
              )}
              {c.drift != null && (
                <span className={`${driftColor(c.drift)}`}>d{c.drift}%</span>
              )}
              {c.cycle_hours != null && (
                <span className="text-gray-400">{c.cycle_hours}h</span>
              )}
            </div>
          ))}
        </div>
      </Card>

      {/* Sprint Comparison */}
      {data.comparison.length > 1 && (
        <Card>
          <SectionTitle>Sprint Comparison</SectionTitle>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-1.5 pr-3">Sprint</th>
                  <th className="text-right py-1.5 px-2">Status</th>
                  <th className="text-right py-1.5 px-2">Done/Total</th>
                  <th className="text-right py-1.5 px-2">Completeness</th>
                  <th className="text-right py-1.5 pl-2">Drift</th>
                </tr>
              </thead>
              <tbody>
                {data.comparison.map(row => (
                  <tr key={row.sprint_id} className={`border-b border-gray-100 dark:border-gray-700/50 last:border-0 ${row.is_current ? 'bg-blue-50 dark:bg-blue-900/10' : ''}`}>
                    <td className="py-1.5 pr-3 text-gray-900 dark:text-white font-medium truncate max-w-[150px]">
                      {row.title} {row.is_current && <span className="text-blue-500 text-[10px]">(current)</span>}
                    </td>
                    <td className="text-right py-1.5 px-2">{statusBadge(row.status)}</td>
                    <td className="text-right py-1.5 px-2 text-gray-600 dark:text-gray-400">{row.tasks_done}/{row.tasks_total}</td>
                    <td className={`text-right py-1.5 px-2 ${completenessColor(row.avg_completeness)}`}>
                      {row.avg_completeness != null ? `${row.avg_completeness}%` : '—'}
                    </td>
                    <td className={`text-right py-1.5 pl-2 ${driftColor(row.avg_drift)}`}>
                      {row.avg_drift != null ? `${row.avg_drift}%` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export function EntityDetail({ boardId, entityType, entityId, from, to }: EntityDetailProps) {
  const api = useDashboardApi();
  const [data, setData] = useState<SpecAnalytics | IdeationAnalytics | RefinementData | SprintAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (entityType === 'refinement') {
        // Backend doesn't have a refinement analytics endpoint — fetch via regular API
        const refinement = await api.getRefinement(entityId);
        setData({
          id: refinement.id,
          title: refinement.title,
          description: refinement.description,
          status: typeof refinement.status === 'string' ? refinement.status : String(refinement.status),
          version: refinement.version,
          ideation_id: refinement.ideation_id,
          in_scope: refinement.in_scope,
          out_of_scope: refinement.out_of_scope,
          specs: (refinement.specs || []).map((s: any) => ({
            id: s.id,
            title: s.title,
            status: typeof s.status === 'string' ? s.status : String(s.status),
          })),
          knowledge_bases: (refinement.knowledge_bases || []).map((kb: any) => ({
            id: kb.id,
            title: kb.title,
          })),
        } as RefinementData);
      } else {
        const result = await api.getEntityAnalytics(boardId, entityType, entityId, from, to);
        setData(result);
      }
    } catch (err: any) {
      setError(err?.message || 'Failed to load entity analytics');
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, entityType, entityId, from, to]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        <span className="ml-2 text-sm text-gray-500 dark:text-gray-400">Loading entity analytics...</span>
      </div>
    );
  }

  if (error) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
          <AlertTriangle className="w-5 h-5" />
          <span className="text-sm">{error}</span>
        </div>
        <button
          onClick={fetchData}
          className="mt-3 text-xs text-blue-600 dark:text-blue-400 hover:underline"
        >
          Retry
        </button>
      </Card>
    );
  }

  if (!data) return null;

  if (entityType === 'spec') return <SpecDetailView data={data as SpecAnalytics} />;
  if (entityType === 'ideation') return <IdeationDetailView data={data as IdeationAnalytics} />;
  if (entityType === 'refinement') return <RefinementDetailView data={data as RefinementData} />;
  if (entityType === 'sprint') return <SprintDetailView data={data as SprintAnalytics} />;

  return null;
}
