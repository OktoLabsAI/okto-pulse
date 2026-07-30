/**
 * IdeationModal - View and manage an ideation, evaluate scope, derive specs
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  X,
  ChevronRight,
  Zap,
  CheckCircle2,
  Clock,
  Ban,
  FileText,
  Lightbulb,
  Sparkles,
  Plus,
  Trash2,
  ChevronDown,
  ChevronUp,
  MessageCircleQuestion,
  Send,
  History,
  ArrowRight,
  Layers,
  Gauge,
  Shield,
  Archive,
  Eye,
  RefreshCw,
  Maximize2,
  Minimize2,
  Download,
  GitBranch,
  FolderOpen,
  Link2,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { exportIdeation, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import { getErrorMessage } from '@/lib/getErrorMessage';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import { openLineageGraph } from '@/components/traceability';
import type {
  Ideation,
  IdeationStatus,
  IdeationQAItem,
  IdeationHistoryEntry,
  IdeationSnapshot,
  IdeationSnapshotSummary,
} from '@/types';
import {
  IDEATION_STATUSES,
  IDEATION_STATUS_LABELS,
  COMPLEXITY_LABELS,
} from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { CancellationDetails, CancellationReasonDialog } from '@/components/shared/CancellationReasonDialog';
import { ContextSelector, buildIdeationItems, compileSelectedContext, type SelectableItem } from '@/components/shared/ContextSelector';
import {
  DerivationPendingBadge,
  getIdeationPendingDerivationLabel,
} from '@/components/shared/DerivationPendingBadge';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { EditableField } from '@/components/shared/EditableField';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { AmbiguityGateSkipToggle } from '@/components/shared/AmbiguityGateSkipToggle';
import { ArchitectureTab } from '@/components/architecture';
import { ResourceGateDisclosure } from '@/components/resources/ResourceGateDisclosure';
import { KnowledgeWorkspace } from '@/components/resources/KnowledgeWorkspace';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import { QualityPanel } from '@/components/quality';
import {
  PolicyCompliancePanel,
  PolicyComplianceTransitionPreview,
  isAllowedTransitionActionable,
  policyTransitionRejectionMessage,
  readPolicyTransitionRejection,
  requirePolicyTransitionEnvelope,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
} from '@/components/policy-compliance';
import { IdeationReferencesPanel } from './IdeationReferencesPanel';

interface IdeationModalProps {
  ideationId: string;
  boardId: string;
  onClose: () => void;
  onEscape?: () => void;
  onChanged: () => void;
}

type ModalTab =
  | 'details'
  | 'resources'
  | 'qa'
  | 'evaluation'
  | 'references'
  | 'versions'
  | 'activity';
type ResourceSubTab = 'mockups' | 'knowledge' | 'architecture';
type EvaluationSubTab =
  | 'evaluation'
  | 'ambiguity'
  | 'policy-compliance';

const STATUS_ICON: Record<IdeationStatus, React.ReactNode> = {
  draft: <Lightbulb size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  evaluating: <Sparkles size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<IdeationStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  evaluating: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

const SCOPE_ASSESSMENT_LABELS = {
  domains: 'Domains',
  ambiguity: 'Ambiguity (complexity input)',
  dependencies: 'Dependencies',
} as const;

const COMPLEXITY_COLORS: Record<string, string> = {
  small: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  medium: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  large: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

/* ============================================================
   History Tab
   ============================================================ */

const ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  updated: 'Updated',
  status_changed: 'Status changed',
  evaluated: 'Evaluated',
  reviewed: 'Reviewed',
  spec_derived: 'Spec derived',
  qa_added: 'Question added',
  qa_answered: 'Question answered',
  refinement_created: 'Refinement created',
};

const ACTION_COLORS: Record<string, string> = {
  created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  status_changed: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  evaluated: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  spec_derived: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300',
};

