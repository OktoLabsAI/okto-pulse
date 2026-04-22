/**
 * SprintModal — Detailed sprint view with tabs
 */

import { useEffect, useState } from 'react';
import {
  X, ChevronRight, ChevronUp, ChevronDown, ArrowRight, FileText, Link2, History, MessageCircleQuestion,
  FlaskConical, Scale, RefreshCw, Maximize2, Minimize2, Download,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { exportSprint, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import type { Sprint, SprintStatus } from '@/types';
import { SPRINT_STATUS_LABELS, SPRINT_STATUS_COLORS } from '@/types';
import { ValidationGateOverride } from '@/components/shared/ValidationGateOverride';

type SprintTab = 'details' | 'scope' | 'cards' | 'evaluations' | 'qa' | 'history';

const SPRINT_ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  updated: 'Updated',
  status_changed: 'Status changed',
  tasks_assigned: 'Cards assigned',
  tasks_unassigned: 'Cards removed',
  evaluation_submitted: 'Evaluation submitted',
  qa_added: 'Question added',
  qa_answered: 'Question answered',
};

const SPRINT_ACTION_COLORS: Record<string, string> = {
  created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  status_changed: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  tasks_assigned: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  tasks_unassigned: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  evaluation_submitted: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300',
};

function formatChangeValue(val: unknown): string {
  if (val === null || val === undefined) return '(empty)';
  if (Array.isArray(val)) {
    if (val.length === 0) return '(empty list)';
    return val.map((v, i) => `${i + 1}. ${v}`).join('\n');
  }
  return String(val);
}

function SprintHistoryTab({ sprintId, api }: { sprintId: string; api: ReturnType<typeof useDashboardApi> }) {
  const [entries, setEntries] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api.listSprintHistory(sprintId).then(setEntries).catch(() => setEntries([])).finally(() => setLoading(false));
  }, [sprintId]);

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading history...</div>;

  if (entries.length === 0) {
    return (
      <div className="text-center py-6">
        <History size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No history yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {entries.map((entry: any) => {
        const isExpanded = expandedId === entry.id;
        const actionColor = SPRINT_ACTION_COLORS[entry.action] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
        const hasChanges = entry.changes && entry.changes.length > 0;

        return (
          <div key={entry.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div
              className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/30"
              onClick={() => hasChanges && setExpandedId(isExpanded ? null : entry.id)}
            >
              <div className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 shrink-0" />
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${actionColor}`}>
                {SPRINT_ACTION_LABELS[entry.action] || entry.action}
              </span>
              <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">
                {entry.summary || entry.action}
              </span>
              <div className="flex items-center gap-2 shrink-0 text-[10px] text-gray-400">
                <span className={`px-1 py-0.5 rounded ${
                  entry.actor_type === 'agent'
                    ? 'bg-violet-100 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300'
                    : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                }`}>
                  {entry.actor_name}
                </span>
                {entry.version && <span>v{entry.version}</span>}
                <span>{entry.created_at ? new Date(entry.created_at).toLocaleString() : ''}</span>
              </div>
              {hasChanges && (
                <span className="text-gray-400 shrink-0">
                  {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
              )}
            </div>

            {isExpanded && hasChanges && (
              <div className="px-3 py-2 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 space-y-2">
                {entry.changes.map((change: any, idx: number) => (
                  <div key={idx} className="text-sm">
                    <div className="font-medium text-gray-700 dark:text-gray-300 text-xs uppercase tracking-wide mb-1">
                      {change.field}
                    </div>
                    {change.old !== undefined && change.new !== undefined ? (
                      <div className="flex items-start gap-2">
                        <div className="flex-1 min-w-0">
                          <div className="text-[10px] text-red-500 font-medium mb-0.5">Before</div>
                          <pre className="text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                            {formatChangeValue(change.old)}
                          </pre>
                        </div>
                        <ArrowRight size={14} className="text-gray-400 mt-4 shrink-0" />
                        <div className="flex-1 min-w-0">
                          <div className="text-[10px] text-green-500 font-medium mb-0.5">After</div>
                          <pre className="text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                            {formatChangeValue(change.new)}
                          </pre>
                        </div>
                      </div>
                    ) : (
                      <pre className="text-xs text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 rounded px-2 py-1 whitespace-pre-wrap">
                        {formatChangeValue(change)}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

interface SprintModalProps {
  sprintId: string;
  onClose: () => void;
}

const FLOW_STATUSES: SprintStatus[] = ['draft', 'active', 'review', 'closed'];

export function SprintModal({ sprintId, onClose }: SprintModalProps) {
  const api = useDashboardApi();
  const [sprint, setSprint] = useState<Sprint | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<SprintTab>('details');
  const [expanded, setExpanded] = useState(false);
  const [movingTo, setMovingTo] = useState<SprintStatus | null>(null);
  const [showAssign, setShowAssign] = useState(false);
  const [specCards, setSpecCards] = useState<any[]>([]);
  const [parentSpec, setParentSpec] = useState<any>(null);

  const loadSprint = async () => {
    try {
      setLoading(true);
      const data = await api.getSprint(sprintId);
      setSprint(data);
      // Load parent spec for scope resolution
      if (data.spec_id) {
        api.getSpec(data.spec_id).then(setParentSpec).catch(() => setParentSpec(null));
      }
    } catch {
      toast.error('Failed to load sprint');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadSprint(); }, [sprintId]);

  const handleMove = async (status: SprintStatus) => {
    if (!sprint) return;
    setMovingTo(status);
    try {
      await api.moveSprint(sprintId, { status });
      toast.success(`Sprint moved to ${SPRINT_STATUS_LABELS[status]}`);
      loadSprint();
    } catch (e: any) {
      toast.error(e.message || `Failed to move sprint`);
    } finally {
      setMovingTo(null);
    }
  };

  if (loading || !sprint) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-xl p-8">
          <RefreshCw className="animate-spin text-gray-400" size={24} />
        </div>
      </div>
    );
  }

  // Next status for contextual action
  const nextAction: Record<SprintStatus, { label: string; status: SprintStatus } | null> = {
    draft: { label: 'Activate', status: 'active' },
    active: { label: 'Submit for Review', status: 'review' },
    review: { label: 'Close Sprint', status: 'closed' },
    closed: null,
    cancelled: null,
  };

  const action = nextAction[sprint.status];
  const currentIdx = FLOW_STATUSES.indexOf(sprint.status as any);

  const tabs: { id: SprintTab; label: string; icon: React.ReactNode; count?: number }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'scope', label: 'Scope', icon: <FlaskConical size={14} />, count: (sprint.test_scenario_ids?.length || 0) + (sprint.business_rule_ids?.length || 0) },
    { id: 'cards', label: 'Cards', icon: <Link2 size={14} />, count: sprint.cards?.length || 0 },
    { id: 'evaluations', label: 'Evaluations', icon: <Scale size={14} />, count: sprint.evaluations?.length || 0 },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: sprint.qa_items?.length || 0 },
    { id: 'history', label: 'History', icon: <History size={14} /> },
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60] p-4">
      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full ${expanded ? 'max-w-[95vw] h-[95vh]' : 'max-w-2xl h-[85vh]'} flex flex-col`}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium text-white ${SPRINT_STATUS_COLORS[sprint.status]}`}>
              {SPRINT_STATUS_LABELS[sprint.status]}
            </span>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{sprint.title}</h2>
            <span className="text-xs text-gray-400 shrink-0">v{sprint.version}</span>
          </div>
          {parentSpec && (
            <button
              onClick={() => {/* could open spec modal */}}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 shrink-0"
              title={`Linked spec: ${parentSpec.title}`}
            >
              <FileText size={11} />
              {parentSpec.title.length > 35 ? parentSpec.title.slice(0, 32) + '...' : parentSpec.title}
            </button>
          )}
          <div className="flex items-center gap-1">
            <button
              onClick={() => { if (!sprint || !parentSpec) return; const md = exportSprint(sprint, parentSpec); downloadMarkdown(md, `sprint_${slugify(sprint.title)}.md`); }}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
              title="Download Markdown"
            >
              <Download size={16} />
            </button>
            <button onClick={loadSprint} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg" title="Refresh">
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <button onClick={() => setExpanded(!expanded)} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Status Flow Bar */}
        <div className="px-6 py-2 flex items-center gap-1 border-b border-gray-100 dark:border-gray-700/50 overflow-x-auto">
          {FLOW_STATUSES.map((s, i) => {
            const isActive = sprint.status === s;
            const isPast = currentIdx >= 0 && i < currentIdx;
            return (
              <div key={s} className="flex items-center">
                {i > 0 && <ChevronRight size={12} className="text-gray-300 mx-0.5" />}
                <span className={`text-xs px-2 py-0.5 rounded-full ${
                  isActive ? `text-white ${SPRINT_STATUS_COLORS[s]}` :
                  isPast ? 'text-green-600 bg-green-100 dark:bg-green-900/30 dark:text-green-400' :
                  'text-gray-400 bg-gray-100 dark:bg-gray-700'
                }`}>
                  {SPRINT_STATUS_LABELS[s]}
                </span>
              </div>
            );
          })}
          {sprint.status === 'cancelled' && (
            <span className="text-xs px-2 py-0.5 rounded-full text-white bg-red-500 ml-2">Cancelled</span>
          )}
        </div>

        {/* Tabs */}
        <div className="px-6 pt-3 flex gap-1 border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
          {tabs.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium rounded-t-lg border-b-2 transition-colors whitespace-nowrap ${
                activeTab === tab.id
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
              }`}
            >
              {tab.icon}
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span className="ml-1 px-1.5 py-0.5 text-[10px] bg-gray-100 dark:bg-gray-600 rounded-full">{tab.count}</span>
              )}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {activeTab === 'details' && (
            <div className="space-y-4">
              {/* Objective */}
              <div>
                <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1">Objective</h4>
                <textarea
                  defaultValue={sprint.objective || ''}
                  onBlur={async (e) => {
                    const val = e.target.value.trim();
                    if (val !== (sprint.objective || '')) {
                      await api.updateSprint(sprintId, { objective: val || null });
                      loadSprint();
                    }
                  }}
                  placeholder="What is this sprint trying to achieve?"
                  rows={2}
                  className="w-full px-3 py-2 text-sm border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-300 resize-y"
                />
              </div>

              {/* Expected Outcome */}
              <div>
                <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1">Expected Outcome</h4>
                <textarea
                  defaultValue={sprint.expected_outcome || ''}
                  onBlur={async (e) => {
                    const val = e.target.value.trim();
                    if (val !== (sprint.expected_outcome || '')) {
                      await api.updateSprint(sprintId, { expected_outcome: val || null });
                      loadSprint();
                    }
                  }}
                  placeholder="What should be deliverable at the end of this sprint?"
                  rows={2}
                  className="w-full px-3 py-2 text-sm border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-300 resize-y"
                />
              </div>

              {sprint.description && (
                <div>
                  <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1">Description</h4>
                  <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{sprint.description}</p>
                </div>
              )}
              <div className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <span className="text-xs text-gray-500">Spec Version</span>
                  <p className="font-medium text-gray-900 dark:text-white">v{sprint.spec_version}</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Sprint Version</span>
                  <p className="font-medium text-gray-900 dark:text-white">v{sprint.version}</p>
                </div>
                {sprint.start_date && (
                  <div>
                    <span className="text-xs text-gray-500">Start Date</span>
                    <p className="font-medium text-gray-900 dark:text-white">{new Date(sprint.start_date).toLocaleDateString()}</p>
                  </div>
                )}
                {sprint.end_date && (
                  <div>
                    <span className="text-xs text-gray-500">End Date</span>
                    <p className="font-medium text-gray-900 dark:text-white">{new Date(sprint.end_date).toLocaleDateString()}</p>
                  </div>
                )}
              </div>
              {sprint.labels && sprint.labels.length > 0 && (
                <div>
                  <span className="text-xs text-gray-500">Labels</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {sprint.labels.map(l => (
                      <span key={l} className="px-2 py-0.5 text-xs bg-gray-100 dark:bg-gray-600 text-gray-600 dark:text-gray-300 rounded-full">{l}</span>
                    ))}
                  </div>
                </div>
              )}

              {/* Validation Gate Override */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
                <ValidationGateOverride
                  title="Validation Gate"
                  requireValue={(sprint as any).require_task_validation ?? null}
                  minConfidence={(sprint as any).validation_min_confidence ?? null}
                  minCompleteness={(sprint as any).validation_min_completeness ?? null}
                  maxDrift={(sprint as any).validation_max_drift ?? null}
                  parentLabel="Spec/Board"
                  onUpdate={async (patch) => {
                    try {
                      await api.updateSprint(sprintId, patch as any);
                      loadSprint();
                    } catch {
                      toast.error('Failed to update validation gate');
                    }
                  }}
                />
              </div>

              {/* Progress + Scope Summary */}
              {sprint.cards && sprint.cards.length > 0 && (() => {
                const total = sprint.cards.length;
                const done = sprint.cards.filter((c: any) => c.status === 'done').length;
                const pct = Math.round((done / total) * 100);
                return (
                <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
                  {/* Progress Bar */}
                  <div className="mb-3">
                    <div className="flex items-center justify-between mb-1">
                      <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Progress</h4>
                      <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{pct}%</span>
                    </div>
                    <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
                      <div
                        className={`h-2.5 rounded-full transition-all duration-500 ${pct === 100 ? 'bg-green-500' : pct >= 50 ? 'bg-blue-500' : 'bg-amber-500'}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <p className="text-[10px] text-gray-400 mt-0.5">{done} of {total} cards done</p>
                  </div>

                  <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">Scope Summary</h4>
                  <div className="grid grid-cols-3 gap-2">
                    <div className="p-2 bg-blue-50 dark:bg-blue-900/20 rounded-lg text-center">
                      <p className="text-lg font-bold text-blue-600 dark:text-blue-400">{total}</p>
                      <p className="text-[10px] text-blue-500">Cards</p>
                    </div>
                    <div className="p-2 bg-purple-50 dark:bg-purple-900/20 rounded-lg text-center">
                      <p className="text-lg font-bold text-purple-600 dark:text-purple-400">{sprint.cards.filter((c: any) => c.card_type === 'test').length}</p>
                      <p className="text-[10px] text-purple-500">Tests</p>
                    </div>
                    <div className="p-2 bg-green-50 dark:bg-green-900/20 rounded-lg text-center">
                      <p className="text-lg font-bold text-green-600 dark:text-green-400">{done}</p>
                      <p className="text-[10px] text-green-500">Done</p>
                    </div>
                  </div>
                </div>
                );
              })()}
            </div>
          )}

          {activeTab === 'scope' && (() => {
            // Compute inherited scope from parent spec
            const specTs = parentSpec?.test_scenarios || [];
            const specBrs = parentSpec?.business_rules || [];
            const specTrs = parentSpec?.technical_requirements || [];
            const specAcs: string[] = parentSpec?.acceptance_criteria || [];
            const specContracts = parentSpec?.api_contracts || [];

            const sprintCardIds = new Set((sprint.cards || []).map((c: any) => c.id));

            // Resolve scoped test scenarios: sprint-level IDs + card-linked
            const tsById = new Map<string, any>();
            if (sprint.test_scenario_ids?.length) {
              for (const ts of specTs) {
                if (sprint.test_scenario_ids.includes(ts.id)) tsById.set(ts.id, ts);
              }
            }
            for (const ts of specTs) {
              if (!tsById.has(ts.id) && ts.linked_task_ids?.some((id: string) => sprintCardIds.has(id))) {
                tsById.set(ts.id, ts);
              }
            }
            const scopedTs = Array.from(tsById.values());

            // Resolve scoped business rules: sprint-level IDs + card-linked
            const brById = new Map<string, any>();
            if (sprint.business_rule_ids?.length) {
              for (const br of specBrs) {
                if (sprint.business_rule_ids.includes(br.id)) brById.set(br.id, br);
              }
            }
            for (const br of specBrs) {
              if (!brById.has(br.id) && br.linked_task_ids?.some((id: string) => sprintCardIds.has(id))) {
                brById.set(br.id, br);
              }
            }
            const scopedBrs = Array.from(brById.values());

            // Compute TRs linked to sprint cards
            const scopedTrs = specTrs.filter((tr: any) =>
              typeof tr === 'object' && tr.linked_task_ids?.some((id: string) => sprintCardIds.has(id))
            );

            // Compute contracts linked to sprint cards
            const scopedContracts = specContracts.filter((c: any) =>
              c.linked_task_ids?.some((id: string) => sprintCardIds.has(id))
            );

            // Compute ACs covered via test scenarios' linked_criteria
            const coveredAcTexts = new Set<string>();
            for (const ts of scopedTs) {
              const linked: string[] = ts.linked_criteria || [];
              linked.forEach((c: string) => coveredAcTexts.add(c));
            }
            // Match linked_criteria strings against spec acceptance_criteria
            const coveredAcs = specAcs.filter((ac: string) =>
              coveredAcTexts.has(ac) || Array.from(coveredAcTexts).some(lc => ac.includes(lc) || lc.includes(ac))
            );

            const ScopeSection = ({ title, count, children }: { title: string; count: number; children: React.ReactNode }) => (
              <div>
                <h4 className="text-xs font-medium text-gray-500 uppercase mb-2 flex items-center gap-2">
                  {title} <span className="px-1.5 py-0.5 text-[10px] bg-gray-100 dark:bg-gray-600 rounded-full">{count}</span>
                </h4>
                {children}
              </div>
            );

            const EmptyScope = ({ text }: { text: string }) => (
              <p className="text-xs text-gray-400 italic">{text}</p>
            );

            return (
              <div className="space-y-5">
                {!parentSpec ? (
                  <p className="text-sm text-gray-400">Loading spec context...</p>
                ) : (
                  <>
                    <ScopeSection title="Test Scenarios" count={scopedTs.length}>
                      {scopedTs.length > 0 ? (
                        <div className="space-y-1.5">
                          {scopedTs.map((ts: any) => (
                            <div key={ts.id} className="p-2 bg-purple-50 dark:bg-purple-900/10 border border-purple-200 dark:border-purple-800 rounded-lg">
                              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">{ts.title}</p>
                              <p className="text-xs text-gray-500 mt-0.5">Given: {ts.given} | When: {ts.when} | Then: {ts.then}</p>
                            </div>
                          ))}
                        </div>
                      ) : <EmptyScope text="No test scenarios scoped to this sprint" />}
                    </ScopeSection>

                    <ScopeSection title="Business Rules" count={scopedBrs.length}>
                      {scopedBrs.length > 0 ? (
                        <div className="space-y-1.5">
                          {scopedBrs.map((br: any) => (
                            <div key={br.id} className="p-2 bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-800 rounded-lg">
                              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">{br.title}</p>
                              <p className="text-xs text-gray-500 mt-0.5">When: {br.when} → Then: {br.then}</p>
                            </div>
                          ))}
                        </div>
                      ) : <EmptyScope text="No business rules scoped to this sprint" />}
                    </ScopeSection>

                    <ScopeSection title="Technical Requirements" count={scopedTrs.length}>
                      {scopedTrs.length > 0 ? (
                        <div className="space-y-1">
                          {scopedTrs.map((tr: any) => (
                            <div key={tr.id} className="text-sm text-gray-700 dark:text-gray-300 px-2 py-1.5 bg-gray-50 dark:bg-gray-700 rounded">
                              {typeof tr === 'string' ? tr : tr.text}
                            </div>
                          ))}
                        </div>
                      ) : <EmptyScope text="No TRs linked to sprint cards" />}
                    </ScopeSection>

                    <ScopeSection title="Acceptance Criteria" count={coveredAcs.length}>
                      {coveredAcs.length > 0 ? (
                        <div className="space-y-1">
                          {coveredAcs.map((ac: string, i: number) => (
                            <div key={i} className="text-sm text-gray-700 dark:text-gray-300 px-2 py-1.5 bg-green-50 dark:bg-green-900/10 border border-green-200 dark:border-green-800 rounded">
                              {ac}
                            </div>
                          ))}
                        </div>
                      ) : <EmptyScope text="No ACs covered by scoped test scenarios" />}
                    </ScopeSection>

                    <ScopeSection title="API Contracts" count={scopedContracts.length}>
                      {scopedContracts.length > 0 ? (
                        <div className="space-y-1.5">
                          {scopedContracts.map((c: any) => (
                            <div key={c.id} className="p-2 bg-blue-50 dark:bg-blue-900/10 border border-blue-200 dark:border-blue-800 rounded-lg">
                              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                                <span className="font-mono text-xs text-blue-600 dark:text-blue-400 mr-1">{c.method}</span>
                                {c.path}
                              </p>
                              {c.description && <p className="text-xs text-gray-500 mt-0.5">{c.description}</p>}
                            </div>
                          ))}
                        </div>
                      ) : <EmptyScope text="No API contracts linked to sprint cards" />}
                    </ScopeSection>
                  </>
                )}
              </div>
            );
          })()}

          {activeTab === 'cards' && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">{sprint.cards?.length || 0} cards assigned</span>
                <button
                  onClick={async () => {
                    if (!showAssign && sprint.spec_id) {
                      try {
                        const spec = await api.getSpec(sprint.spec_id);
                        setSpecCards((spec.cards || []).filter((c: any) => !c.sprint_id || c.sprint_id === sprintId));
                      } catch { setSpecCards([]); }
                    }
                    setShowAssign(!showAssign);
                  }}
                  className="text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-800"
                >
                  {showAssign ? 'Done' : '+ Assign Cards'}
                </button>
              </div>

              {/* Assign picker */}
              {showAssign && specCards.length > 0 && (
                <div className="border border-indigo-200 dark:border-indigo-800 rounded-lg p-3 space-y-1 max-h-48 overflow-y-auto bg-indigo-50/30 dark:bg-indigo-900/10">
                  {specCards.map((c: any) => {
                    const isAssigned = c.sprint_id === sprintId;
                    return (
                      <div key={c.id} className="flex items-center justify-between p-1.5 rounded hover:bg-white dark:hover:bg-gray-800">
                        <div className="flex items-center gap-2 flex-1 min-w-0">
                          <span className={`w-2 h-2 rounded-full shrink-0 ${c.status === 'done' ? 'bg-green-500' : c.status === 'in_progress' ? 'bg-blue-500' : 'bg-gray-400'}`} />
                          <span className="text-xs text-gray-800 dark:text-gray-200 truncate">{c.title}</span>
                          {c.card_type === 'test' && <span className="text-[9px] px-1 bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 rounded">test</span>}
                          {c.card_type === 'bug' && <span className="text-[9px] px-1 bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 rounded">bug</span>}
                        </div>
                        <button
                          onClick={async () => {
                            try {
                              if (isAssigned) {
                                await api.unassignTasksFromSprint(sprintId, [c.id]);
                                toast.success('Unassigned');
                              } else {
                                await api.assignTasksToSprint(sprintId, [c.id]);
                                toast.success('Assigned');
                              }
                              loadSprint();
                              const spec = await api.getSpec(sprint.spec_id);
                              setSpecCards((spec.cards || []).filter((x: any) => !x.sprint_id || x.sprint_id === sprintId));
                            } catch (e: any) { toast.error(e?.message || 'Failed'); }
                          }}
                          className={`text-xs px-2 py-0.5 rounded ${isAssigned ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20' : 'text-indigo-600 hover:bg-indigo-50 dark:hover:bg-indigo-900/20'}`}
                        >
                          {isAssigned ? 'Remove' : '+ Add'}
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Assigned cards list */}
              {sprint.cards && sprint.cards.length > 0 ? (
                sprint.cards.map(card => (
                  <div key={card.id} className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                    <span className={`w-2 h-2 rounded-full ${
                      card.status === 'done' ? 'bg-green-500' :
                      card.status === 'in_progress' ? 'bg-blue-500' :
                      card.status === 'cancelled' ? 'bg-red-500' : 'bg-gray-400'
                    }`} />
                    <span className="text-sm text-gray-900 dark:text-white flex-1 truncate">{card.title}</span>
                    {card.card_type === 'test' && <span className="text-[9px] px-1.5 py-0.5 bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 rounded">test</span>}
                    <span className="text-xs text-gray-400">{card.status}</span>
                  </div>
                ))
              ) : !showAssign ? (
                <div className="text-center py-6">
                  <Link2 size={24} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                  <p className="text-sm text-gray-400">No cards assigned to this sprint</p>
                  <p className="text-xs text-gray-400 mt-1">Click "Assign Cards" to add tasks</p>
                </div>
              ) : null}
            </div>
          )}

          {activeTab === 'evaluations' && (
            <div className="space-y-3">
              {sprint.evaluations && sprint.evaluations.length > 0 ? (
                sprint.evaluations.map((ev: any) => (
                  <div key={ev.id} className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-gray-900 dark:text-white">{ev.evaluator_name}</span>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        ev.recommendation === 'approve' ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' :
                        ev.recommendation === 'reject' ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' :
                        'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                      }`}>
                        {ev.recommendation}
                      </span>
                    </div>
                    <div className="text-xs text-gray-500">Score: {ev.overall_score}/100</div>
                    {ev.stale && <span className="text-xs text-amber-500">Stale</span>}
                  </div>
                ))
              ) : (
                <p className="text-sm text-gray-400 text-center py-6">No evaluations yet</p>
              )}
            </div>
          )}

          {activeTab === 'qa' && (
            <div className="space-y-3">
              {sprint.qa_items && sprint.qa_items.length > 0 ? (
                sprint.qa_items.map(qa => (
                  <div key={qa.id} className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                    <p className="text-sm font-medium text-gray-900 dark:text-white">{qa.question}</p>
                    {qa.answer ? (
                      <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">{qa.answer}</p>
                    ) : (
                      <p className="text-xs text-amber-500 mt-1">Awaiting answer</p>
                    )}
                  </div>
                ))
              ) : (
                <p className="text-sm text-gray-400 text-center py-6">No Q&A items</p>
              )}
            </div>
          )}

          {activeTab === 'history' && (
            <SprintHistoryTab sprintId={sprintId} api={api} />
          )}
        </div>

        {/* Footer */}
        {action && (
          <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-700 flex justify-between items-center">
            <button
              onClick={() => handleMove('cancelled')}
              disabled={movingTo !== null}
              className="text-xs text-red-500 hover:text-red-600"
            >
              Cancel Sprint
            </button>
            <button
              onClick={() => handleMove(action.status)}
              disabled={movingTo !== null}
              className="px-4 py-2 text-sm bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
            >
              {movingTo ? 'Moving...' : action.label}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
