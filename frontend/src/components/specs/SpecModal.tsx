/**
 * SpecModal - View and edit a spec, derive cards, manage skills and knowledge bases
 */

import { useEffect, useState } from 'react';
import {
  X,
  ChevronRight,
  CheckCircle2,
  Circle,
  Clock,
  Ban,
  FileText,
  Settings,
  Target,
  Link2,
  BookOpen,
  Wrench,
  Plus,
  Trash2,
  ChevronDown,
  ChevronUp,
  MessageCircleQuestion,
  Send,
  History,
  ArrowRight,
  Lightbulb,
  Layers,
  FlaskConical,
  Link,
  Unlink,
  Monitor,
  RefreshCw,
  Maximize2,
  Minimize2,
  Scale,
  FileCode,
  Download,
  Network,
  ShieldCheck,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { exportSpec, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import type { Spec, SpecStatus, SpecSkill, SpecKnowledgeSummary, SpecQAItem, SpecHistoryEntry, TestScenario, BoardSettings } from '@/types';
import { SubmitSpecValidationModal } from './SubmitSpecValidationModal';
import { MockupsTab } from './MockupsTab';
import { RulesTab } from './RulesTab';
import { ContractsTab } from './ContractsTab';
import { TechnicalRequirementsTab } from './TechnicalRequirementsTab';
import { KGValidationTab } from './KGValidationTab';
import { SpecValidationHistoryPanel } from './SpecValidationHistoryPanel';
import { SprintSuggestionModal } from '@/components/sprints/SprintSuggestionModal';
import { SPEC_STATUSES, SPEC_STATUS_LABELS } from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { IdeationModal } from '@/components/ideations/IdeationModal';
import { RefinementModal } from '@/components/refinements/RefinementModal';
import { EditableField } from '@/components/shared/EditableField';
import { ValidationGateOverride } from '@/components/shared/ValidationGateOverride';

interface SpecModalProps {
  specId: string;
  boardId: string;
  onClose: () => void;
  onChanged: () => void;
}

type ModalTab = 'details' | 'tests' | 'rules' | 'contracts' | 'trs' | 'mockups' | 'qa' | 'skills' | 'knowledge' | 'cards' | 'sprints' | 'history' | 'validation' | 'kg';

const STATUS_ICON: Record<SpecStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  validated: <CheckCircle2 size={14} />,
  in_progress: <Settings size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<SpecStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  validated: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  in_progress: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

const CARD_STATUS_COLORS: Record<string, string> = {
  not_started: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  started: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  in_progress: 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900/40 dark:text-indigo-300',
  on_hold: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  done: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
};

function EditableRequirementsList({
  title,
  icon,
  items,
  onUpdate,
  placeholder,
  renderItemExtra,
}: {
  title: string;
  icon: React.ReactNode;
  items: string[] | null;
  onUpdate: (items: string[]) => void;
  placeholder: string;
  renderItemExtra?: (item: string, index: number) => React.ReactNode;
}) {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed) {
      onUpdate([...(items || []), trimmed]);
      setDraft('');
    }
  };

  const remove = (idx: number) => {
    onUpdate((items || []).filter((_, i) => i !== idx));
  };

  const hasItems = items && items.length > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-1.5">
          {icon} {title}
          {hasItems && <span className="text-xs font-normal text-gray-400">({items.length})</span>}
        </h4>
        {!editing && (
          <button
            onClick={() => setEditing(true)}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-0.5"
          >
            <Plus size={12} /> Add
          </button>
        )}
      </div>

      {hasItems ? (
        <ol className="space-y-1.5 ml-1">
          {items.map((item, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-400 group">
              <span className="text-xs text-gray-400 mt-0.5 w-4 shrink-0">{i + 1}.</span>
              <span className="flex-1">{item}</span>
              {renderItemExtra?.(item, i)}
              <button
                onClick={() => remove(i)}
                className="opacity-0 group-hover:opacity-100 p-0.5 text-red-400 hover:text-red-600 transition-opacity"
              >
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-xs text-gray-400 dark:text-gray-500 italic ml-1">
          No {title.toLowerCase()} defined yet
        </p>
      )}

      {editing && (
        <div className="flex gap-2 mt-2">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { add(); } if (e.key === 'Escape') { setEditing(false); setDraft(''); } }}
            placeholder={placeholder}
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
            autoFocus
          />
          <button onClick={add} disabled={!draft.trim()} className="btn btn-primary text-xs">Add</button>
          <button onClick={() => { setEditing(false); setDraft(''); }} className="btn btn-secondary text-xs">Done</button>
        </div>
      )}
    </div>
  );
}

/* ============================================================
   History Tab
   ============================================================ */

const ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  updated: 'Updated',
  status_changed: 'Status changed',
  cards_derived: 'Cards derived',
  skill_added: 'Skill added',
  skill_removed: 'Skill removed',
  knowledge_added: 'Knowledge added',
  knowledge_removed: 'Knowledge removed',
  qa_added: 'Question added',
  qa_answered: 'Question answered',
};

const ACTION_COLORS: Record<string, string> = {
  created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  status_changed: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  cards_derived: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
};

function formatValue(val: unknown): string {
  if (val === null || val === undefined) return '(empty)';
  if (Array.isArray(val)) {
    if (val.length === 0) return '(empty list)';
    return val.map((v, i) => `${i + 1}. ${v}`).join('\n');
  }
  return String(val);
}

const SCENARIO_TYPES = ['unit', 'integration', 'e2e', 'manual'] as const;
const SCENARIO_STATUSES = ['draft', 'ready', 'automated', 'passed', 'failed'] as const;

const SCENARIO_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  ready: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  automated: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  passed: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

const SCENARIO_TYPE_COLORS: Record<string, string> = {
  unit: 'bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300',
  integration: 'bg-violet-50 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300',
  e2e: 'bg-amber-50 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300',
  manual: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
};