function formatValue(val: unknown): string {
  if (val === null || val === undefined) return '(empty)';
  if (Array.isArray(val)) {
    if (val.length === 0) return '(empty list)';
    return val
      .map((v, i) => `${i + 1}. ${v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
      .join('\n');
  }
  if (typeof val === 'object') return JSON.stringify(val, null, 2);
  return String(val);
}

function VersionsTab({ ideationId }: { ideationId: string }) {
  const api = useDashboardApi();
  const [snapshots, setSnapshots] = useState<IdeationSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<IdeationSnapshot | null>(null);
  const [, setLoadingVersion] = useState(false);

  useEffect(() => { load(); }, [ideationId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listIdeationSnapshots(ideationId);
      setSnapshots(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const viewVersion = async (version: number) => {
    if (viewing?.version === version) { setViewing(null); return; }
    setLoadingVersion(true);
    try {
      const data = await api.getIdeationSnapshot(ideationId, version);
      setViewing(data);
    } catch { toast.error('Failed to load snapshot'); } finally { setLoadingVersion(false); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading versions...</div>;

  if (snapshots.length === 0) {
    return (
      <div className="text-center py-6">
        <Archive size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No versions yet</p>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">A snapshot is created each time the ideation is marked as "done"</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {snapshots.map((snap) => (
        <div key={snap.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div
            className="flex items-center justify-between px-3 py-2.5 bg-gray-50 dark:bg-gray-700/50 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={() => viewVersion(snap.version)}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-blue-600 dark:text-blue-400">v{snap.version}</span>
              <span className="text-sm text-gray-700 dark:text-gray-300">{snap.title}</span>
              {snap.complexity && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                  snap.complexity === 'large' ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' :
                  snap.complexity === 'medium' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' :
                  'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                }`}>{snap.complexity}</span>
              )}
            </div>
            <div className="flex items-center gap-2 text-xs text-gray-400">
              <span>{new Date(snap.created_at).toLocaleString()}</span>
              <Eye size={14} className={viewing?.version === snap.version ? 'text-blue-500' : ''} />
            </div>
          </div>

          {viewing?.version === snap.version && (
            <div className="px-4 py-3 border-t border-gray-100 dark:border-gray-700 space-y-3">
              {viewing.problem_statement && (
                <div>
                  <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Problem Statement</h5>
                  <MarkdownContent content={viewing.problem_statement} />
                </div>
              )}
              {viewing.proposed_approach && (
                <div>
                  <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Proposed Approach</h5>
                  <MarkdownContent content={viewing.proposed_approach} />
                </div>
              )}
              {viewing.description && (
                <div>
                  <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Description</h5>
                  <MarkdownContent content={viewing.description} />
                </div>
              )}
              {viewing.scope_assessment && (
                <div>
                  <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Scope Assessment</h5>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                    <span>Domains: <strong>{viewing.scope_assessment.domains}</strong>/5</span>
                    <span>Scope Ambiguity: <strong>{viewing.scope_assessment.ambiguity}</strong>/5</span>
                    <span>Dependencies: <strong>{viewing.scope_assessment.dependencies}</strong>/5</span>
                  </div>
                </div>
              )}
              {viewing.labels && viewing.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {viewing.labels.map((l, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{l}</span>
                  ))}
                </div>
              )}
              {viewing.qa_snapshot && viewing.qa_snapshot.length > 0 && (
                <div>
                  <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Q&A at this version</h5>
                  <div className="space-y-2">
                    {viewing.qa_snapshot.map((qa, i) => (
                      <div key={i} className="border-l-2 border-gray-300 dark:border-gray-600 pl-3">
                        <p className="text-sm text-gray-700 dark:text-gray-300"><strong>Q:</strong> {qa.question}</p>
                        {qa.answer && <p className="text-sm text-gray-600 dark:text-gray-400"><strong>A:</strong> {qa.answer}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function KnowledgeTab({
  ideationId,
  boardId,
  onResourcesChanged,
}: {
  ideationId: string;
  boardId: string;
  onResourcesChanged: () => void;
}) {
  const api = useDashboardApi();
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [adding, setAdding] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  const refreshWorkspace = () => {
    setRefreshGeneration((current) => current + 1);
  };

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    try {
      await api.createIdeationKnowledge(ideationId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        content: newContent.trim(),
      });
      toast.success('Knowledge added');
      setAdding(false);
      setNewTitle('');
      setNewDesc('');
      setNewContent('');
      refreshWorkspace();
      onResourcesChanged();
    } catch {
      toast.error('Failed to add knowledge');
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this knowledge base item?')) return false;
    try {
      await api.deleteIdeationKnowledge(ideationId, id);
      onResourcesChanged();
      return true;
    } catch {
      toast.error('Failed to delete knowledge');
      return false;
    }
  };

  return (
    <div className="space-y-3">
      <KnowledgeWorkspace
        boardId={boardId}
        entityType="ideation"
        entityId={ideationId}
        refreshKey={refreshGeneration}
        loadFallbackDetail={(id) => api.getIdeationKnowledge(ideationId, id)}
        onDelete={handleDelete}
      />
      {adding ? (
        <div className="border border-amber-200 dark:border-amber-700 rounded-lg p-3 space-y-2 bg-amber-50/50 dark:bg-amber-900/10">
          <input
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="Title"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
          />
          <input
            type="text"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="Description (optional)"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
          />
          <textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="Content..."
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
            rows={6}
          />
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button
              onClick={handleAdd}
              disabled={!newTitle.trim() || !newContent.trim()}
              className="btn btn-primary text-xs"
            >
              Add
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1 text-sm text-amber-600 dark:text-amber-400 hover:text-amber-800 dark:hover:text-amber-300"
        >
          <Plus size={14} /> Add Knowledge
        </button>
      )}
    </div>
  );
}

function HistoryTab({ ideationId }: { ideationId: string }) {
  const api = useDashboardApi();
  const [entries, setEntries] = useState<IdeationHistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => { load(); }, [ideationId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listIdeationHistory(ideationId);
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

function ChoiceOptionsDisplay({ choices, selected }: { choices: IdeationQAItem['choices']; selected: string[] | null }) {
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
  qa: IdeationQAItem;
  onAnswer: (qaId: string, answer: string | null, selected: string[] | null) => void;
  onCancel: () => void;
}) {
  const [sel, setSel] = useState<string[]>([]);
  const [freeText, setFreeText] = useState('');

  const toggleOption = (optId: string) => {
    // `single_choice` is an alias of `choice` — the service accepts both and
    // trims to the first selection anyway, but the UI must enforce single-
    // select behavior visually so the user doesn't see multiple highlights.
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

function QATab({ ideationId, mentionables }: { ideationId: string; mentionables: Mentionable[] }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<IdeationQAItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [answerDraft, setAnswerDraft] = useState('');

  // Ask question form
  const [askMode, setAskMode] = useState<'text' | 'choice'>('text');
  const [newQuestion, setNewQuestion] = useState('');
  const [newOptions, setNewOptions] = useState('');
  const [newMulti, setNewMulti] = useState(false);
  const [newAllowFreeText, setNewAllowFreeText] = useState(false);

  useEffect(() => { load(); }, [ideationId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listIdeationQA(ideationId);
      setItems(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAskText = async () => {
    if (!newQuestion.trim()) return;
    try {
      await api.createIdeationQuestion(ideationId, newQuestion.trim());
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
      await api.createIdeationChoiceQuestion(ideationId, {
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
      await api.answerIdeationQuestion(ideationId, qaId, answer || '', selected);
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
      await api.deleteIdeationQuestion(ideationId, qaId);
      await load();
    } catch { toast.error('Failed to delete'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading Q&A...</div>;

  const isAnswered = (qa: IdeationQAItem) => Boolean(qa.answered_at);
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
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Ask questions to clarify the ideation before evaluation begins</p>
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
   Scope Gauge
   ============================================================ */

function ScopeScoreRing({
  dimension,
  label,
  value,
  justification,
}: {
  dimension: keyof typeof SCOPE_ASSESSMENT_LABELS;
  label: string;
  value: number;
  justification?: string;
}) {
  const ringTone = dimension === 'ambiguity'
    ? (
        value <= 2
          ? 'border-emerald-400 text-emerald-700 dark:border-emerald-500 dark:text-emerald-300'
          : value === 3
            ? 'border-amber-400 text-amber-700 dark:border-amber-500 dark:text-amber-300'
            : 'border-red-400 text-red-700 dark:border-red-500 dark:text-red-300'
      )
    : dimension === 'domains'
      ? 'border-blue-400 text-blue-700 dark:border-blue-500 dark:text-blue-300'
      : 'border-violet-400 text-violet-700 dark:border-violet-500 dark:text-violet-300';

  return (
    <div className="flex min-w-0 flex-col items-center text-center">
      <div
        role="img"
        aria-label={`${label} score ${value} out of 5`}
        data-testid={`ideation-evaluation-score-${dimension}`}
        className={`flex h-20 w-20 items-center justify-center rounded-full border-4 ${ringTone}`}
      >
        <span aria-hidden="true" className="text-2xl font-bold leading-none">
          {value}
          <span className="ml-0.5 text-sm font-semibold text-gray-400">/5</span>
        </span>
      </div>
      <p className="mt-2 text-xs font-semibold text-gray-700 dark:text-gray-200">{label}</p>
      {justification && (
        <p className="mt-1 text-xs italic text-gray-500 dark:text-gray-400">
          {justification}
        </p>
      )}
    </div>
  );
}

/* ============================================================
   Main IdeationModal
   ============================================================ */

export function IdeationModal({ ideationId, boardId: _boardId, onClose, onEscape, onChanged }: IdeationModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();
  const perms = usePermissions(_boardId);
  const canReadQuality = perms.has('ideation.quality.read');
  const canAssessQuality = perms.has('ideation.quality.assess');
  const canProposeQualityQuestions = perms.has('ideation.qa.ask');
  const canReadPolicyCompliance = perms.has(
    'guidelines.compliance.read',
  );
  const ambiguityGateRequired = Boolean(
    currentBoard?.settings?.require_ideation_ambiguity_gate,
  );
  const canAccessAmbiguityAssessment = canReadQuality || ambiguityGateRequired;
  const [ideation, setIdeation] = useState<Ideation | null>(null);
  const [loading, setLoading] = useState(true);
  const [movingTo, setMovingTo] = useState<IdeationStatus | null>(null);
  const [nextStatuses, setNextStatuses] = useState<IdeationStatus[]>([]);
  const [
    policyTransitionPreview,
    setPolicyTransitionPreview,
  ] = useState<PolicyTransitionPreviewLoadState>({
    status: 'loading',
    transitions: [],
    error: null,
  });
  const [
    policyTransitionRejection,
    setPolicyTransitionRejection,
  ] = useState<PolicyTransitionRejection | null>(null);
  const lastTransitionSubjectKey = useRef<string | null>(null);
  const transitionRequestId = useRef(0);
  const [savingSkip, setSavingSkip] = useState(false);
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [resourceSubTab, setResourceSubTab] = useState<ResourceSubTab>('mockups');
  const [resourceGateRefreshKey, setResourceGateRefreshKey] = useState(0);
  const [evaluationSubTab, setEvaluationSubTab] = useState<EvaluationSubTab>('evaluation');
  const [expanded, setExpanded] = useState(false);
  const [derivingSpec, setDerivingSpec] = useState(false);
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);

  useEscapeToClose(onEscape ?? onClose);

  useEffect(() => {
    if (evaluationSubTab === 'ambiguity' && !canAccessAmbiguityAssessment) {
      setEvaluationSubTab('evaluation');
      return;
    }
    if (
      evaluationSubTab === 'policy-compliance'
      && !canReadPolicyCompliance
    ) {
      setEvaluationSubTab('evaluation');
    }
  }, [
    canAccessAmbiguityAssessment,
    canReadPolicyCompliance,
    evaluationSubTab,
  ]);

  // Evaluate form
  const [showEvalForm, setShowEvalForm] = useState(false);
  const [evalDomains, setEvalDomains] = useState(1);
  const [evalDomainsJust, setEvalDomainsJust] = useState('');
  const [evalAmbiguity, setEvalAmbiguity] = useState(1);
  const [evalAmbiguityJust, setEvalAmbiguityJust] = useState('');
  const [evalDependencies, setEvalDependencies] = useState(1);
  const [evalDependenciesJust, setEvalDependenciesJust] = useState('');
  const [evaluating, setEvaluating] = useState(false);

  const [creatingRefinement, setCreatingRefinement] = useState(false);

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

  useEffect(() => { loadIdeation(); }, [ideationId]);

  const loadAllowedTransitions = useCallback(async (data: Ideation) => {
    const requestId = transitionRequestId.current + 1;
    transitionRequestId.current = requestId;
    lastTransitionSubjectKey.current = [
      data.id,
      data.version,
      data.status,
    ].join(':');
    setPolicyTransitionRejection(null);
    setNextStatuses([]);
    setPolicyTransitionPreview({
      status: 'loading',
      transitions: [],
      error: null,
    });
    try {
      const response = await api.getAllowedTransitions(data.board_id || _boardId, {
        entity_type: 'ideation',
        entity_id: data.id,
      });
      if (transitionRequestId.current !== requestId) {
        return;
      }
      const transitions = requirePolicyTransitionEnvelope(response, {
        boardId: data.board_id || _boardId,
        entityType: 'ideation',
        subjectId: data.id,
        currentStatus: data.status,
      });
      setPolicyTransitionPreview({
        status: 'ready',
        transitions,
        error: null,
      });
      setNextStatuses(
        transitions
          .filter(isAllowedTransitionActionable)
          .map((item) => item.to_status)
          .filter((status): status is IdeationStatus => IDEATION_STATUSES.includes(status as IdeationStatus))
      );
    } catch (caught) {
      if (transitionRequestId.current !== requestId) {
        return;
      }
      setNextStatuses([]);
      setPolicyTransitionPreview({
        status: 'error',
        transitions: [],
        error: caught instanceof Error
          ? caught.message
          : 'The server transition contract could not be loaded.',
      });
    }
  }, [api, _boardId]);

  useEffect(() => {
    if (!ideation) {
      return;
    }
    const subjectKey = [
      ideation.id,
      ideation.version,
      ideation.status,
    ].join(':');
    if (lastTransitionSubjectKey.current === subjectKey) {
      return;
    }
    void loadAllowedTransitions(ideation);
  }, [ideation, loadAllowedTransitions]);

  const loadIdeation = async () => {
    setLoading(true);
    try {
      const data = await api.getIdeation(ideationId);
      setIdeation(data);
      await loadAllowedTransitions(data);
    } catch { toast.error('Failed to load ideation'); } finally { setLoading(false); }
  };

  const performMove = async (status: IdeationStatus, cancellationReason?: string) => {
    if (!ideation) return;
    setMovingTo(status);
    setPolicyTransitionRejection(null);
    try {
      const updated = await api.moveIdeation(ideationId, {
        status,
        ...(cancellationReason ? { cancellation_reason: cancellationReason } : {}),
      });
      setIdeation(updated);
      await loadAllowedTransitions(updated);
      onChanged();
      toast.success(`Ideation moved to ${IDEATION_STATUS_LABELS[status]}`);
    } catch (err) {
      const rejection = readPolicyTransitionRejection(err, {
        boardId: ideation.board_id || _boardId,
        entityType: 'ideation',
        subjectId: ideation.id,
        currentStatus: ideation.status,
        toStatus: status,
      });
      toast.error(
        rejection
          ? policyTransitionRejectionMessage(rejection)
          : getErrorMessage(err),
      );
      await loadAllowedTransitions(ideation);
      setPolicyTransitionRejection(rejection);
    } finally { setMovingTo(null); }
  };

  const handleMove = async (status: IdeationStatus) => {
    if (!ideation) return;
    // ITEM 17: cancelling requires a justification — intercept with the dialog.
    if (status === 'cancelled') {
      setCancelDialogOpen(true);
      return;
    }
    await performMove(status);
  };

  // Persist the per-ideation Max ambiguity gate skip via the dedicated endpoint
  // (spec 2485780b TR11) and refresh state from the response. Backend failures
  // surface through getErrorMessage/toast without a generic replacement.
  const handleToggleAmbiguitySkip = async (next: boolean) => {
    if (!ideation) return;
    setSavingSkip(true);
    try {
      const updated = await api.setIdeationAmbiguityGateSkip(ideationId, next);
      setIdeation(updated);
      await loadAllowedTransitions(updated);
      onChanged();
      toast.success(next ? 'Max ambiguity gate will be skipped for this ideation' : 'Max ambiguity gate re-enabled for this ideation');
    } catch (err) { toast.error(getErrorMessage(err)); } finally { setSavingSkip(false); }
  };

  const handleDelete = async () => {
    if (!ideation) return;
    if (!confirm(`Delete ideation "${ideation.title}"?`)) return;
    try {
      await api.deleteIdeation(ideationId);
      toast.success('Ideation deleted');
      onChanged();
      onClose();
    } catch { toast.error('Failed to delete ideation'); }
  };

  const handleEvaluate = async () => {
    setEvaluating(true);
    try {
      const updated = await api.evaluateIdeation(ideationId, {
        domains: evalDomains,
        domains_justification: evalDomainsJust.trim(),
        ambiguity: evalAmbiguity,
        ambiguity_justification: evalAmbiguityJust.trim(),
        dependencies: evalDependencies,
        dependencies_justification: evalDependenciesJust.trim(),
      });
      setIdeation(updated);
      await loadAllowedTransitions(updated);
      setShowEvalForm(false);
      onChanged();
      toast.success('Ideation evaluated');
    } catch { toast.error('Failed to evaluate ideation'); } finally { setEvaluating(false); }
  };

  const [selectorTarget, setSelectorTarget] = useState<'spec' | 'refinement' | null>(null);

  const handleSelectorConfirm = async (selectedItems: SelectableItem[], title: string) => {
    const compiledContext = compileSelectedContext(selectedItems);

    if (selectorTarget === 'spec') {
      setDerivingSpec(true);
      try {
        await api.createSpec(ideation!.board_id, {
          title,
          context: compiledContext,
          ideation_id: ideationId,
          labels: ideation!.labels || undefined,
        });
        toast.success('Spec draft created');
        await loadIdeation();
        onChanged();
      } catch (err) { toast.error(getErrorMessage(err)); } finally { setDerivingSpec(false); }
    } else if (selectorTarget === 'refinement') {
      setCreatingRefinement(true);
      try {
        await api.createRefinement(ideationId, {
          ideation_id: ideationId,
          title,
          description: compiledContext,
        });
        toast.success('Refinement created');
        await loadIdeation();
        onChanged();
      } catch { toast.error('Failed to create refinement'); } finally { setCreatingRefinement(false); }
    }
    setSelectorTarget(null);
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-xl p-8">
          <div className="text-gray-500 dark:text-gray-400">Loading ideation...</div>
        </div>
      </div>
    );
  }

  if (!ideation) return null;

  const canEvaluate = ideation.status === 'evaluating';
  const canDeriveSpec = ideation.status === 'done' && ideation.complexity === 'small';
  const needsRefinements = ideation.status === 'done' && ideation.complexity && ideation.complexity !== 'small';

  const unansweredQA = ideation.qa_items?.filter((q) => q.answered_at == null).length || 0;
  const resourceCount = (
    (ideation.screen_mockups?.length || 0)
    + (ideation.knowledge_bases?.length || 0)
    + (ideation.architecture_designs?.length || 0)
  );
  const referenceCount = (
    (ideation.stories?.length || 0)
    + (ideation.refinements?.length || 0)
    + (ideation.specs || []).filter((spec) => spec.refinement_id === null).length
  );
  const tabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'resources', label: 'Resources', icon: <FolderOpen size={14} />, count: resourceCount },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: ideation.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'evaluation', label: 'Evaluation', icon: <Gauge size={14} /> },
    { id: 'references', label: 'References', icon: <Link2 size={14} />, count: referenceCount },
    { id: 'versions', label: 'Versions', icon: <Archive size={14} /> },
    { id: 'activity', label: 'Activity', icon: <History size={14} /> },
  ];
  const resourceTabs: { id: ResourceSubTab; label: string; count: number }[] = [
    { id: 'mockups', label: 'Mockups', count: ideation.screen_mockups?.length || 0 },
    { id: 'knowledge', label: 'Knowledge', count: ideation.knowledge_bases?.length || 0 },
    { id: 'architecture', label: 'Architecture', count: ideation.architecture_designs?.length || 0 },
  ];
  const evaluationTabs: { id: EvaluationSubTab; label: string }[] = [
    { id: 'evaluation', label: 'Evaluation' },
    ...(canAccessAmbiguityAssessment
      ? [{ id: 'ambiguity' as const, label: 'Ambiguity Assessment' }]
      : []),
    ...(canReadPolicyCompliance
      ? [{ id: 'policy-compliance' as const, label: 'Policy Compliance' }]
      : []),
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full ${expanded ? 'max-w-[95vw] h-[95vh]' : 'max-w-3xl h-[90vh]'} flex flex-col overflow-hidden`}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[ideation.status]}`}>
              {STATUS_ICON[ideation.status]}
              {IDEATION_STATUS_LABELS[ideation.status]}
            </span>
            <DerivationPendingBadge label={getIdeationPendingDerivationLabel(ideation)} />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{ideation.title}</h2>
            <span className="text-xs text-gray-400 shrink-0">v{ideation.version}</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => openLineageGraph('ideation', ideation.id)}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title="Open lineage graph"
            >
              <GitBranch size={16} />
            </button>
            <button
              onClick={async () => {
                try {
                  // Hydrate architecture design summaries into full designs (entities,
                  // interfaces, diagram payloads) so the Markdown export renders the
                  // Mermaid diagram — same pattern as Spec/Card export.
                  const fullArchitecture = await Promise.all(
                    (ideation.architecture_designs || []).map((d) =>
                      api.getArchitectureDesign(d.id, true).catch(() => d)
                    )
                  );
                  const md = exportIdeation({ ...ideation, architecture_designs: fullArchitecture as any });
                  downloadMarkdown(md, `ideation_${slugify(ideation.title)}_v${ideation.version}.md`);
                } catch {
                  toast.error('Failed to prepare markdown export');
                }
              }}
              disabled={loading}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-30"
              title="Download Markdown"
            >
              <Download size={16} />
            </button>
            <button onClick={loadIdeation} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title="Refresh">
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
            {nextStatuses.map((status) => (
              <button
                key={status}
                onClick={() => handleMove(status)}
                disabled={movingTo !== null}
                className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-colors
                  ${STATUS_COLORS[status]} hover:ring-2 hover:ring-offset-1 hover:ring-gray-300 dark:hover:ring-gray-600
                  disabled:opacity-50`}
              >
                <ChevronRight size={12} />
                {IDEATION_STATUS_LABELS[status]}
                {movingTo === status && '...'}
              </button>
            ))}
          </div>
        )}

        {/* Tabs */}
        <div className="min-w-0">
          <AccessibleTabList
            idBase={`ideation-${ideationId}`}
            ariaLabel="Ideation sections"
            items={tabs.map((tab) => ({
              id: tab.id,
              label: tab.label,
              icon: tab.icon,
              count: tab.count,
              attention: tab.highlight,
            }))}
            value={activeTab}
            onValueChange={setActiveTab}
            className="px-6 pt-3"
          />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          <AccessibleTabPanel
            idBase={`ideation-${ideationId}`}
            tabId={activeTab}
            value={activeTab}
          >
          {activeTab === 'details' && (
            <div className="space-y-5">
              {ideation.status === 'cancelled' && (
                <CancellationDetails
                  id="cancellation-panel"
                  entityLabel="ideation"
                  reason={ideation.cancellation_reason}
                  cancelledBy={ideation.cancelled_by}
                  cancelledAt={ideation.cancelled_at}
                />
              )}
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Problem Statement</h4>
                <EditableField
                  value={ideation.problem_statement || ''}
                  onSave={async (val) => {
                    const updated = await api.updateIdeation(ideationId, { problem_statement: val });
                    setIdeation(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No problem statement"
                />
              </div>
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Proposed Approach</h4>
                <EditableField
                  value={ideation.proposed_approach || ''}
                  onSave={async (val) => {
                    const updated = await api.updateIdeation(ideationId, { proposed_approach: val });
                    setIdeation(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No proposed approach"
                />
              </div>
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Description</h4>
                <EditableField
                  value={ideation.description || ''}
                  onSave={async (val) => {
                    const updated = await api.updateIdeation(ideationId, { description: val });
                    setIdeation(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No description"
                />
              </div>

              {/* Labels */}
              {ideation.labels && ideation.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {ideation.labels.map((label, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{label}</span>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeTab === 'resources' && (
            <div className="space-y-4" data-testid="ideation-resources-panel">
              <ResourceGateDisclosure
                boardId={ideation.board_id || _boardId}
                entityType="ideation"
                entityId={ideationId}
                refreshKey={resourceGateRefreshKey}
              />
              <AccessibleTabList
                idBase={`ideation-${ideationId}-resources`}
                ariaLabel="Ideation resources"
                items={resourceTabs}
                value={resourceSubTab}
                onValueChange={setResourceSubTab}
                variant="secondary"
                className="max-w-full"
              />

              <AccessibleTabPanel
                idBase={`ideation-${ideationId}-resources`}
                tabId="mockups"
                value={resourceSubTab}
                mount="lazy-keep"
              >
                  <MockupsTab
                    screenMockups={ideation.screen_mockups}
                    boardId={ideation.board_id}
                    entityType="ideation"
                    entityId={ideationId}
                    expanded={expanded}
                    onUpdate={async (mockups) => {
                      const updated = await api.updateIdeation(ideationId, { screen_mockups: mockups });
                      setIdeation(updated);
                      setResourceGateRefreshKey((value) => value + 1);
                      await loadAllowedTransitions(updated);
                    }}
                  />
              </AccessibleTabPanel>
              <AccessibleTabPanel
                idBase={`ideation-${ideationId}-resources`}
                tabId="knowledge"
                value={resourceSubTab}
                mount="lazy-keep"
              >
                  <KnowledgeTab
                    ideationId={ideationId}
                    boardId={ideation.board_id}
                    onResourcesChanged={() => {
                      setResourceGateRefreshKey((value) => value + 1);
                      void loadIdeation();
                    }}
                  />
              </AccessibleTabPanel>
              <AccessibleTabPanel
                idBase={`ideation-${ideationId}-resources`}
                tabId="architecture"
                value={resourceSubTab}
                mount="lazy-keep"
              >
                  <ArchitectureTab
                    parentType="ideation"
                    parentId={ideationId}
                    boardId={ideation.board_id}
                    entityType="ideation"
                    entityId={ideationId}
                    expanded={expanded}
                    screenMockups={ideation.screen_mockups || []}
                    onChanged={(items) => {
                      setIdeation((current) => current
                        ? { ...current, architecture_designs: items }
                        : current);
                      setResourceGateRefreshKey((value) => value + 1);
                      void loadIdeation();
                    }}
                  />
              </AccessibleTabPanel>
            </div>
          )}

          {activeTab === 'evaluation' && (
            <div className="space-y-4" data-testid="ideation-evaluation-panel">
              <AccessibleTabList
                idBase={`ideation-${ideationId}-evaluation`}
                ariaLabel="Ideation evaluation"
                items={evaluationTabs}
                value={evaluationSubTab}
                onValueChange={setEvaluationSubTab}
                variant="secondary"
                className="max-w-full"
              />

              <AccessibleTabPanel
                idBase={`ideation-${ideationId}-evaluation`}
                tabId="evaluation"
                value={evaluationSubTab}
                mount="lazy-keep"
                className="space-y-5"
              >
                  <div>
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-800 dark:text-gray-100">
                          <Gauge size={15} /> Scope evaluation
                        </h3>
                        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                          These dimensions classify delivery complexity; they do not decide the ambiguity gate.
                        </p>
                      </div>
                      {ideation.complexity && (
                        <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${COMPLEXITY_COLORS[ideation.complexity]}`}>
                          Complexity: {COMPLEXITY_LABELS[ideation.complexity]}
                        </span>
                      )}
                    </div>

                    {ideation.scope_assessment ? (
                      <div className="mt-5 grid gap-5 sm:grid-cols-3">
                        {(['domains', 'ambiguity', 'dependencies'] as const).map((dimension) => {
                          const assessment = ideation.scope_assessment as Record<string, unknown>;
                          return (
                            <ScopeScoreRing
                              key={dimension}
                              dimension={dimension}
                              label={SCOPE_ASSESSMENT_LABELS[dimension]}
                              value={(assessment[dimension] as number) || 0}
                              justification={(assessment[`${dimension}_justification`] as string) || undefined}
                            />
                          );
                        })}
                      </div>
                    ) : (
                      <p className="mt-4 rounded-lg border border-dashed border-gray-300 p-4 text-center text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
                        This ideation has not been evaluated yet.
                      </p>
                    )}
                  </div>

                  <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-3 text-xs text-blue-800 dark:border-blue-800/50 dark:bg-blue-950/20 dark:text-blue-200">
                    <strong>Two distinct ambiguity signals:</strong> the value above is only an input to
                    complexity. The governed score and its transition decision live in Ambiguity Assessment.
                  </div>

                  {showEvalForm && canEvaluate && (
                    <section className="rounded-xl border border-amber-200 bg-amber-50/50 p-4 dark:border-amber-700/50 dark:bg-amber-900/10">
                      <h4 className="mb-3 text-sm font-semibold text-gray-700 dark:text-gray-300">Evaluate Scope</h4>
                      <div className="mb-3 space-y-4">
                        {([
                          { label: 'Domains', sublabel: 'How many systems/services are impacted?', value: evalDomains, setValue: setEvalDomains, just: evalDomainsJust, setJust: setEvalDomainsJust },
                          { label: 'Ambiguity (complexity input)', sublabel: 'How much does uncertainty increase delivery complexity?', value: evalAmbiguity, setValue: setEvalAmbiguity, just: evalAmbiguityJust, setJust: setEvalAmbiguityJust },
                          { label: 'Dependencies', sublabel: 'How many external dependencies?', value: evalDependencies, setValue: setEvalDependencies, just: evalDependenciesJust, setJust: setEvalDependenciesJust },
                        ] as const).map((dimension) => (
                          <div key={dimension.label} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                            <div className="mb-1 flex items-center justify-between">
                              <label className="text-xs font-semibold text-gray-700 dark:text-gray-300">{dimension.label}</label>
                              <span className="text-sm font-bold text-blue-700 dark:text-blue-300">{dimension.value}/5</span>
                            </div>
                            <p className="mb-2 text-[10px] text-gray-400">{dimension.sublabel}</p>
                            <input
                              type="range"
                              min={1}
                              max={5}
                              step={1}
                              value={dimension.value}
                              onChange={(event) => dimension.setValue(Number(event.target.value))}
                              className="h-2 w-full cursor-pointer appearance-none rounded-lg bg-gray-200 accent-blue-600 dark:bg-gray-600"
                            />
                            <div className="mt-0.5 flex justify-between px-0.5 text-[9px] text-gray-400">
                              <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span>
                            </div>
                            <textarea
                              value={dimension.just}
                              onChange={(event) => dimension.setJust(event.target.value)}
                              placeholder={`Justification: why ${dimension.label.toLowerCase()} = ${dimension.value}?`}
                              className="mt-2 w-full resize-none rounded-lg border border-gray-300 px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-700"
                              rows={2}
                            />
                          </div>
                        ))}
                      </div>
                      <div className="flex justify-end gap-2">
                        <button onClick={() => setShowEvalForm(false)} className="btn btn-secondary text-xs">Cancel</button>
                        <button
                          onClick={handleEvaluate}
                          disabled={evaluating || !evalDomainsJust.trim() || !evalAmbiguityJust.trim() || !evalDependenciesJust.trim()}
                          className="btn btn-primary text-xs"
                        >
                          {evaluating ? 'Evaluating...' : 'Submit Evaluation'}
                        </button>
                      </div>
                      {(!evalDomainsJust.trim() || !evalAmbiguityJust.trim() || !evalDependenciesJust.trim()) && (
                        <p className="mt-1 text-right text-[10px] text-amber-600 dark:text-amber-400">All justifications are required</p>
                      )}
                    </section>
                  )}
              </AccessibleTabPanel>

              {canAccessAmbiguityAssessment && (
                <AccessibleTabPanel
                  idBase={`ideation-${ideationId}-evaluation`}
                  tabId="ambiguity"
                  value={evaluationSubTab}
                  mount="lazy-keep"
                  className="space-y-4"
                >
                  {ambiguityGateRequired ? (
                    <section
                      className="space-y-4"
                      data-testid="ambiguity-gate-panel"
                    >
                      <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-800 dark:text-gray-100">
                        <Shield size={15} /> Ambiguity assessment and gate
                      </h3>
                      {canReadQuality ? (
                        <QualityPanel
                          key={`ideation-quality-${ideation.skip_ambiguity_gate ? 'skipped' : 'active'}`}
                          subjectType="ideation"
                          subjectId={ideationId}
                          subjectVersion={ideation.version}
                          subjectStatus={ideation.status}
                          subjectArchived={ideation.archived ?? false}
                          canRead={canReadQuality}
                          canAssess={canAssessQuality}
                          canProposeQuestions={canProposeQualityQuestions}
                          onAssessmentRecorded={() => {
                            void loadIdeation();
                            onChanged();
                          }}
                        />
                      ) : (
                        <p className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
                          The assessment and server gate preview are omitted because Quality read permission is not available.
                        </p>
                      )}
                      <AmbiguityGateSkipToggle
                        subjectLabel="ideation"
                        checked={ideation.skip_ambiguity_gate ?? false}
                        disabled={savingSkip}
                        onCheckedChange={(checked) => {
                          void handleToggleAmbiguitySkip(checked);
                        }}
                      />
                    </section>
                  ) : canReadQuality ? (
                    <QualityPanel
                      subjectType="ideation"
                      subjectId={ideationId}
                      subjectVersion={ideation.version}
                      subjectStatus={ideation.status}
                      subjectArchived={ideation.archived ?? false}
                      canRead={canReadQuality}
                      canAssess={canAssessQuality}
                      canProposeQuestions={canProposeQualityQuestions}
                      onAssessmentRecorded={() => {
                        void loadIdeation();
                        onChanged();
                      }}
                    />
                  ) : null}
                </AccessibleTabPanel>
              )}

              {canReadPolicyCompliance && (
                <AccessibleTabPanel
                  idBase={`ideation-${ideationId}-evaluation`}
                  tabId="policy-compliance"
                  value={evaluationSubTab}
                  mount="lazy-keep"
                  className="space-y-4"
                >
                  <PolicyComplianceTransitionPreview
                    preview={policyTransitionPreview}
                    rejection={policyTransitionRejection}
                  />
                  <PolicyCompliancePanel
                    boardId={ideation.board_id || _boardId}
                    entityType="ideation"
                    subjectId={ideation.id}
                    refreshKey={ideation.version}
                    onEvaluated={() => {
                      void loadAllowedTransitions(ideation);
                    }}
                    onRefreshed={() => {
                      void loadAllowedTransitions(ideation);
                    }}
                  />
                </AccessibleTabPanel>
              )}
            </div>
          )}

          {activeTab === 'qa' && <QATab ideationId={ideationId} mentionables={mentionables} />}
          {activeTab === 'references' && <IdeationReferencesPanel ideation={ideation} />}
          {activeTab === 'versions' && <VersionsTab ideationId={ideationId} />}
          {activeTab === 'activity' && <HistoryTab ideationId={ideationId} />}
          </AccessibleTabPanel>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete ideation
          </button>
          <div className="flex gap-2">
            {canEvaluate && (
              <button
                onClick={() => {
                  setActiveTab('evaluation');
                  setEvaluationSubTab('evaluation');
                  setShowEvalForm(true);
                }}
                className="btn btn-secondary flex items-center gap-1.5"
              >
                <Gauge size={16} />
                Evaluate
              </button>
            )}
            {canDeriveSpec && (
              <button
                onClick={() => setSelectorTarget('spec')}
                disabled={derivingSpec}
                className="btn btn-primary flex items-center gap-1.5"
              >
                <Zap size={16} />
                {derivingSpec ? 'Creating...' : 'Create Spec Draft'}
              </button>
            )}
            {needsRefinements && (
              <button
                onClick={() => setSelectorTarget('refinement')}
                disabled={creatingRefinement}
                className="btn btn-primary flex items-center gap-1.5"
              >
                <Layers size={16} />
                {creatingRefinement ? 'Creating...' : 'Create Refinement'}
              </button>
            )}
            <button onClick={onClose} className="btn btn-secondary">Close</button>
          </div>
        </div>
      </div>

      {/* Context selector for derivation */}
      {selectorTarget && ideation && (
        <ContextSelector
          title={ideation.title}
          description={
            selectorTarget === 'spec'
              ? 'Select which parts of the ideation to include in the spec draft context'
              : 'Select which parts of the ideation to include in the refinement'
          }
          items={buildIdeationItems(ideation)}
          targetLabel={selectorTarget === 'spec' ? 'Spec Draft' : 'Refinement'}
          onConfirm={handleSelectorConfirm}
          onCancel={() => setSelectorTarget(null)}
        />
      )}

      {/* Cancellation justification (ITEM 17) */}
      <CancellationReasonDialog
        open={cancelDialogOpen}
        entityLabel="ideation"
        submitting={movingTo === 'cancelled'}
        onConfirm={async (reason) => {
          setCancelDialogOpen(false);
          await performMove('cancelled', reason);
        }}
        onCancel={() => setCancelDialogOpen(false)}
      />
    </div>
  );
}
