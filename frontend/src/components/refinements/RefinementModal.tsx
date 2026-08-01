/**
 * RefinementModal - View and manage a refinement, derive specs
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
  Plus,
  Zap,
  CheckCircle2,
  Clock,
  Ban,
  FileText,
  Layers,
  Send,
  Trash2,
  ChevronDown,
  ChevronUp,
  MessageCircleQuestion,
  History,
  ArrowRight,
  Link2,
  Lightbulb,
  Archive,
  Eye,
  RefreshCw,
  Maximize2,
  Minimize2,
  Download,
  GitBranch,
  Shield,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { v4 as uuidv4 } from 'uuid';
import { exportRefinement, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import { getErrorMessage } from '@/lib/getErrorMessage';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import { openLineageGraph } from '@/components/traceability';
import type { Refinement, RefinementStatus, RefinementQAItem, RefinementHistoryEntry, RefinementSnapshot, RefinementSnapshotSummary } from '@/types';
import { REFINEMENT_STATUSES, REFINEMENT_STATUS_LABELS } from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { CancellationDetails, CancellationReasonDialog } from '@/components/shared/CancellationReasonDialog';
import { ContextSelector, buildRefinementItems, type SelectableItem } from '@/components/shared/ContextSelector';
import {
  buildKnowledgePropagationEnvelope,
  type KnowledgePropagationChoice,
} from '@/components/shared/knowledgePropagationChoice';
import {
  effectiveKnowledgeCandidate,
  mergeKnowledgePropagationCandidates,
  physicalKnowledgeCandidate,
  type KnowledgePropagationCandidate,
} from '@/components/shared/knowledgePropagationCandidates';
import {
  DerivationPendingBadge,
  getRefinementPendingDerivationLabel,
} from '@/components/shared/DerivationPendingBadge';
import { EditableField } from '@/components/shared/EditableField';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { AmbiguityGateSkipToggle } from '@/components/shared/AmbiguityGateSkipToggle';
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
import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import type { RefinementModalTab } from '@/components/shared/tabRouting';
import { ResearchDecisionTab } from './ResearchDecisionPanel';
import { RefinementResourcesPanel } from './RefinementResourcesPanel';
import {
  RefinementReferencesPanel,
  RefinementToSummary,
  type RefinementReferenceTab,
} from './RefinementReferencesPanel';

interface RefinementModalProps {
  refinementId: string;
  boardId: string;
  onClose: () => void;
  onEscape?: () => void;
  onChanged: () => void;
}

type ModalTab = RefinementModalTab;
type ValidationSubTab = 'ambiguity' | 'policy-compliance';

const STATUS_ICON: Record<RefinementStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<RefinementStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

/* ============================================================
   History Tab
   ============================================================ */

const ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  updated: 'Updated',
  status_changed: 'Status changed',
  spec_derived: 'Spec derived',
  qa_added: 'Question added',
  qa_answered: 'Question answered',
};

const ACTION_COLORS: Record<string, string> = {
  created: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  updated: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  status_changed: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  spec_derived: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
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

function EditableList({ title, items, placeholder, colorClass, onUpdate }: {
  title: string;
  items: string[] | null;
  placeholder: string;
  colorClass: string;
  onUpdate: (items: string[]) => void;
}) {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const hasItems = items && items.length > 0;

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed) { onUpdate([...(items || []), trimmed]); setDraft(''); }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className={`text-sm font-semibold flex items-center gap-1.5 ${colorClass}`}>
          {title} {hasItems && <span className="text-xs font-normal text-gray-400">({items.length})</span>}
        </h4>
        {!editing && (
          <button onClick={() => setEditing(true)} className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-0.5">
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
              <button onClick={() => onUpdate(items.filter((_, idx) => idx !== i))} className="opacity-0 group-hover:opacity-100 p-0.5 text-red-400 hover:text-red-600 transition-opacity">
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-xs text-gray-400 dark:text-gray-500 italic ml-1">No {title.toLowerCase()} defined yet</p>
      )}
      {editing && (
        <div className="flex gap-2 mt-2">
          <input type="text" value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') add();
              if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setEditing(false);
                setDraft('');
              }
            }}
            placeholder={placeholder}
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" autoFocus
          />
          <button onClick={add} disabled={!draft.trim()} className="btn btn-primary text-xs">Add</button>
          <button onClick={() => { setEditing(false); setDraft(''); }} className="btn btn-secondary text-xs">Done</button>
        </div>
      )}
    </div>
  );
}