function TestScenariosTab({ spec, onUpdate, onSpecUpdate }: { spec: Spec; onUpdate: (scenarios: TestScenario[]) => void; onSpecUpdate: (data: Record<string, unknown>) => Promise<void> }) {
  const api = useDashboardApi();
  const [adding, setAdding] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [linkingScenarioId, setLinkingScenarioId] = useState<string | null>(null);

  // New scenario form
  const [newTitle, setNewTitle] = useState('');
  const [newType, setNewType] = useState<string>('integration');
  const [newGiven, setNewGiven] = useState('');
  const [newWhen, setNewWhen] = useState('');
  const [newThen, setNewThen] = useState('');
  const [newNotes, setNewNotes] = useState('');
  const [newCriteria, setNewCriteria] = useState<string[]>([]);

  const scenarios = spec.test_scenarios || [];
  const criteria = spec.acceptance_criteria || [];

  const handleAdd = () => {
    if (!newTitle.trim() || !newGiven.trim() || !newWhen.trim() || !newThen.trim()) return;
    const id = `ts_${Date.now()}`;
    const scenario: TestScenario = {
      id,
      title: newTitle.trim(),
      linked_criteria: newCriteria.length > 0 ? newCriteria : null,
      scenario_type: newType as TestScenario['scenario_type'],
      given: newGiven.trim(),
      when: newWhen.trim(),
      then: newThen.trim(),
      notes: newNotes.trim() || null,
      status: 'draft',
      linked_task_ids: null,
    };
    onUpdate([...scenarios, scenario]);
    setAdding(false);
    setNewTitle(''); setNewType('integration'); setNewGiven(''); setNewWhen(''); setNewThen(''); setNewNotes(''); setNewCriteria([]);
  };

  const handleRemove = (id: string) => {
    onUpdate(scenarios.filter((s) => s.id !== id));
  };

  const handleStatusChange = (id: string, status: string) => {
    onUpdate(scenarios.map((s) => s.id === id ? { ...s, status: status as TestScenario['status'] } : s));
  };

  // Coverage matrix
  const coverageMap = new Map<string, string[]>();
  criteria.forEach((c, i) => {
    const covering = scenarios.filter((s) => s.linked_criteria?.includes(c) || s.linked_criteria?.includes(String(i)));
    coverageMap.set(c, covering.map((s) => s.id));
  });
  const uncoveredCriteria = criteria.filter((c) => !coverageMap.get(c)?.length);

  return (
    <div className="space-y-4">
      {/* Coverage summary */}
      {criteria.length > 0 && (() => {
        const coveredCount = criteria.length - uncoveredCriteria.length;
        const coveragePct = Math.round((coveredCount / criteria.length) * 100);
        return (
          <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                AC Coverage ({coveredCount}/{criteria.length})
              </h4>
              {coveredCount === criteria.length ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                  100% covered
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 font-medium">
                  {coveragePct}% covered
                </span>
              )}
            </div>
            {/* Progress bar */}
            <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
              <div
                className={`h-full transition-all duration-500 rounded-full ${coveredCount === criteria.length ? 'bg-green-500' : 'bg-amber-500'}`}
                style={{ width: `${coveragePct}%` }}
              />
            </div>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {criteria.map((c, i) => {
                const covering = coverageMap.get(c) || [];
                const covered = covering.length > 0;
                return (
                  <div key={i} className="flex items-start gap-2 text-xs">
                    <span className={`mt-0.5 w-3 h-3 rounded-full shrink-0 ${covered ? 'bg-green-500' : 'bg-red-400'}`} />
                    <span className={`flex-1 line-clamp-1 ${covered ? 'text-gray-600 dark:text-gray-400' : 'text-red-600 dark:text-red-400 font-medium'}`}>
                      {c}
                    </span>
                    <span className="text-gray-400 shrink-0">{covering.length} test{covering.length !== 1 ? 's' : ''}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })()}

      {/* Skip test coverage toggle */}
      <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
        <div>
          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip test coverage requirement</span>
          <p className="text-[10px] text-gray-400">Allow moving spec to Done without full test coverage</p>
        </div>
        <button
          onClick={() => onSpecUpdate({ skip_test_coverage: !spec.skip_test_coverage })}
          className={`relative w-10 h-5 rounded-full transition-colors ${spec.skip_test_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
        >
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${spec.skip_test_coverage ? 'translate-x-5' : ''}`} />
        </button>
      </div>

      {/* Scenarios list */}
      {scenarios.length === 0 && !adding && (
        <div className="text-center py-6">
          <FlaskConical size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No test scenarios</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Define test scenarios to validate acceptance criteria</p>
        </div>
      )}

      {scenarios.map((scenario) => {
        const isExpanded = expandedId === scenario.id;
        const linkedCards = scenario.linked_task_ids?.length || 0;
        return (
          <div key={scenario.id} className={`border rounded-lg overflow-hidden ${linkedCards > 0 ? 'border-gray-200 dark:border-gray-700' : 'border-amber-300 dark:border-amber-700 border-dashed'}`}>
            <div
              className={`flex items-center gap-2 px-3 py-2 cursor-pointer ${linkedCards > 0 ? 'bg-gray-50 dark:bg-gray-700/50' : 'bg-amber-50/50 dark:bg-amber-900/10'}`}
              onClick={() => setExpandedId(isExpanded ? null : scenario.id)}
            >
              <FlaskConical size={14} className={linkedCards > 0 ? 'text-violet-500 shrink-0' : 'text-amber-500 shrink-0'} />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">{scenario.title}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${SCENARIO_TYPE_COLORS[scenario.scenario_type] || ''}`}>
                {scenario.scenario_type}
              </span>
              <select
                value={scenario.status}
                onChange={(e) => { e.stopPropagation(); handleStatusChange(scenario.id, e.target.value); }}
                onClick={(e) => e.stopPropagation()}
                className={`text-[10px] px-1.5 py-0.5 rounded border-0 cursor-pointer ${SCENARIO_STATUS_COLORS[scenario.status] || ''}`}
              >
                {SCENARIO_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              {linkedCards > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                  {linkedCards} task{linkedCards !== 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-400 font-medium animate-pulse">
                  no tasks
                </span>
              )}
              <button onClick={(e) => { e.stopPropagation(); handleRemove(scenario.id); }} className="p-0.5 text-gray-400 hover:text-red-500">
                <Trash2 size={12} />
              </button>
            </div>
            {isExpanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <span className="text-[10px] font-semibold text-green-600 uppercase">Given</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.given}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-blue-600 uppercase">When</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.when}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-violet-600 uppercase">Then</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.then}</p>
                  </div>
                </div>
                {scenario.notes && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">{scenario.notes}</p>
                )}
                {scenario.linked_criteria && scenario.linked_criteria.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Validates:</span>
                    {scenario.linked_criteria.map((c, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300">
                        {c.length > 60 ? c.slice(0, 57) + '...' : c}
                      </span>
                    ))}
                  </div>
                )}
                {/* Linked tasks */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-gray-400">Linked tasks:</span>
                    <button
                      onClick={() => setLinkingScenarioId(linkingScenarioId === scenario.id ? null : scenario.id)}
                      className="text-[10px] text-blue-500 hover:text-blue-600 dark:text-blue-400"
                    >
                      {linkingScenarioId === scenario.id ? 'Cancel' : '+ Link task'}
                    </button>
                  </div>
                  {scenario.linked_task_ids && scenario.linked_task_ids.length > 0 && (
                    <div className="space-y-1">
                      {scenario.linked_task_ids.map((taskId) => {
                        const card = spec.cards?.find((c) => c.id === taskId);
                        return (
                          <div key={taskId} className="flex items-center justify-between px-2 py-1 rounded bg-blue-50 dark:bg-blue-900/10 text-xs group">
                            <span className="text-gray-700 dark:text-gray-300 truncate">
                              {card ? card.title : taskId.slice(0, 12) + '...'}
                            </span>
                            <div className="flex items-center gap-1">
                              {card && (
                                <span className={`text-[10px] px-1 py-0.5 rounded ${
                                  card.status === 'done' ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' :
                                  card.status === 'in_progress' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                                  'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                                }`}>
                                  {card.status.replace('_', ' ')}
                                </span>
                              )}
                              <button
                                onClick={async () => {
                                  try {
                                    await api.unlinkTaskFromScenario(spec.id, scenario.id, taskId);
                                    const updated = await api.getSpec(spec.id);
                                    onUpdate(updated.test_scenarios || []);
                                    toast.success('Task unlinked');
                                  } catch { toast.error('Failed to unlink'); }
                                }}
                                className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                                title="Unlink task"
                              >
                                <Unlink size={10} />
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {(!scenario.linked_task_ids || scenario.linked_task_ids.length === 0) && (
                    <p className="text-[10px] text-gray-400 italic">No tasks linked to this scenario yet</p>
                  )}
                  {/* Task picker */}
                  {linkingScenarioId === scenario.id && spec.cards && spec.cards.length > 0 && (
                    <div className="mt-1 border border-gray-200 dark:border-gray-700 rounded p-1.5 max-h-32 overflow-y-auto space-y-0.5">
                      {spec.cards
                        .filter((c) => !(scenario.linked_task_ids || []).includes(c.id))
                        .map((c) => (
                          <button
                            key={c.id}
                            onClick={async () => {
                              try {
                                await api.linkTaskToScenario(spec.id, scenario.id, c.id);
                                const updated = await api.getSpec(spec.id);
                                onUpdate(updated.test_scenarios || []);
                                setLinkingScenarioId(null);
                                toast.success('Task linked');
                              } catch { toast.error('Failed to link'); }
                            }}
                            className="w-full text-left px-2 py-1 rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 truncate flex items-center gap-1"
                          >
                            <Link size={9} className="shrink-0 text-gray-400" />
                            {c.title}
                          </button>
                        ))}
                      {spec.cards.filter((c) => !(scenario.linked_task_ids || []).includes(c.id)).length === 0 && (
                        <p className="text-[10px] text-gray-400 italic px-1">All cards already linked</p>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}

      {/* Add scenario form */}
      {adding ? (
        <div className="border border-violet-200 dark:border-violet-700 rounded-lg p-3 space-y-2 bg-violet-50/50 dark:bg-violet-900/10">
          <div className="flex gap-2">
            <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Scenario title" className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" autoFocus />
            <select value={newType} onChange={(e) => setNewType(e.target.value)} className="px-2 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600">
              {SCENARIO_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <textarea value={newGiven} onChange={(e) => setNewGiven(e.target.value)} placeholder="Given: precondition..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
            <textarea value={newWhen} onChange={(e) => setNewWhen(e.target.value)} placeholder="When: action..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
            <textarea value={newThen} onChange={(e) => setNewThen(e.target.value)} placeholder="Then: expected result..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
          </div>
          <textarea value={newNotes} onChange={(e) => setNewNotes(e.target.value)} placeholder="Notes (optional)" className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={1} />
          {/* Link to acceptance criteria */}
          {criteria.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to acceptance criteria:</span>
              <div className="flex flex-wrap gap-1">
                {criteria.map((c, i) => {
                  const isLinked = newCriteria.includes(c);
                  return (
                    <button
                      key={i}
                      onClick={() => setNewCriteria(isLinked ? newCriteria.filter((x) => x !== c) : [...newCriteria, c])}
                      className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${
                        isLinked
                          ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 ring-1 ring-green-400'
                          : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'
                      }`}
                    >
                      {c.length > 60 ? c.slice(0, 57) + '...' : c}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAdd} disabled={!newTitle.trim() || !newGiven.trim() || !newWhen.trim() || !newThen.trim()} className="btn btn-primary text-xs">Add Scenario</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-violet-600 dark:text-violet-400 hover:text-violet-800 dark:hover:text-violet-300">
          <Plus size={14} /> Add Test Scenario
        </button>
      )}
    </div>
  );
}

function HistoryTab({ specId }: { specId: string }) {
  const api = useDashboardApi();
  const [entries, setEntries] = useState<SpecHistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecHistory(specId);
      setEntries(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

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
      {entries.map((entry) => {
        const isExpanded = expandedId === entry.id;
        const actionColor = ACTION_COLORS[entry.action] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
        const hasChanges = entry.changes && entry.changes.length > 0;

        return (
          <div
            key={entry.id}
            className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
          >
            <div
              className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/30"
              onClick={() => hasChanges && setExpandedId(isExpanded ? null : entry.id)}
            >
              {/* Timeline dot */}
              <div className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 shrink-0" />

              {/* Action badge */}
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${actionColor}`}>
                {ACTION_LABELS[entry.action] || entry.action}
              </span>

              {/* Summary */}
              <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">
                {entry.summary || entry.action}
              </span>

              {/* Actor + time */}
              <div className="flex items-center gap-2 shrink-0 text-[10px] text-gray-400">
                <span className={`px-1 py-0.5 rounded ${
                  entry.actor_type === 'agent'
                    ? 'bg-violet-100 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300'
                    : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                }`}>
                  {entry.actor_name}
                </span>
                {entry.version && <span>v{entry.version}</span>}
                <span>{new Date(entry.created_at).toLocaleString()}</span>
              </div>

              {hasChanges && (
                <span className="text-gray-400 shrink-0">
                  {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
              )}
            </div>

            {/* Expanded diff view */}
            {isExpanded && hasChanges && (
              <div className="px-3 py-2 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 space-y-2">
                {entry.changes!.map((change, idx) => (
                  <div key={idx} className="text-sm">
                    <div className="font-medium text-gray-700 dark:text-gray-300 text-xs uppercase tracking-wide mb-1">
                      {change.field}
                    </div>
                    <div className="flex items-start gap-2">
                      {/* Old value */}
                      <div className="flex-1 min-w-0">
                        <div className="text-[10px] text-red-500 font-medium mb-0.5">Before</div>
                        <pre className="text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                          {formatValue(change.old)}
                        </pre>
                      </div>
                      <ArrowRight size={14} className="text-gray-400 mt-4 shrink-0" />
                      {/* New value */}
                      <div className="flex-1 min-w-0">
                        <div className="text-[10px] text-green-500 font-medium mb-0.5">After</div>
                        <pre className="text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 rounded px-2 py-1 whitespace-pre-wrap overflow-x-auto max-h-32 overflow-y-auto">
                          {formatValue(change.new)}
                        </pre>
                      </div>
                    </div>
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

/* ============================================================
   Q&A Tab
   ============================================================ */

function ChoiceOptionsDisplay({ choices, selected }: { choices: SpecQAItem['choices']; selected: string[] | null }) {
  if (!choices) return null;
  return (
    <div className="space-y-1 mt-1">
      {choices.map((opt) => {
        const isSelected = selected?.includes(opt.id);
        return (
          <div key={opt.id} className={`flex items-center gap-2 text-sm px-2 py-1 rounded ${
            isSelected
              ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300 font-medium'
              : 'text-gray-600 dark:text-gray-400'
          }`}>
            <span className={`w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 ${
              isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-600'
            }`}>
              {isSelected && <CheckCircle2 size={12} className="text-white" />}
            </span>
            {opt.label}
          </div>
        );
      })}
    </div>
  );
}

function ChoiceAnswerForm({
  qa,
  onAnswer,
  onCancel,
}: {
  qa: SpecQAItem;
  onAnswer: (qaId: string, answer: string | null, selected: string[] | null) => void;
  onCancel: () => void;
}) {
  const [sel, setSel] = useState<string[]>([]);
  const [freeText, setFreeText] = useState('');

  const toggleOption = (optId: string) => {
    // `single_choice` is an alias of `choice` — accept both for single-select.
    const isSingle = qa.question_type === 'choice' || qa.question_type === 'single_choice';
    if (isSingle) {
      setSel([optId]);
    } else {
      setSel((prev) => prev.includes(optId) ? prev.filter((s) => s !== optId) : [...prev, optId]);
    }
  };

  const canSubmit = sel.length > 0 || (qa.allow_free_text && freeText.trim());

  return (
    <div className="mt-2 space-y-2">
      <div className="space-y-1">
        {qa.choices?.map((opt) => (
          <button
            key={opt.id}
            onClick={() => toggleOption(opt.id)}
            className={`flex items-center gap-2 w-full text-sm text-left px-2 py-1.5 rounded transition-colors ${
              sel.includes(opt.id)
                ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300'
                : 'bg-gray-50 dark:bg-gray-700/50 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            <span className={`w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 ${
              sel.includes(opt.id) ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-600'
            }`}>
              {sel.includes(opt.id) && <CheckCircle2 size={10} className="text-white" />}
            </span>
            {opt.label}
          </button>
        ))}
      </div>
      {qa.allow_free_text && (
        <input
          type="text"
          value={freeText}
          onChange={(e) => setFreeText(e.target.value)}
          placeholder="Additional comment..."
          className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        />
      )}
      <div className="flex justify-end gap-2">
        <button onClick={onCancel} className="btn btn-secondary text-xs">Cancel</button>
        <button
          onClick={() => onAnswer(qa.id, freeText.trim() || null, sel.length > 0 ? sel : null)}
          disabled={!canSubmit}
          className="btn btn-primary text-xs"
        >
          Submit
        </button>
      </div>
    </div>
  );
}

function QATab({ specId, mentionables }: { specId: string; mentionables: Mentionable[] }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<SpecQAItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [answerDraft, setAnswerDraft] = useState('');

  // Ask question form
  const [askMode, setAskMode] = useState<'text' | 'choice'>('text');
  const [newQuestion, setNewQuestion] = useState('');
  const [newOptions, setNewOptions] = useState('');
  const [newMulti, setNewMulti] = useState(false);
  const [newAllowFreeText, setNewAllowFreeText] = useState(false);

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecQA(specId);
      setItems(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAskText = async () => {
    if (!newQuestion.trim()) return;
    try {
      await api.createSpecQuestion(specId, newQuestion.trim());
      setNewQuestion('');
      toast.success('Question posted');
      await load();
    } catch { toast.error('Failed to post question'); }
  };

  const handleAskChoice = async () => {
    if (!newQuestion.trim() || !newOptions.trim()) return;
    const optLabels = newOptions.split(',').map((o) => o.trim()).filter(Boolean);
    if (optLabels.length < 2) { toast.error('Need at least 2 options'); return; }
    try {
      await api.createSpecChoiceQuestion(specId, {
        question: newQuestion.trim(),
        question_type: (newMulti ? 'multi_choice' : 'choice') as 'choice' | 'multi_choice',
        choices: optLabels.map((label, i) => ({ id: `opt_${i}`, label })),
        allow_free_text: newAllowFreeText,
      });
      setNewQuestion(''); setNewOptions(''); setNewMulti(false); setNewAllowFreeText(false);
      toast.success('Choice question posted');
      await load();
    } catch { toast.error('Failed to post choice question'); }
  };

  const handleAnswer = async (qaId: string, answer: string | null, selected: string[] | null) => {
    try {
      await api.answerSpecQuestion(specId, qaId, answer || '', selected);
      setAnsweringId(null);
      setAnswerDraft('');
      toast.success('Answer posted');
      await load();
    } catch { toast.error('Failed to post answer'); }
  };

  const handleTextAnswer = async (qaId: string) => {
    if (!answerDraft.trim()) return;
    await handleAnswer(qaId, answerDraft.trim(), null);
  };

  const handleDelete = async (qaId: string) => {
    if (!confirm('Delete this Q&A?')) return;
    try {
      await api.deleteSpecQuestion(specId, qaId);
      await load();
    } catch { toast.error('Failed to delete'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading Q&A...</div>;

  const isAnswered = (qa: SpecQAItem) => qa.answer || (qa.selected && qa.selected.length > 0);
  const unanswered = items.filter((q) => !isAnswered(q));
  const answered = items.filter((q) => isAnswered(q));

  return (
    <div className="space-y-4">
      {/* Ask mode toggle + form */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setAskMode('text')}
            className={`text-xs px-2 py-1 rounded ${askMode === 'text' ? 'bg-blue-600 text-white' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'}`}
          >
            Free Text
          </button>
          <button
            onClick={() => setAskMode('choice')}
            className={`text-xs px-2 py-1 rounded ${askMode === 'choice' ? 'bg-blue-600 text-white' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'}`}
          >
            Choice / Form
          </button>
        </div>

        {askMode === 'text' ? (
          <div className="flex gap-2">
            <MentionInput
              value={newQuestion}
              onChange={setNewQuestion}
              onSubmit={handleAskText}
              placeholder="Ask a question... (type @ to mention)"
              mentionables={mentionables}
              className="flex-1"
            />
            <button onClick={handleAskText} disabled={!newQuestion.trim()} className="btn btn-primary flex items-center gap-1 text-sm shrink-0">
              <Send size={14} /> Ask
            </button>
          </div>
        ) : (
          <div className="border border-blue-200 dark:border-blue-700 rounded-lg p-3 space-y-2 bg-blue-50/30 dark:bg-blue-900/10">
            <MentionInput
              value={newQuestion}
              onChange={setNewQuestion}
              placeholder="Question... (type @ to mention)"
              mentionables={mentionables}
              className="w-full"
            />
            <input type="text" value={newOptions} onChange={(e) => setNewOptions(e.target.value)} placeholder="Options (comma-separated): OAuth2, API Keys, Both" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <input type="checkbox" checked={newMulti} onChange={(e) => setNewMulti(e.target.checked)} className="rounded" />
                Multi-select
              </label>
              <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <input type="checkbox" checked={newAllowFreeText} onChange={(e) => setNewAllowFreeText(e.target.checked)} className="rounded" />
                Allow free text
              </label>
            </div>
            <div className="flex justify-end">
              <button onClick={handleAskChoice} disabled={!newQuestion.trim() || !newOptions.trim()} className="btn btn-primary flex items-center gap-1 text-sm">
                <Send size={14} /> Post Choice
              </button>
            </div>
          </div>
        )}
      </div>

      {items.length === 0 && (
        <div className="text-center py-6">
          <MessageCircleQuestion size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No questions yet</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Ask questions to clarify spec requirements before work begins</p>
        </div>
      )}

      {/* Unanswered */}
      {unanswered.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-amber-600 dark:text-amber-400 uppercase tracking-wide mb-2">
            Unanswered ({unanswered.length})
          </h4>
          <div className="space-y-2">
            {unanswered.map((qa) => (
              <div key={qa.id} className="border border-amber-200 dark:border-amber-700/50 rounded-lg p-3 bg-amber-50/50 dark:bg-amber-900/10">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <p className="text-sm text-gray-900 dark:text-white">{qa.question}</p>
                      {qa.question_type !== 'text' && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300">
                          {qa.question_type === 'multi_choice' ? 'multi-select' : 'single-select'}
                        </span>
                      )}
                    </div>
                    {qa.question_type !== 'text' && qa.choices && (
                      <div className="mt-1 space-y-0.5">
                        {qa.choices.map((opt) => (
                          <div key={opt.id} className="text-xs text-gray-500 dark:text-gray-400 pl-2">
                            &bull; {opt.label}
                          </div>
                        ))}
                      </div>
                    )}
                    <span className="text-[10px] text-gray-400 mt-1 block">
                      Asked by {qa.asked_by.slice(0, 12)}... &middot; {new Date(qa.created_at).toLocaleDateString()}
                    </span>
                  </div>
                  <button onClick={() => handleDelete(qa.id)} className="p-1 text-gray-400 hover:text-red-500 shrink-0">
                    <Trash2 size={12} />
                  </button>
                </div>
                {answeringId === qa.id ? (
                  qa.question_type !== 'text' ? (
                    <ChoiceAnswerForm qa={qa} onAnswer={handleAnswer} onCancel={() => setAnsweringId(null)} />
                  ) : (
                    <div className="mt-2 flex gap-2">
                      <MentionInput
                        value={answerDraft}
                        onChange={setAnswerDraft}
                        onSubmit={() => handleTextAnswer(qa.id)}
                        placeholder="Type your answer... (@ to mention)"
                        mentionables={mentionables}
                        className="flex-1"
                        autoFocus
                      />
                      <button onClick={() => handleTextAnswer(qa.id)} disabled={!answerDraft.trim()} className="btn btn-primary text-xs">Answer</button>
                      <button onClick={() => { setAnsweringId(null); setAnswerDraft(''); }} className="btn btn-secondary text-xs">Cancel</button>
                    </div>
                  )
                ) : (
                  <button
                    onClick={() => { setAnsweringId(qa.id); setAnswerDraft(''); }}
                    className="mt-2 text-xs text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    Answer this question
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Answered */}
      {answered.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-green-600 dark:text-green-400 uppercase tracking-wide mb-2">
            Answered ({answered.length})
          </h4>
          <div className="space-y-2">
            {answered.map((qa) => (
              <div key={qa.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <p className="text-sm text-gray-900 dark:text-white">{qa.question}</p>
                    {qa.question_type !== 'text' && (
                      <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300">
                        {qa.question_type === 'multi_choice' ? 'multi' : 'choice'}
                      </span>
                    )}
                  </div>
                  <button onClick={() => handleDelete(qa.id)} className="p-1 text-gray-400 hover:text-red-500 shrink-0">
                    <Trash2 size={12} />
                  </button>
                </div>
                <span className="text-[10px] text-gray-400 block mt-0.5">Asked by {qa.asked_by.slice(0, 12)}...</span>
                <div className="mt-2 pl-3 border-l-2 border-green-300 dark:border-green-600">
                  {qa.question_type !== 'text' && qa.choices && (
                    <ChoiceOptionsDisplay choices={qa.choices} selected={qa.selected} />
                  )}
                  {qa.answer && <p className="text-sm text-gray-700 dark:text-gray-300 mt-1">{qa.answer}</p>}
                  <span className="text-[10px] text-gray-400 block mt-0.5">
                    Answered by {qa.answered_by?.slice(0, 12)}... &middot; {qa.answered_at ? new Date(qa.answered_at).toLocaleDateString() : ''}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ============================================================
   Skills Tab
   ============================================================ */

function SpecSprintsTab({ sprints, api }: { sprints: any[]; api: ReturnType<typeof useDashboardApi> }) {
  const [details, setDetails] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (sprints.length === 0) { setLoading(false); return; }
    Promise.all(sprints.map(s => api.getSprint(s.id).catch(() => null)))
      .then(results => {
        const map: Record<string, any> = {};
        for (const r of results) { if (r) map[r.id] = r; }
        setDetails(map);
      })
      .finally(() => setLoading(false));
  }, [sprints.length]);

  if (loading) return <p className="text-sm text-gray-400 text-center py-6">Loading sprints...</p>;
  if (sprints.length === 0) return <p className="text-sm text-gray-400 text-center py-6">No sprints linked to this spec</p>;

  return (
    <div className="space-y-3">
      {sprints.map((sprint: any) => {
        const detail = details[sprint.id];
        const cards = detail?.cards || [];
        const total = cards.length;
        const done = cards.filter((c: any) => c.status === 'done').length;
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
        return (
          <div key={sprint.id} className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium text-white ${
                  sprint.status === 'closed' ? 'bg-green-500' :
                  sprint.status === 'active' ? 'bg-blue-500' :
                  sprint.status === 'review' ? 'bg-amber-500' :
                  sprint.status === 'cancelled' ? 'bg-red-500' : 'bg-gray-500'
                }`}>{sprint.status}</span>
                <span className="text-sm font-medium text-gray-900 dark:text-white">{sprint.title}</span>
              </div>
              <span className="text-xs font-bold text-gray-600 dark:text-gray-300">{pct}%</span>
            </div>
            <div className="w-full bg-gray-200 dark:bg-gray-600 rounded-full h-2 mb-1">
              <div
                className={`h-2 rounded-full transition-all ${pct === 100 ? 'bg-green-500' : pct >= 50 ? 'bg-blue-500' : 'bg-amber-500'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex items-center justify-between mt-1">
              <p className="text-[10px] text-gray-400">{done}/{total} cards done · v{sprint.version}</p>
              {sprint.objective && <p className="text-[10px] text-gray-500 truncate max-w-[60%]">{sprint.objective}</p>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SkillsTab({ specId }: { specId: string }) {
  const api = useDashboardApi();
  const [skills, setSkills] = useState<SpecSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Add form
  const [newSkillId, setNewSkillId] = useState('');
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');
  const [newTags, setNewTags] = useState('');

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecSkills(specId);
      setSkills(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAdd = async () => {
    if (!newSkillId.trim() || !newName.trim() || !newDesc.trim()) return;
    try {
      await api.createSpecSkill(specId, {
        skill_id: newSkillId.trim(),
        name: newName.trim(),
        description: newDesc.trim(),
        tags: newTags ? newTags.split(',').map((t) => t.trim()).filter(Boolean) : undefined,
        sections: newContent.trim() ? [{
          id: 'main',
          title: newName.trim(),
          description: newDesc.trim(),
          level: 'full' as const,
          content: newContent.trim(),
        }] : undefined,
      });
      toast.success('Skill added');
      setAdding(false);
      setNewSkillId(''); setNewName(''); setNewDesc(''); setNewContent(''); setNewTags('');
      await load();
    } catch { toast.error('Failed to add skill'); }
  };

  const handleDelete = async (skillId: string) => {
    if (!confirm('Delete this skill?')) return;
    try {
      await api.deleteSpecSkill(specId, skillId);
      toast.success('Skill deleted');
      await load();
    } catch { toast.error('Failed to delete skill'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading skills...</div>;

  return (
    <div className="space-y-3">
      {skills.length === 0 && !adding && (
        <div className="text-center py-6">
          <Wrench size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No skills attached</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Skills provide structured instructions for AI agents</p>
        </div>
      )}

      {skills.map((skill) => (
        <div key={skill.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div
            className="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-700/50 cursor-pointer"
            onClick={() => setExpandedId(expandedId === skill.id ? null : skill.id)}
          >
            <div className="flex items-center gap-2 min-w-0">
              <Wrench size={14} className="text-violet-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate">{skill.name}</span>
              <span className="text-xs text-gray-400 font-mono">{skill.skill_id}</span>
              {skill.tags && skill.tags.map((tag, i) => (
                <span key={i} className="text-[10px] px-1 py-0.5 rounded bg-violet-100 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300">{tag}</span>
              ))}
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(skill.skill_id); }}
                className="p-1 text-gray-400 hover:text-red-500"
              >
                <Trash2 size={14} />
              </button>
              {expandedId === skill.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </div>
          </div>
          {expandedId === skill.id && (
            <div className="px-3 py-2 space-y-2">
              <p className="text-sm text-gray-600 dark:text-gray-400">{skill.description}</p>
              {skill.sections && skill.sections.map((sec) => (
                <div key={sec.id} className="border-l-2 border-violet-300 dark:border-violet-600 pl-3 mt-2">
                  <h5 className="text-xs font-semibold text-gray-700 dark:text-gray-300">{sec.title}</h5>
                  {sec.description && <p className="text-xs text-gray-500 dark:text-gray-400">{sec.description}</p>}
                  {sec.content && (
                    <pre className="mt-1 text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-48 overflow-y-auto">
                      {sec.content}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}

      {adding ? (
        <div className="border border-violet-200 dark:border-violet-700 rounded-lg p-3 space-y-2 bg-violet-50/50 dark:bg-violet-900/10">
          <div className="grid grid-cols-2 gap-2">
            <input type="text" value={newSkillId} onChange={(e) => setNewSkillId(e.target.value)} placeholder="skill-id (slug)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
            <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Display name" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          </div>
          <input type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Description" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <textarea value={newContent} onChange={(e) => setNewContent(e.target.value)} placeholder="Skill content / instructions..." className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" rows={4} />
          <input type="text" value={newTags} onChange={(e) => setNewTags(e.target.value)} placeholder="Tags (comma-separated)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAdd} disabled={!newSkillId.trim() || !newName.trim() || !newDesc.trim()} className="btn btn-primary text-xs">Add Skill</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-violet-600 dark:text-violet-400 hover:text-violet-800 dark:hover:text-violet-300">
          <Plus size={14} /> Add Skill
        </button>
      )}
    </div>
  );
}

/* ============================================================
   Knowledge Base Tab
   ============================================================ */

function KnowledgeTab({ specId }: { specId: string }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<SpecKnowledgeSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [viewContent, setViewContent] = useState<string>('');

  // Add form
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecKnowledge(specId);
      setItems(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    try {
      await api.createSpecKnowledge(specId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        content: newContent.trim(),
      });
      toast.success('Knowledge base item added');
      setAdding(false);
      setNewTitle(''); setNewDesc(''); setNewContent('');
      await load();
    } catch { toast.error('Failed to add knowledge'); }
  };

  const handleDelete = async (knowledgeId: string) => {
    if (!confirm('Delete this knowledge base item?')) return;
    try {
      await api.deleteSpecKnowledge(specId, knowledgeId);
      toast.success('Deleted');
      if (viewingId === knowledgeId) { setViewingId(null); setViewContent(''); }
      await load();
    } catch { toast.error('Failed to delete'); }
  };

  const handleView = async (knowledgeId: string) => {
    if (viewingId === knowledgeId) { setViewingId(null); setViewContent(''); return; }
    try {
      const kb = await api.getSpecKnowledge(specId, knowledgeId);
      setViewingId(knowledgeId);
      setViewContent(kb.content);
    } catch { toast.error('Failed to load content'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading knowledge base...</div>;

  return (
    <div className="space-y-3">
      {items.length === 0 && !adding && (
        <div className="text-center py-6">
          <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge base items</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Attach reference documents, API specs, or context docs</p>
        </div>
      )}

      {items.map((item) => (
        <div key={item.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div
            className="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-700/50 cursor-pointer"
            onClick={() => handleView(item.id)}
          >
            <div className="flex items-center gap-2 min-w-0">
              <BookOpen size={14} className="text-amber-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate">{item.title}</span>
              <span className="text-[10px] px-1 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-600 dark:text-gray-400">{item.mime_type}</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
                className="p-1 text-gray-400 hover:text-red-500"
              >
                <Trash2 size={14} />
              </button>
              {viewingId === item.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </div>
          </div>
          {item.description && (
            <p className="text-xs text-gray-500 dark:text-gray-400 px-3 py-1">{item.description}</p>
          )}
          {viewingId === item.id && viewContent && (
            <div className="px-3 py-2 border-t border-gray-100 dark:border-gray-700">
              <pre className="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">
                {viewContent}
              </pre>
            </div>
          )}
        </div>
      ))}

      {adding ? (
        <div className="border border-amber-200 dark:border-amber-700 rounded-lg p-3 space-y-2 bg-amber-50/50 dark:bg-amber-900/10">
          <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Title" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <input type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Description (optional)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <textarea value={newContent} onChange={(e) => setNewContent(e.target.value)} placeholder="Content (markdown, text, JSON...)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" rows={6} />
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAdd} disabled={!newTitle.trim() || !newContent.trim()} className="btn btn-primary text-xs">Add</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-amber-600 dark:text-amber-400 hover:text-amber-800 dark:hover:text-amber-300">
          <Plus size={14} /> Add Knowledge
        </button>
      )}
    </div>
  );
}

/* ============================================================
   Validation Error Display — parses gate errors into readable items
   ============================================================ */

function ValidationErrorDisplay({ error }: { error: string }) {
  // Try to extract structured info from the error message
  // Backend returns: "Cannot validate spec: N test scenario(s)... REQUIRED ACTION: ..."
  // Or Pydantic: [{"type":"enum","loc":["body","status"],"msg":"..."}]

  // Split on "REQUIRED ACTION:" to separate issue from fix
  const reqIdx = error.indexOf('REQUIRED ACTION:');
  const issue = reqIdx > 0 ? error.slice(0, reqIdx).trim() : error;
  const action = reqIdx > 0 ? error.slice(reqIdx + 16).trim() : null;

  // Try to detect gate type from keywords
  let gateType = 'unknown';
  if (error.includes('test scenario')) { gateType = 'Test Coverage'; }
  else if (error.includes('business rule')) { gateType = 'Rules Coverage'; }
  else if (error.includes('technical requirement') || error.includes('TR')) { gateType = 'TRs Coverage'; }
  else if (error.includes('api contract') || error.includes('contract')) { gateType = 'Contract Coverage'; }
  else if (error.includes('evaluation') || error.includes('approval')) { gateType = 'Qualitative Validation'; }
  else if (error.includes('state machine') || error.includes('transition')) { gateType = 'State Transition'; }

  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        The following gate must be satisfied before the spec can be validated:
      </p>

      {/* Gate type badge */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 font-semibold uppercase tracking-wide">
          {gateType}
        </span>
      </div>

      {/* Issue description */}
      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
        <p className="text-sm text-red-800 dark:text-red-200 leading-relaxed">
          {issue}
        </p>
      </div>

      {/* Required action */}
      {action && (
        <div className="bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-800 rounded-lg p-4">
          <p className="text-[10px] font-semibold text-amber-700 dark:text-amber-300 uppercase tracking-wide mb-1">
            Required Action
          </p>
          <p className="text-sm text-amber-800 dark:text-amber-200 leading-relaxed">
            {action}
          </p>
        </div>
      )}
    </div>
  );
}

/* ============================================================
   Main SpecModal
   ============================================================ */

export function SpecModal({ specId, boardId: _boardId, onClose, onChanged }: SpecModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();
  const [spec, setSpec] = useState<Spec | null>(null);
  const [loading, setLoading] = useState(true);
  const [movingTo, setMovingTo] = useState<SpecStatus | null>(null);
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [expanded, setExpanded] = useState(false);
  const [showValidateModal, setShowValidateModal] = useState(false);
  const [validateResult, setValidateResult] = useState<{ success: boolean; error: string | null }>({ success: false, error: null });
  const [validating, setValidating] = useState(false);
  const [sprintSuggestions, setSprintSuggestions] = useState<any[] | null>(null);
  const [linkedSprints, setLinkedSprints] = useState<any[]>([]);

  // Build mentionables from board agents + owner
  const mentionables: Mentionable[] = [];
  if (currentBoard) {
    if (currentBoard.owner_id) {
      mentionables.push({ id: currentBoard.owner_id, name: 'Owner', type: 'user' });
    }
    for (const agent of currentBoard.agents) {
      mentionables.push({ id: agent.id, name: agent.name, type: 'agent' });
    }
  }

  const [parentIdeation, setParentIdeation] = useState<{ id: string; title: string; version: number } | null>(null);
  const [parentRefinement, setParentRefinement] = useState<{ id: string; title: string; version: number } | null>(null);
  const [viewingIdeationId, setViewingIdeationId] = useState<string | null>(null);
  const [viewingRefinementId, setViewingRefinementId] = useState<string | null>(null);

  useEffect(() => { loadSpec(); }, [specId]);

  const loadSpec = async () => {
    setLoading(true);
    try {
      const data = await api.getSpec(specId);
      setSpec(data);
      if (data.ideation_id) {
        try {
          const ideation = await api.getIdeation(data.ideation_id);
          setParentIdeation({ id: ideation.id, title: ideation.title, version: ideation.version });
        } catch { setParentIdeation(null); }
      } else { setParentIdeation(null); }
      if (data.refinement_id) {
        try {
          const refinement = await api.getRefinement(data.refinement_id);
          setParentRefinement({ id: refinement.id, title: refinement.title, version: refinement.version });
        } catch { setParentRefinement(null); }
      } else { setParentRefinement(null); }
      // Load linked sprints
      try {
        const sprints = await api.listSprints(data.board_id, data.id);
        setLinkedSprints(sprints);
      } catch { setLinkedSprints([]); }
    } catch { toast.error('Failed to load spec'); } finally { setLoading(false); }
  };

  const boardSettings = (currentBoard?.settings || {}) as BoardSettings;
  const requireSpecValidation = Boolean(boardSettings.require_spec_validation);
  const [showSubmitValidationModal, setShowSubmitValidationModal] = useState(false);

  const handleMoveSpec = async (status: SpecStatus) => {
    if (!spec) return;
    // Spec Validation Gate: when the board opts in, intercept approved→validated
    // to show the new SubmitSpecValidationModal. The modal calls the backend gate
    // which runs coverage checks and then computes outcome — on success the spec
    // is promoted to validated automatically, so we just refetch after.
    if (status === 'validated' && spec.status === 'approved' && requireSpecValidation) {
      setShowSubmitValidationModal(true);
      return;
    }
    // Legacy path (pre-gate): direct move_spec with coverage gate feedback in the
    // existing validate modal.
    if (status === 'validated' && spec.status === 'approved') {
      setShowValidateModal(true);
      setValidateResult({ success: false, error: null });
      setValidating(true);
      try {
        const updated = await api.moveSpec(specId, { status });
        setSpec(updated);
        onChanged();
        setValidateResult({ success: true, error: null });
        if (updated.cards && updated.cards.length >= 6) {
          try {
            const result = await api.suggestSprints(updated.board_id, specId);
            if (result.suggestions && result.suggestions.length > 1) {
              setSprintSuggestions(result.suggestions);
            }
          } catch {
            // Suggestion is optional, don't block on failure
          }
        }
      } catch (err: any) {
        setValidateResult({ success: false, error: err?.message || 'Validation failed' });
      } finally {
        setValidating(false);
      }
      return;
    }
    setMovingTo(status);
    try {
      const updated = await api.moveSpec(specId, { status });
      setSpec(updated);
      onChanged();
      toast.success(`Spec moved to ${SPEC_STATUS_LABELS[status]}`);
    } catch { toast.error('Failed to move spec'); } finally { setMovingTo(null); }
  };

  const handleDelete = async () => {
    if (!spec) return;
    if (!confirm(`Delete spec "${spec.title}"? Linked cards will be unlinked but not deleted.`)) return;
    try {
      await api.deleteSpec(specId);
      toast.success('Spec deleted');
      onChanged();
      onClose();
    } catch { toast.error('Failed to delete spec'); }
  };

  const getNextStatuses = (current: SpecStatus): SpecStatus[] => {
    // Spec Validation Gate adds direct approved→draft and validated→draft
    // transitions to unlock content editing in 1 click after a passed validation.
    const flow: Record<SpecStatus, SpecStatus[]> = {
      draft: ['review', 'cancelled'],
      review: ['approved', 'draft', 'cancelled'],
      approved: ['validated', 'review', 'draft', 'cancelled'],
      validated: ['in_progress', 'approved', 'draft', 'cancelled'],
      in_progress: ['done', 'validated', 'cancelled'],
      done: ['draft'],
      cancelled: ['draft'],
    };
    return (flow[current] || []).filter((s) => SPEC_STATUSES.includes(s));
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-xl p-8">
          <div className="text-gray-500 dark:text-gray-400">Loading spec...</div>
        </div>
      </div>
    );
  }

  if (!spec) return null;

  const nextStatuses = getNextStatuses(spec.status);

  const unansweredQA = spec.qa_items?.filter((q) => !q.answer).length || 0;
  const tabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'tests', label: 'Tests', icon: <FlaskConical size={14} />, count: spec.test_scenarios?.length || 0 },
    { id: 'rules', label: 'Rules', icon: <Scale size={14} />, count: spec.business_rules?.length || 0 },
    { id: 'contracts', label: 'Contracts', icon: <FileCode size={14} />, count: spec.api_contracts?.length || 0 },
    { id: 'trs', label: 'TRs', icon: <Settings size={14} />, count: spec.technical_requirements?.length || 0 },
    { id: 'mockups', label: 'Mockups', icon: <Monitor size={14} />, count: spec.screen_mockups?.length || 0 },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: spec.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'skills', label: 'Skills', icon: <Wrench size={14} />, count: spec.skills?.length || 0 },
    { id: 'knowledge', label: 'Knowledge', icon: <BookOpen size={14} />, count: spec.knowledge_bases?.length || 0 },
    { id: 'cards', label: 'Cards', icon: <Link2 size={14} />, count: spec.cards?.length || 0 },
    { id: 'sprints', label: 'Sprints', icon: <Layers size={14} />, count: linkedSprints.length },
    { id: 'validation', label: 'Validation', icon: <ShieldCheck size={14} /> },
    { id: 'kg', label: 'KG Graph', icon: <Network size={14} /> },
    { id: 'history', label: 'Activity', icon: <History size={14} /> },
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full ${expanded ? 'max-w-[95vw] h-[95vh]' : 'max-w-3xl h-[90vh]'} flex flex-col`}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[spec.status]}`}>
              {STATUS_ICON[spec.status]}
              {SPEC_STATUS_LABELS[spec.status]}
            </span>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{spec.title}</h2>
            <span className="text-xs text-gray-400 shrink-0">v{spec.version}</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => { const md = exportSpec(spec); downloadMarkdown(md, `spec_${slugify(spec.title)}_v${spec.version}.md`); }}
              disabled={loading}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-30"
              title="Download Markdown"
            >
              <Download size={16} />
            </button>
            <button onClick={loadSpec} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title="Refresh">
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <button onClick={() => setExpanded(!expanded)} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title={expanded ? 'Collapse' : 'Expand'}>
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Status flow */}
        {nextStatuses.length > 0 && (
          <div className="px-6 py-2.5 border-b border-gray-100 dark:border-gray-700/50 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 dark:text-gray-400">Move to:</span>
            {nextStatuses
              .filter((s) => !(s === 'validated' && spec.status === 'approved'))
              .map((status) => (
                <button
                  key={status}
                  onClick={() => handleMoveSpec(status)}
                  disabled={movingTo !== null}
                  className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-colors
                    ${STATUS_COLORS[status]} hover:ring-2 hover:ring-offset-1 hover:ring-gray-300 dark:hover:ring-gray-600
                    disabled:opacity-50`}
                >
                  <ChevronRight size={12} />
                  {SPEC_STATUS_LABELS[status]}
                  {movingTo === status && '...'}
                </button>
              ))}
          </div>
        )}

        {/* Provenance breadcrumb */}
        {(parentIdeation || parentRefinement) && (
          <div className="px-6 py-2 border-b border-gray-100 dark:border-gray-700/50 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            <span className="text-gray-400">From:</span>
            {parentIdeation && (
              <button
                onClick={() => setViewingIdeationId(parentIdeation.id)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300 hover:ring-2 hover:ring-amber-300 dark:hover:ring-amber-600 transition-all cursor-pointer"
              >
                <Lightbulb size={11} />
                {parentIdeation.title}
                <span className="text-[10px] text-amber-500 dark:text-amber-400">v{parentIdeation.version}</span>
              </button>
            )}
            {parentIdeation && parentRefinement && <ChevronRight size={12} className="text-gray-300" />}
            {parentRefinement && (
              <button
                onClick={() => setViewingRefinementId(parentRefinement.id)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-300 hover:ring-2 hover:ring-blue-300 dark:hover:ring-blue-600 transition-all cursor-pointer"
              >
                <Layers size={11} />
                {parentRefinement.title}
                <span className="text-[10px] text-blue-500 dark:text-blue-400">v{parentRefinement.version}</span>
              </button>
            )}
          </div>
        )}

        {/* Tabs */}
        <div className="flex items-center gap-1 px-6 pt-3 border-b border-gray-200 dark:border-gray-700 overflow-x-auto shrink-0 scrollbar-hide">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap shrink-0 ${
                activeTab === tab.id
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              }`}
            >
              {tab.icon}
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                  tab.highlight
                    ? 'bg-amber-200 text-amber-700 dark:bg-amber-800 dark:text-amber-300'
                    : 'bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300'
                }`}>
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {activeTab === 'details' && (
            <div className="space-y-5">
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Description</h4>
                <EditableField
                  value={spec.description || ''}
                  onSave={async (val) => {
                    const updated = await api.updateSpec(specId, { description: val });
                    setSpec(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No description"
                />
              </div>
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Context</h4>
                <EditableField
                  value={spec.context || ''}
                  onSave={async (val) => {
                    const updated = await api.updateSpec(specId, { context: val });
                    setSpec(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No context"
                />
              </div>
              <EditableRequirementsList
                title="Functional Requirements"
                icon={<Circle size={14} />}
                items={spec.functional_requirements}
                placeholder="Add a functional requirement..."
                onUpdate={async (items) => {
                  try {
                    const updated = await api.updateSpec(specId, { functional_requirements: items });
                    setSpec(updated);
                  } catch { toast.error('Failed to update'); }
                }}
              />
              <EditableRequirementsList
                title="Technical Requirements"
                icon={<Settings size={14} />}
                items={(spec.technical_requirements || []).map((tr) =>
                  typeof tr === 'string' ? tr : (tr as any).text || ''
                )}
                placeholder="Add a technical constraint..."
                onUpdate={async (items) => {
                  try {
                    const existingTRs = (spec.technical_requirements || []).map((tr) =>
                      typeof tr === 'string' ? { id: `tr_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, text: tr, linked_task_ids: null } : tr
                    );
                    const newTRs = items.map((text) => {
                      const existing = existingTRs.find((tr: any) => tr.text === text);
                      if (existing) return existing;
                      return { id: `tr_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, text, linked_task_ids: null };
                    });
                    const updated = await api.updateSpec(specId, { technical_requirements: newTRs as any });
                    setSpec(updated);
                  } catch { toast.error('Failed to update'); }
                }}
              />
              <EditableRequirementsList
                title="Acceptance Criteria"
                icon={<Target size={14} />}
                items={spec.acceptance_criteria}
                placeholder="Add an acceptance criterion..."
                onUpdate={async (items) => {
                  try {
                    const updated = await api.updateSpec(specId, { acceptance_criteria: items });
                    setSpec(updated);
                  } catch { toast.error('Failed to update'); }
                }}
              />
              {spec.labels && spec.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {spec.labels.map((label, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{label}</span>
                  ))}
                </div>
              )}

              {/* Validation Gate Override */}
              <ValidationGateOverride
                title="Validation Gate"
                requireValue={(spec as any).require_task_validation ?? null}
                minConfidence={(spec as any).validation_min_confidence ?? null}
                minCompleteness={(spec as any).validation_min_completeness ?? null}
                maxDrift={(spec as any).validation_max_drift ?? null}
                parentLabel="Board default"
                onUpdate={async (patch) => {
                  try {
                    const updated = await api.updateSpec(specId, patch as any);
                    setSpec(updated);
                  } catch { toast.error('Failed to update validation gate'); }
                }}
              />

              {/* Sprints summary — details in Sprints tab */}
              {linkedSprints.length > 0 && (
                <button
                  onClick={() => setActiveTab('sprints')}
                  className="text-xs text-indigo-600 dark:text-indigo-400 hover:underline flex items-center gap-1"
                >
                  <Layers size={12} />
                  {linkedSprints.length} sprint{linkedSprints.length > 1 ? 's' : ''} linked — view details
                </button>
              )}
            </div>
          )}

          {activeTab === 'tests' && spec && (
            <TestScenariosTab
              spec={spec}
              onUpdate={async (scenarios) => {
                try {
                  const updated = await api.updateSpec(specId, { test_scenarios: scenarios });
                  setSpec(updated);
                } catch { toast.error('Failed to update test scenarios'); }
              }}
              onSpecUpdate={async (data) => {
                try {
                  const updated = await api.updateSpec(specId, data as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
            />
          )}
          {activeTab === 'rules' && spec && (
            <RulesTab
              spec={spec}
              onUpdate={async (rules) => {
                try {
                  const updated = await api.updateSpec(specId, { business_rules: rules });
                  setSpec(updated);
                } catch { toast.error('Failed to update business rules'); }
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
            />
          )}
          {activeTab === 'contracts' && spec && (
            <ContractsTab
              spec={spec}
              onUpdate={async (contracts) => {
                try {
                  const updated = await api.updateSpec(specId, { api_contracts: contracts });
                  setSpec(updated);
                } catch { toast.error('Failed to update API contracts'); }
              }}
              onSpecUpdate={async (data) => {
                try {
                  const updated = await api.updateSpec(specId, data as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={async (contractId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'api_contracts', contractId, cardId);
                setSpec(updated);
              }}
              onUnlinkTask={async (contractId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'api_contracts', contractId, cardId);
                setSpec(updated);
              }}
            />
          )}
          {activeTab === 'trs' && spec && (
            <TechnicalRequirementsTab
              spec={spec}
              onUpdate={async (trs) => {
                try {
                  const updated = await api.updateSpec(specId, { technical_requirements: trs as any });
                  setSpec(updated);
                } catch { toast.error('Failed to update technical requirements'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={async (trId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'technical_requirements', trId, cardId);
                setSpec(updated);
              }}
              onUnlinkTask={async (trId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'technical_requirements', trId, cardId);
                setSpec(updated);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
            />
          )}
          {activeTab === 'mockups' && spec && (
            <MockupsTab
              screenMockups={spec.screen_mockups}
              expanded={expanded}
              onUpdate={async (mockups) => {
                const updated = await api.updateSpec(specId, { screen_mockups: mockups });
                setSpec(updated);
              }}
            />
          )}
          {activeTab === 'validation' && spec && (
            <div className="p-4 space-y-4">
              <SpecValidationHistoryPanel specId={specId} />
            </div>
          )}
          {activeTab === 'kg' && spec && (
            <KGValidationTab boardId={spec.board_id} specId={specId} />
          )}
          {activeTab === 'history' && <HistoryTab specId={specId} />}
          {activeTab === 'qa' && <QATab specId={specId} mentionables={mentionables} />}
          {activeTab === 'skills' && <SkillsTab specId={specId} />}
          {activeTab === 'knowledge' && <KnowledgeTab specId={specId} />}

          {activeTab === 'sprints' && (
            <SpecSprintsTab sprints={linkedSprints} api={api} />
          )}


          {activeTab === 'cards' && (
            <div className="space-y-2">
              {(!spec.cards || spec.cards.length === 0) ? (
                <div className="text-center py-6">
                  <Link2 size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                  <p className="text-sm text-gray-500 dark:text-gray-400">No linked cards</p>
                  <p className="text-xs text-gray-400 mt-1">Cards are created manually and linked to this spec</p>
                </div>
              ) : (
                spec.cards.map((card) => (
                  <div key={card.id} className="flex items-center justify-between py-1.5 px-2 rounded bg-gray-50 dark:bg-gray-700/50">
                    <span className="text-sm text-gray-700 dark:text-gray-300 truncate">{card.title}</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${CARD_STATUS_COLORS[card.status] || ''}`}>
                      {card.status.replace('_', ' ')}
                    </span>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete spec
          </button>
          <div className="flex items-center gap-2">
            {spec.status === 'approved' && (
              <button
                onClick={() => handleMoveSpec('validated' as SpecStatus)}
                disabled={validating}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold transition-colors
                  bg-purple-500 text-white hover:bg-purple-600 shadow-sm hover:shadow-md
                  disabled:opacity-50"
              >
                <CheckCircle2 size={16} />
                {validating ? 'Validating...' : 'Validate'}
              </button>
            )}
            {['validated', 'in_progress'].includes(spec.status) && (spec.cards?.length || 0) >= 4 && (
              <button
                onClick={async () => {
                  try {
                    const result = await api.suggestSprints(spec.board_id, specId);
                    if (result.suggestions?.length > 1) {
                      setSprintSuggestions(result.suggestions);
                    } else {
                      toast('Not enough tasks to split into sprints', { icon: 'ℹ️' });
                    }
                  } catch { toast.error('Failed to generate suggestions'); }
                }}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors
                  bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:bg-indigo-900/30 dark:text-indigo-300 dark:hover:bg-indigo-900/50"
              >
                <Layers size={14} />
                Suggest Sprints
              </button>
            )}
            <button onClick={onClose} className="btn btn-secondary">Close</button>
          </div>
        </div>
      </div>

      {/* Parent modals */}
      {viewingIdeationId && (
        <IdeationModal
          ideationId={viewingIdeationId}
          boardId={_boardId}
          onClose={() => setViewingIdeationId(null)}
          onChanged={loadSpec}
        />
      )}
      {viewingRefinementId && (
        <RefinementModal
          refinementId={viewingRefinementId}
          boardId={_boardId}
          onClose={() => setViewingRefinementId(null)}
          onChanged={loadSpec}
        />
      )}

      {/* Sprint Suggestion Modal */}
      {sprintSuggestions && spec && (
        <SprintSuggestionModal
          boardId={spec.board_id}
          specId={specId}
          suggestions={sprintSuggestions}
          onClose={() => setSprintSuggestions(null)}
          onSkip={() => setSprintSuggestions(null)}
          onCreated={() => { setSprintSuggestions(null); loadSpec(); }}
        />
      )}

      {/* Validation Gate Results Modal */}
      {showValidateModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]" onClick={() => !validating && setShowValidateModal(false)}>
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-lg p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              {validating ? (
                <>
                  <RefreshCw size={20} className="text-purple-500 animate-spin" />
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Running validation gates...</h3>
                </>
              ) : validateResult.success ? (
                <>
                  <CheckCircle2 size={20} className="text-green-500" />
                  <h3 className="text-lg font-semibold text-green-700 dark:text-green-400">Validation Passed</h3>
                </>
              ) : (
                <>
                  <Ban size={20} className="text-red-500" />
                  <h3 className="text-lg font-semibold text-red-700 dark:text-red-400">Validation Failed</h3>
                </>
              )}
            </div>

            {!validating && (
              <div className="space-y-3">
                {validateResult.success ? (
                  <p className="text-sm text-gray-600 dark:text-gray-400">
                    All coverage gates passed. Spec has been moved to <span className="font-semibold text-purple-600">Validated</span>.
                  </p>
                ) : (
                  <ValidationErrorDisplay error={validateResult.error || ''} />
                )}
                <div className="flex justify-end gap-2 pt-2">
                  <button
                    onClick={() => setShowValidateModal(false)}
                    className="btn btn-secondary text-sm"
                  >
                    Close
                  </button>
                  {!validateResult.success && (
                    <button
                      onClick={() => { setShowValidateModal(false); setActiveTab('tests'); }}
                      className="btn btn-primary text-sm"
                    >
                      Review Coverage
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Spec Validation Gate — submission modal (opens when board opts in and user clicks Validate) */}
      {showSubmitValidationModal && spec && (
        <SubmitSpecValidationModal
          specId={spec.id}
          specTitle={spec.title}
          settings={boardSettings}
          onClose={() => setShowSubmitValidationModal(false)}
          onSubmitted={async () => {
            setShowSubmitValidationModal(false);
            // Refetch the spec to reflect the new status and current_validation_id
            try {
              const updated = await api.getSpec(specId);
              setSpec(updated);
              onChanged();
            } catch {
              // Non-fatal; user can manually refresh
            }
          }}
        />
      )}
    </div>
  );
}