function VersionsTab({ refinementId }: { refinementId: string }) {
  const api = useDashboardApi();
  const [snapshots, setSnapshots] = useState<RefinementSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<RefinementSnapshot | null>(null);

  useEffect(() => { load(); }, [refinementId]);

  const load = async () => {
    setLoading(true);
    try { setSnapshots(await api.listRefinementSnapshots(refinementId)); } catch { /* */ } finally { setLoading(false); }
  };

  const viewVersion = async (version: number) => {
    if (viewing?.version === version) { setViewing(null); return; }
    try { setViewing(await api.getRefinementSnapshot(refinementId, version)); } catch { toast.error('Failed to load snapshot'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading versions...</div>;

  if (snapshots.length === 0) {
    return (
      <div className="text-center py-6">
        <Archive size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No versions yet</p>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">A snapshot is created each time the refinement is marked as "done"</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {snapshots.map((snap) => (
        <div key={snap.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div className="flex items-center justify-between px-3 py-2.5 bg-gray-50 dark:bg-gray-700/50 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700" onClick={() => viewVersion(snap.version)}>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-blue-600 dark:text-blue-400">v{snap.version}</span>
              <span className="text-sm text-gray-700 dark:text-gray-300">{snap.title}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-gray-400">
              <span>{new Date(snap.created_at).toLocaleString()}</span>
              <Eye size={14} className={viewing?.version === snap.version ? 'text-blue-500' : ''} />
            </div>
          </div>
          {viewing?.version === snap.version && (
            <div className="px-4 py-3 border-t border-gray-100 dark:border-gray-700 space-y-3">
              {viewing.in_scope && viewing.in_scope.length > 0 && (
                <div><h5 className="text-xs font-semibold text-green-600 uppercase tracking-wide mb-1">In Scope</h5>
                  <ol className="space-y-1 ml-1">{viewing.in_scope.map((s, i) => <li key={i} className="text-sm text-gray-600 dark:text-gray-400">{i+1}. {s}</li>)}</ol>
                </div>
              )}
              {viewing.out_of_scope && viewing.out_of_scope.length > 0 && (
                <div><h5 className="text-xs font-semibold text-red-600 uppercase tracking-wide mb-1">Out of Scope</h5>
                  <ol className="space-y-1 ml-1">{viewing.out_of_scope.map((s, i) => <li key={i} className="text-sm text-gray-600 dark:text-gray-400">{i+1}. {s}</li>)}</ol>
                </div>
              )}
              {viewing.analysis && <div><h5 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Analysis</h5><MarkdownContent content={viewing.analysis} /></div>}
              {viewing.decisions && viewing.decisions.length > 0 && (
                <div><h5 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Decisions</h5>
                  <ol className="space-y-1 ml-1">{viewing.decisions.map((d, i) => <li key={i} className="text-sm text-gray-600 dark:text-gray-400">{i+1}. {d}</li>)}</ol>
                </div>
              )}
              {viewing.qa_snapshot && viewing.qa_snapshot.length > 0 && (
                <div><h5 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Q&A at this version</h5>
                  <div className="space-y-2">{viewing.qa_snapshot.map((qa, i) => (
                    <div key={i} className="border-l-2 border-gray-300 dark:border-gray-600 pl-3">
                      <p className="text-sm text-gray-700 dark:text-gray-300"><strong>Q:</strong> {qa.question}</p>
                      {qa.answer && <p className="text-sm text-gray-600 dark:text-gray-400"><strong>A:</strong> {qa.answer}</p>}
                    </div>
                  ))}</div>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function HistoryTab({ refinementId }: { refinementId: string }) {
  const api = useDashboardApi();
  const [entries, setEntries] = useState<RefinementHistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => { load(); }, [refinementId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listRefinementHistory(refinementId);
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

function ChoiceOptionsDisplay({ choices, selected }: { choices: RefinementQAItem['choices']; selected: string[] | null }) {
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
  qa: RefinementQAItem;
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

function QATab({ refinementId, mentionables }: { refinementId: string; mentionables: Mentionable[] }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<RefinementQAItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [answerDraft, setAnswerDraft] = useState('');

  // Ask question form
  const [askMode, setAskMode] = useState<'text' | 'choice'>('text');
  const [newQuestion, setNewQuestion] = useState('');
  const [newOptions, setNewOptions] = useState('');
  const [newMulti, setNewMulti] = useState(false);
  const [newAllowFreeText, setNewAllowFreeText] = useState(false);

  useEffect(() => { load(); }, [refinementId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listRefinementQA(refinementId);
      setItems(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAskText = async () => {
    if (!newQuestion.trim()) return;
    try {
      await api.createRefinementQuestion(refinementId, newQuestion.trim());
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
      await api.createRefinementChoiceQuestion(refinementId, {
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
      await api.answerRefinementQuestion(refinementId, qaId, answer || '', selected);
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
      await api.deleteRefinementQuestion(refinementId, qaId);
      await load();
    } catch { toast.error('Failed to delete'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading Q&A...</div>;

  const isAnswered = (qa: RefinementQAItem) => Boolean(qa.answered_at);
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
            <input type="text" value={newOptions} onChange={(e) => setNewOptions(e.target.value)} placeholder="Options (comma-separated): Option A, Option B, Both" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
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
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Ask questions to clarify refinement details before proceeding</p>
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
   Main RefinementModal
   ============================================================ */

export function RefinementModal({ refinementId, boardId: _boardId, onClose, onEscape, onChanged }: RefinementModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();
  const modalStack = useOptionalModalStack();
  const perms = usePermissions(_boardId);
  const canReadQuality = perms.has('refinement.quality.read');
  const canAssessQuality = perms.has('refinement.quality.assess');
  const canProposeQualityQuestions = perms.has('refinement.qa.ask');
  const canReadResearchDecisions = perms.has('refinement.research_decisions.read');
  const canReadPolicyCompliance = perms.has(
    'guidelines.assessments.read',
  );
  const requiresAmbiguityGate =
    currentBoard?.settings?.require_refinement_ambiguity_gate ?? false;
  const canAccessAmbiguityAssessment =
    canReadQuality || requiresAmbiguityGate;
  const canViewValidation =
    canAccessAmbiguityAssessment || canReadPolicyCompliance;
  const [refinement, setRefinement] = useState<Refinement | null>(null);
  const [loading, setLoading] = useState(true);
  const [derivingSpec, setDerivingSpec] = useState(false);
  const [movingTo, setMovingTo] = useState<RefinementStatus | null>(null);
  const [nextStatuses, setNextStatuses] = useState<RefinementStatus[]>([]);
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
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [validationSubTab, setValidationSubTab] =
    useState<ValidationSubTab>('ambiguity');
  const [referenceTab, setReferenceTab] =
    useState<RefinementReferenceTab>('ideation');
  const [expanded, setExpanded] = useState(false);
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);
  const [savingAmbiguitySkip, setSavingAmbiguitySkip] = useState(false);

  useEscapeToClose(onEscape ?? onClose);

  useEffect(() => {
    if (
      (activeTab === 'validation' && !canViewValidation)
      || (activeTab === 'research-decisions' && !canReadResearchDecisions)
    ) {
      setActiveTab('details');
    }
  }, [
    activeTab,
    canReadResearchDecisions,
    canViewValidation,
  ]);

  useEffect(() => {
    if (!canViewValidation) {
      return;
    }
    if (
      validationSubTab === 'ambiguity'
      && !canAccessAmbiguityAssessment
    ) {
      setValidationSubTab('policy-compliance');
      return;
    }
    if (
      validationSubTab === 'policy-compliance'
      && !canReadPolicyCompliance
    ) {
      setValidationSubTab('ambiguity');
    }
  }, [
    canAccessAmbiguityAssessment,
    canReadPolicyCompliance,
    canViewValidation,
    validationSubTab,
  ]);

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

  useEffect(() => {
    setReferenceTab('ideation');
    loadRefinement();
  }, [refinementId]);

  const loadAllowedTransitions = useCallback(async (data: Refinement) => {
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
        entity_type: 'refinement',
        entity_id: data.id,
      });
      if (transitionRequestId.current !== requestId) {
        return;
      }
      const transitions = requirePolicyTransitionEnvelope(response, {
        boardId: data.board_id || _boardId,
        entityType: 'refinement',
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
          .filter((status): status is RefinementStatus => REFINEMENT_STATUSES.includes(status as RefinementStatus))
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
    if (!refinement) {
      return;
    }
    const subjectKey = [
      refinement.id,
      refinement.version,
      refinement.status,
    ].join(':');
    if (lastTransitionSubjectKey.current === subjectKey) {
      return;
    }
    void loadAllowedTransitions(refinement);
  }, [loadAllowedTransitions, refinement]);

  const loadRefinement = async () => {
    setLoading(true);
    try {
      const data = await api.getRefinement(refinementId);
      setRefinement(data);
      await loadAllowedTransitions(data);
      if (data.ideation_id) {
        try {
          const ideation = await api.getIdeation(data.ideation_id);
          setParentIdeation({ id: ideation.id, title: ideation.title, version: ideation.version });
        } catch { setParentIdeation(null); }
      } else { setParentIdeation(null); }
    } catch { toast.error('Failed to load refinement'); } finally { setLoading(false); }
  };

  const performMove = async (status: RefinementStatus, cancellationReason?: string) => {
    if (!refinement) return;
    setMovingTo(status);
    setPolicyTransitionRejection(null);
    try {
      const updated = await api.moveRefinement(refinementId, {
        status,
        ...(cancellationReason ? { cancellation_reason: cancellationReason } : {}),
      });
      setRefinement(updated);
      await loadAllowedTransitions(updated);
      onChanged();
      toast.success(`Refinement moved to ${REFINEMENT_STATUS_LABELS[status]}`);
    } catch (err) {
      const rejection = readPolicyTransitionRejection(err, {
        boardId: refinement.board_id || _boardId,
        entityType: 'refinement',
        subjectId: refinement.id,
        currentStatus: refinement.status,
        toStatus: status,
      });
      toast.error(
        rejection
          ? policyTransitionRejectionMessage(rejection)
          : getErrorMessage(err),
      );
      await loadAllowedTransitions(refinement);
      setPolicyTransitionRejection(rejection);
    } finally { setMovingTo(null); }
  };

  const handleMove = async (status: RefinementStatus) => {
    if (!refinement) return;
    // ITEM 17: cancelling requires a justification — intercept with the dialog.
    if (status === 'cancelled') {
      setCancelDialogOpen(true);
      return;
    }
    await performMove(status);
  };

  const handleToggleAmbiguitySkip = async (skip: boolean) => {
    if (!refinement) return;
    setSavingAmbiguitySkip(true);
    try {
      const receipt = await api.setRefinementAmbiguityGateSkip(refinementId, {
        skip_ambiguity_gate: skip,
        reason: skip
          ? 'Max ambiguity gate skipped from the refinement UI.'
          : 'Max ambiguity gate re-enabled from the refinement UI.',
        expected_refinement_version: refinement.version,
      });
      const updated: Refinement = {
        ...refinement,
        skip_ambiguity_gate: receipt.skipped,
        version: receipt.version,
      };
      setRefinement(updated);
      await loadAllowedTransitions(updated);
      onChanged();
      toast.success(receipt.skipped
        ? 'Max ambiguity gate will be skipped for this refinement'
        : 'Max ambiguity gate re-enabled for this refinement');
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setSavingAmbiguitySkip(false);
    }
  };

  const [showSpecSelector, setShowSpecSelector] = useState(false);
  const [deriveIdempotencyKey, setDeriveIdempotencyKey] = useState<string>(
    () => uuidv4(),
  );
  const lastSubmittedDeriveIntentRef = useRef<string | null>(null);
  const [deriveKnowledgeItems, setDeriveKnowledgeItems] = useState<
    KnowledgePropagationCandidate[]
  >([]);
  const [deriveKnowledgeLoading, setDeriveKnowledgeLoading] = useState(false);
  const [deriveKnowledgeError, setDeriveKnowledgeError] = useState<string | null>(
    null,
  );
  const [deriveKnowledgeReload, setDeriveKnowledgeReload] = useState(0);

  useEffect(() => {
    if (!showSpecSelector || !refinement) return;
    let cancelled = false;
    setDeriveKnowledgeLoading(true);
    setDeriveKnowledgeError(null);
    const direct = (refinement.knowledge_bases || []).map(
      physicalKnowledgeCandidate,
    );
    api.getEffectiveResources(
      refinement.board_id,
      'refinement',
      refinement.id,
    ).then((response) => {
      if (cancelled) return;
      const effective = (response.resources.knowledge_base || [])
        .map(effectiveKnowledgeCandidate)
        .filter((item): item is KnowledgePropagationCandidate => item !== null);
      setDeriveKnowledgeItems(
        mergeKnowledgePropagationCandidates(direct, effective),
      );
    }).catch((error: unknown) => {
      if (cancelled) return;
      setDeriveKnowledgeItems(direct);
      setDeriveKnowledgeError(
        error instanceof Error
          ? error.message
          : 'Failed to load effective Knowledge resources',
      );
    }).finally(() => {
      if (!cancelled) setDeriveKnowledgeLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [showSpecSelector, refinement, deriveKnowledgeReload]);

  const openSpecSelector = () => {
    if (derivingSpec) return;
    setDeriveIdempotencyKey(uuidv4());
    lastSubmittedDeriveIntentRef.current = null;
    setDeriveKnowledgeItems([]);
    setDeriveKnowledgeError(null);
    setShowSpecSelector(true);
  };

  const handleDeriveSpec = async (
    knowledgeChoice: KnowledgePropagationChoice,
  ) => {
    if (!refinement) return;
    const intentFingerprint = JSON.stringify(
      buildKnowledgePropagationEnvelope(
        knowledgeChoice,
        '__intent_fingerprint__',
      ),
    );
    let idempotencyKey = deriveIdempotencyKey;
    if (
      lastSubmittedDeriveIntentRef.current !== null
      && lastSubmittedDeriveIntentRef.current !== intentFingerprint
    ) {
      idempotencyKey = uuidv4();
      setDeriveIdempotencyKey(idempotencyKey);
    }
    lastSubmittedDeriveIntentRef.current = intentFingerprint;
    setDerivingSpec(true);
    try {
      await api.deriveSpecFromRefinement(refinementId, {
        knowledge_propagation: buildKnowledgePropagationEnvelope(
          knowledgeChoice,
          idempotencyKey,
        ),
      });
      toast.success('Spec draft created');
      setShowSpecSelector(false);
      await loadRefinement();
      onChanged();
    } catch (err) { toast.error(getErrorMessage(err)); } finally { setDerivingSpec(false); }
  };

  const handleSpecSelectorConfirm = async (
    _selectedItems: SelectableItem[],
    _title: string,
    knowledgeChoice: KnowledgePropagationChoice,
  ) => {
    await handleDeriveSpec(knowledgeChoice);
  };

  const handleDelete = async () => {
    if (!refinement) return;
    if (!confirm(`Delete refinement "${refinement.title}"? Linked specs will be unlinked but not deleted.`)) return;
    try {
      await api.deleteRefinement(refinementId);
      toast.success('Refinement deleted');
      onChanged();
      onClose();
    } catch { toast.error('Failed to delete refinement'); }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-xl p-8">
          <div className="text-gray-500 dark:text-gray-400">Loading refinement...</div>
        </div>
      </div>
    );
  }

  if (!refinement) return null;

  const canDeriveSpec = refinement.status === 'done';

  const unansweredQA = refinement.qa_items?.filter((q) => q.answered_at == null).length || 0;
  const validationTabs: {
    id: ValidationSubTab;
    label: string;
  }[] = [
    ...(canAccessAmbiguityAssessment
      ? [{ id: 'ambiguity' as const, label: 'Ambiguity Assessment' }]
      : []),
    ...(canReadPolicyCompliance
      ? [{
          id: 'policy-compliance' as const,
          label: 'Policy Compliance',
        }]
      : []),
  ];
  const allTabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean; permission?: string }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'research-decisions', label: 'Research decisions', icon: <Lightbulb size={14} />, permission: 'refinement.research_decisions.read' },
    { id: 'resources', label: 'Resources', icon: <Layers size={14} /> },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: refinement.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'references', label: 'References', icon: <Link2 size={14} /> },
    ...(canViewValidation
      ? [{ id: 'validation' as ModalTab, label: 'Validation', icon: <Shield size={14} /> }]
      : []),
    { id: 'versions', label: 'Versions', icon: <Archive size={14} /> },
    { id: 'activity', label: 'Activity', icon: <History size={14} /> },
  ];
  const tabs = allTabs.filter((tab) => !tab.permission || perms.has(tab.permission));

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full ${expanded ? 'max-w-[95vw] h-[95vh]' : 'max-w-3xl h-[90vh]'} flex flex-col`}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[refinement.status]}`}>
              {STATUS_ICON[refinement.status]}
              {REFINEMENT_STATUS_LABELS[refinement.status]}
            </span>
            <DerivationPendingBadge label={getRefinementPendingDerivationLabel(refinement)} />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{refinement.title}</h2>
            <span className="text-xs text-gray-400 shrink-0">v{refinement.version}</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => openLineageGraph('refinement', refinement.id)}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title="Open lineage graph"
            >
              <GitBranch size={16} />
            </button>
            <button
              onClick={async () => {
                try {
                  const fullKnowledge = await Promise.all(
                    (refinement.knowledge_bases || []).map((kb) =>
                      api.getRefinementKnowledge(refinement.id, kb.id).catch(() => kb)
                    )
                  );
                  // Hydrate architecture design summaries into full designs so the
                  // Markdown export renders the Mermaid diagram — same pattern as Spec/Card.
                  const fullArchitecture = await Promise.all(
                    (refinement.architecture_designs || []).map((d) =>
                      api.getArchitectureDesign(d.id, true).catch(() => d)
                    )
                  );
                  const md = exportRefinement({ ...refinement, knowledge_bases: fullKnowledge as any, architecture_designs: fullArchitecture as any });
                  downloadMarkdown(md, `refinement_${slugify(refinement.title)}_v${refinement.version}.md`);
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
            <button onClick={loadRefinement} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title="Refresh">
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
                {REFINEMENT_STATUS_LABELS[status]}
                {movingTo === status && '...'}
              </button>
            ))}
          </div>
        )}

        {/* Compact lineage summary. References is the canonical full view. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-gray-100 px-6 py-2 text-xs text-gray-500 dark:border-gray-700/50 dark:text-gray-400">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-gray-400">From:</span>
            <button
              type="button"
              onClick={() =>
                modalStack?.push({
                  type: 'ideation',
                  id: refinement.ideation_id,
                })
              }
              className="inline-flex min-w-0 items-center gap-1 rounded bg-amber-50 px-2 py-0.5 text-amber-700 transition-all hover:ring-2 hover:ring-amber-300 dark:bg-amber-900/20 dark:text-amber-300 dark:hover:ring-amber-600"
              aria-label={`Open ideation ${parentIdeation?.title || refinement.ideation_id}`}
            >
              <Lightbulb size={11} />
              <span className="truncate">
                {parentIdeation?.title || refinement.ideation_id}
              </span>
              {parentIdeation && (
                <span className="shrink-0 text-[10px] text-amber-500 dark:text-amber-400">
                  v{parentIdeation.version}
                </span>
              )}
            </button>
          </div>
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-gray-400">To:</span>
            <RefinementToSummary
              specs={refinement.specs || []}
              onSeeReferences={() => {
                setReferenceTab('specs');
                setActiveTab('references');
              }}
            />
          </div>
        </div>

        {/* Tabs */}
        <div className="min-w-0">
          <AccessibleTabList
            idBase={`refinement-${refinement.id}`}
            ariaLabel="Refinement sections"
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
            idBase={`refinement-${refinement.id}`}
            tabId="details"
            value={activeTab}
          >
            <div className="space-y-5">
              {refinement.status === 'cancelled' && (
                <CancellationDetails
                  id="cancellation-panel"
                  entityLabel="refinement"
                  reason={refinement.cancellation_reason}
                  cancelledBy={refinement.cancelled_by}
                  cancelledAt={refinement.cancelled_at}
                />
              )}
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Description</h4>
                <EditableField
                  value={refinement.description || ''}
                  onSave={async (val) => {
                    const updated = await api.updateRefinement(refinementId, { description: val });
                    setRefinement(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No description"
                />
              </div>
              <EditableList
                title="In Scope"
                items={refinement.in_scope}
                placeholder="Add an in-scope item..."
                colorClass="text-green-600 dark:text-green-400"
                onUpdate={async (items) => {
                  try { const updated = await api.updateRefinement(refinementId, { in_scope: items }); setRefinement(updated); } catch { toast.error('Failed to update'); }
                }}
              />
              <EditableList
                title="Out of Scope"
                items={refinement.out_of_scope}
                placeholder="Add an out-of-scope item..."
                colorClass="text-red-600 dark:text-red-400"
                onUpdate={async (items) => {
                  try { const updated = await api.updateRefinement(refinementId, { out_of_scope: items }); setRefinement(updated); } catch { toast.error('Failed to update'); }
                }}
              />
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Analysis</h4>
                <EditableField
                  value={refinement.analysis || ''}
                  onSave={async (val) => {
                    const updated = await api.updateRefinement(refinementId, { analysis: val });
                    setRefinement(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No analysis"
                />
              </div>
              {refinement.labels && refinement.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {refinement.labels.map((label, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{label}</span>
                  ))}
                </div>
              )}
            </div>
          </AccessibleTabPanel>

          <AccessibleTabPanel
            idBase={`refinement-${refinement.id}`}
            tabId="resources"
            value={activeTab}
            mount="lazy-keep"
          >
            <RefinementResourcesPanel
              refinement={refinement}
              fallbackBoardId={_boardId}
              expanded={expanded}
              onRefinementChanged={(updated) => {
                setRefinement(updated);
                void loadAllowedTransitions(updated);
                onChanged();
              }}
              onArchitectureChanged={(items) => {
                setRefinement((current) =>
                  current
                    ? { ...current, architecture_designs: items }
                    : current,
                );
                void loadRefinement();
                onChanged();
              }}
              onKnowledgeCreated={(knowledge) => {
                setRefinement((current) =>
                  current
                    ? {
                        ...current,
                        knowledge_bases: [
                          ...(current.knowledge_bases || []),
                          knowledge,
                        ],
                      }
                    : current,
                );
                void loadRefinement();
                onChanged();
              }}
              onKnowledgeDeleted={(knowledgeId) => {
                setRefinement((current) =>
                  current
                    ? {
                        ...current,
                        knowledge_bases: (
                          current.knowledge_bases || []
                        ).filter((item) => item.id !== knowledgeId),
                      }
                    : current,
                );
                void loadRefinement();
                onChanged();
              }}
            />
          </AccessibleTabPanel>
          {canViewValidation && (
            <AccessibleTabPanel
              idBase={`refinement-${refinement.id}`}
              tabId="validation"
              value={activeTab}
              mount="lazy-keep"
            >
              <div
                className="space-y-4"
                data-testid="refinement-validation-panel"
              >
                <AccessibleTabList
                  idBase={`refinement-${refinement.id}-validation`}
                  ariaLabel="Refinement validation"
                  items={validationTabs}
                  value={validationSubTab}
                  onValueChange={setValidationSubTab}
                  variant="secondary"
                />

                {canAccessAmbiguityAssessment && (
                  <AccessibleTabPanel
                    idBase={`refinement-${refinement.id}-validation`}
                    tabId="ambiguity"
                    value={validationSubTab}
                    mount="lazy-keep"
                  >
                    {requiresAmbiguityGate ? (
                      <section
                        className="space-y-4"
                        data-testid="refinement-ambiguity-gate-panel"
                      >
                        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-800 dark:text-gray-100">
                          <Shield size={15} /> Ambiguity assessment and gate
                        </h3>
                        {canReadQuality ? (
                          <QualityPanel
                            key={`${refinement.id}:${refinement.version}:${refinement.skip_ambiguity_gate ?? false}`}
                            subjectType="refinement"
                            subjectId={refinementId}
                            subjectVersion={refinement.version}
                            subjectStatus={refinement.status}
                            subjectArchived={refinement.archived ?? false}
                            canRead={canReadQuality}
                            canAssess={canAssessQuality}
                            canProposeQuestions={canProposeQualityQuestions}
                            onAssessmentRecorded={() => {
                              void loadRefinement();
                              onChanged();
                            }}
                          />
                        ) : (
                          <p
                            className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300"
                            data-testid="refinement-ambiguity-currentness-note"
                          >
                            The assessment and server gate preview are omitted because Quality read permission is not available.
                          </p>
                        )}
                        <AmbiguityGateSkipToggle
                          subjectLabel="refinement"
                          checked={refinement.skip_ambiguity_gate ?? false}
                          disabled={savingAmbiguitySkip}
                          onCheckedChange={(checked) => {
                            void handleToggleAmbiguitySkip(checked);
                          }}
                        />
                      </section>
                    ) : canReadQuality ? (
                      <QualityPanel
                        subjectType="refinement"
                        subjectId={refinementId}
                        subjectVersion={refinement.version}
                        subjectStatus={refinement.status}
                        subjectArchived={refinement.archived ?? false}
                        canRead={canReadQuality}
                        canAssess={canAssessQuality}
                        canProposeQuestions={canProposeQualityQuestions}
                        onAssessmentRecorded={() => {
                          void loadRefinement();
                          onChanged();
                        }}
                      />
                    ) : null}
                  </AccessibleTabPanel>
                )}

                {canReadPolicyCompliance && (
                  <AccessibleTabPanel
                    idBase={`refinement-${refinement.id}-validation`}
                    tabId="policy-compliance"
                    value={validationSubTab}
                    mount="lazy-keep"
                    className="space-y-4"
                  >
                    <PolicyComplianceTransitionPreview
                      preview={policyTransitionPreview}
                      rejection={policyTransitionRejection}
                    />
                    <PolicyCompliancePanel
                      boardId={refinement.board_id || _boardId}
                      entityType="refinement"
                      subjectId={refinement.id}
                      subjectVersion={refinement.version}
                      transitionPreview={policyTransitionPreview}
                      refreshKey={refinement.version}
                      onEvaluated={() => {
                        void loadAllowedTransitions(refinement);
                      }}
                      onRefreshed={() => {
                        void loadAllowedTransitions(refinement);
                      }}
                    />
                  </AccessibleTabPanel>
                )}
              </div>
            </AccessibleTabPanel>
          )}
          {canReadResearchDecisions && (
            <AccessibleTabPanel
              idBase={`refinement-${refinement.id}`}
              tabId="research-decisions"
              value={activeTab}
              mount="lazy-keep"
            >
              <div data-testid="research-decisions-tab-state">
                <ResearchDecisionTab
                  key={refinement.id}
                  boardId={refinement.board_id || _boardId}
                  refinementId={refinement.id}
                  refinementStatus={refinement.status}
                  refinementArchived={refinement.archived}
                  legacyDecisions={refinement.decisions}
                  onRefinementVersionChanged={(version) => {
                    setRefinement((current) => current ? { ...current, version } : current);
                    onChanged();
                  }}
                />
              </div>
            </AccessibleTabPanel>
          )}
          <AccessibleTabPanel
            idBase={`refinement-${refinement.id}`}
            tabId="qa"
            value={activeTab}
            mount="lazy-keep"
          >
            <QATab refinementId={refinementId} mentionables={mentionables} />
          </AccessibleTabPanel>
          <AccessibleTabPanel
            idBase={`refinement-${refinement.id}`}
            tabId="references"
            value={activeTab}
            mount="lazy-keep"
          >
            <RefinementReferencesPanel
              originId={refinement.ideation_id}
              origin={parentIdeation}
              specs={refinement.specs || []}
              activeTab={referenceTab}
              onTabChange={setReferenceTab}
              canDeriveSpec={canDeriveSpec}
              derivingSpec={derivingSpec}
              onCreateSpec={openSpecSelector}
            />
          </AccessibleTabPanel>
          <AccessibleTabPanel
            idBase={`refinement-${refinement.id}`}
            tabId="versions"
            value={activeTab}
            mount="lazy-keep"
          >
            <VersionsTab refinementId={refinementId} />
          </AccessibleTabPanel>
          <AccessibleTabPanel
            idBase={`refinement-${refinement.id}`}
            tabId="activity"
            value={activeTab}
            mount="lazy-keep"
          >
            <HistoryTab refinementId={refinementId} />
          </AccessibleTabPanel>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete refinement
          </button>
          <div className="flex gap-2">
            {canDeriveSpec && (
              <button onClick={openSpecSelector} disabled={derivingSpec} className="btn btn-primary flex items-center gap-1.5">
                <Zap size={16} />
                {derivingSpec ? 'Creating...' : 'Create Spec Draft'}
              </button>
            )}
            <button onClick={onClose} className="btn btn-secondary">Close</button>
          </div>
        </div>
      </div>

      {/* Context selector for spec creation */}
      {showSpecSelector && refinement && (
        <ContextSelector
          title={refinement.title}
          description="Choose only the non-authoritative Knowledge references that are relevant to the derived spec."
          items={buildRefinementItems(refinement)}
          knowledgeItems={deriveKnowledgeItems}
          knowledgeLoading={deriveKnowledgeLoading}
          knowledgeError={deriveKnowledgeError}
          onKnowledgeRetry={() =>
            setDeriveKnowledgeReload((current) => current + 1)
          }
          knowledgeOnly
          targetLabel="Spec Draft"
          onConfirm={handleSpecSelectorConfirm}
          onCancel={() => setShowSpecSelector(false)}
          busy={derivingSpec}
        />
      )}

      {/* Cancellation justification (ITEM 17) */}
      <CancellationReasonDialog
        open={cancelDialogOpen}
        entityLabel="refinement"
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
