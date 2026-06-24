/**
 * IdeationModal - View and manage an ideation, evaluate scope, derive specs
 */

import { useEffect, useState } from 'react';
import {
  X,
  ChevronRight,
  Zap,
  CheckCircle2,
  Clock,
  Ban,
  FileText,
  Lightbulb,
  BookOpen,
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
  Monitor,
  Maximize2,
  Minimize2,
  Download,
  GitBranch,
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
  IdeationKnowledgeSummary,
  IdeationSnapshot,
  IdeationSnapshotSummary,
  RefinementSummary,
  StorySummary,
  EffectiveResourceItem,
} from '@/types';
import {
  IDEATION_STATUSES,
  IDEATION_STATUS_LABELS,
  COMPLEXITY_LABELS,
  STORY_STATUS_LABELS,
} from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { ContextSelector, buildIdeationItems, compileSelectedContext, type SelectableItem } from '@/components/shared/ContextSelector';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { EditableField } from '@/components/shared/EditableField';
import { ArchitectureTab } from '@/components/architecture';
import { ResourceGateSummary } from '@/components/resources/ResourceGateSummary';

interface IdeationModalProps {
  ideationId: string;
  boardId: string;
  onClose: () => void;
  onChanged: () => void;
}

type ModalTab = 'details' | 'stories' | 'mockups' | 'architecture' | 'qa' | 'knowledge' | 'refinements' | 'versions' | 'history';

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

const REFINEMENT_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  review: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  done: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
};

const STORY_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  triage: 'bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-300',
  ready: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  converted: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
};

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
                  <div className="flex gap-4 text-sm">
                    <span>Domains: <strong>{viewing.scope_assessment.domains}</strong>/5</span>
                    <span>Ambiguity: <strong>{viewing.scope_assessment.ambiguity}</strong>/5</span>
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

type IdeationKnowledgeListItem = IdeationKnowledgeSummary & {
  inherited?: boolean;
  read_only?: boolean;
  source_entity_type?: string | null;
  source_entity_id?: string | null;
  source_entity_title?: string | null;
  content?: string;
};

function effectiveKnowledgeToIdeationItem(item: EffectiveResourceItem): IdeationKnowledgeListItem | null {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Partial<IdeationKnowledgeListItem>
    : item as Partial<IdeationKnowledgeListItem>;
  const id = String(item.id || resource.id || '');
  if (!id) return null;
  return {
    id,
    ideation_id: String(resource.ideation_id || item.source_entity_id || ''),
    title: String(resource.title || item.title || 'Inherited knowledge'),
    description: typeof resource.description === 'string' ? resource.description : null,
    mime_type: String(resource.mime_type || 'text/markdown'),
    created_at: String(resource.created_at || ''),
    content: typeof resource.content === 'string' ? resource.content : '',
    inherited: item.inherited,
    read_only: item.read_only,
    source_entity_type: item.source_entity_type ?? item.provenance?.source_entity_type ?? null,
    source_entity_id: item.source_entity_id ?? item.provenance?.source_entity_id ?? null,
    source_entity_title: item.source_entity_title ?? item.provenance?.source_entity_title ?? null,
  };
}

function knowledgeSourceLabel(item: IdeationKnowledgeListItem): string {
  const type = item.source_entity_type || 'source';
  const title = item.source_entity_title || item.source_entity_id || 'parent';
  return `${type}: ${title}`;
}

function KnowledgeTab({ ideationId, boardId }: { ideationId: string; boardId: string }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<IdeationKnowledgeSummary[]>([]);
  const [effectiveItems, setEffectiveItems] = useState<EffectiveResourceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [viewContent, setViewContent] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  useEffect(() => { load(); }, [ideationId]);

  const visibleItems: IdeationKnowledgeListItem[] = [
    ...items,
    ...effectiveItems
      .filter((item) => item.inherited && !items.some((direct) => direct.id === item.id))
      .map(effectiveKnowledgeToIdeationItem)
      .filter((item): item is IdeationKnowledgeListItem => Boolean(item)),
  ];

  const load = async () => {
    setLoading(true);
    try {
      const [direct, effective] = await Promise.all([
        api.listIdeationKnowledge(ideationId),
        api.getEffectiveResources(boardId, 'ideation', ideationId).catch(() => null),
      ]);
      setItems(direct);
      setEffectiveItems(effective?.resources.knowledge_base || []);
    } catch {
      toast.error('Failed to load knowledge base');
    } finally {
      setLoading(false);
    }
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
      await load();
    } catch {
      toast.error('Failed to add knowledge');
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this knowledge base item?')) return;
    try {
      await api.deleteIdeationKnowledge(ideationId, id);
      if (viewingId === id) {
        setViewingId(null);
        setViewContent('');
      }
      await load();
    } catch {
      toast.error('Failed to delete knowledge');
    }
  };

  const handleView = async (id: string) => {
    if (viewingId === id) {
      setViewingId(null);
      setViewContent('');
      return;
    }
    const inherited = visibleItems.find((item) => item.id === id && item.inherited);
    if (inherited) {
      setViewingId(id);
      setViewContent(inherited.content || '');
      return;
    }
    try {
      const kb = await api.getIdeationKnowledge(ideationId, id);
      setViewingId(id);
      setViewContent(kb.content);
    } catch {
      toast.error('Failed to load knowledge');
    }
  };

  if (loading) {
    return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading knowledge base...</div>;
  }

  return (
    <div className="space-y-3">
      {visibleItems.length === 0 && !adding && (
        <div className="text-center py-6">
          <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge base items</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Attach reference documents, discovery notes, or context docs</p>
        </div>
      )}
      {visibleItems.map((item) => (
        <div key={item.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div
            className="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-700/50 cursor-pointer"
            onClick={() => handleView(item.id)}
          >
            <div className="flex items-center gap-2 min-w-0">
              <BookOpen size={14} className="text-amber-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate">{item.title}</span>
              {item.inherited && (
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-200">
                  from {knowledgeSourceLabel(item)}
                </span>
              )}
              <span className="text-[10px] px-1 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-600 dark:text-gray-400">{item.mime_type}</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {!item.read_only && (
                <button
                  onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
                  className="p-1 text-gray-400 hover:text-red-500"
                >
                  <Trash2 size={14} />
                </button>
              )}
              {viewingId === item.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </div>
          </div>
          {viewingId === item.id && viewContent && (
            <div className="px-3 py-2 border-t border-gray-100 dark:border-gray-700">
              <pre className="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">{viewContent}</pre>
            </div>
          )}
        </div>
      ))}
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

  const isAnswered = (qa: IdeationQAItem) => qa.answer || (qa.selected && qa.selected.length > 0);
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

function ScopeGauge({ label, value }: { label: string; value: number }) {
  const pct = ((value - 1) / 4) * 100;
  const color =
    value <= 2 ? 'bg-green-500' :
    value <= 3 ? 'bg-yellow-500' :
    'bg-red-500';

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-400">{label}</span>
        <span className="text-xs font-bold text-gray-700 dark:text-gray-300">{value}/5</span>
      </div>
      <div className="w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/* ============================================================
   Main IdeationModal
   ============================================================ */

export function IdeationModal({ ideationId, boardId: _boardId, onClose, onChanged }: IdeationModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();
  const [ideation, setIdeation] = useState<Ideation | null>(null);
  const [loading, setLoading] = useState(true);
  const [movingTo, setMovingTo] = useState<IdeationStatus | null>(null);
  const [savingSkip, setSavingSkip] = useState(false);
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [expanded, setExpanded] = useState(false);
  const [derivingSpec, setDerivingSpec] = useState(false);

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

  const loadIdeation = async () => {
    setLoading(true);
    try {
      const data = await api.getIdeation(ideationId);
      setIdeation(data);
    } catch { toast.error('Failed to load ideation'); } finally { setLoading(false); }
  };

  const handleMove = async (status: IdeationStatus) => {
    if (!ideation) return;
    setMovingTo(status);
    try {
      const updated = await api.moveIdeation(ideationId, { status });
      setIdeation(updated);
      onChanged();
      toast.success(`Ideation moved to ${IDEATION_STATUS_LABELS[status]}`);
    } catch (err) { toast.error(getErrorMessage(err)); } finally { setMovingTo(null); }
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

  const getNextStatuses = (current: IdeationStatus): IdeationStatus[] => {
    const flow: Record<IdeationStatus, IdeationStatus[]> = {
      draft: ['review', 'cancelled'],
      review: ['approved', 'draft', 'cancelled'],
      approved: ['evaluating', 'review', 'cancelled'],
      evaluating: ['done', 'approved', 'cancelled'],
      done: ['draft'],
      cancelled: [],
    };
    return (flow[current] || []).filter((s) => IDEATION_STATUSES.includes(s));
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

  const nextStatuses = getNextStatuses(ideation.status);
  const canEvaluate = ideation.status === 'evaluating';
  const canDeriveSpec = ideation.status === 'done' && ideation.complexity === 'small';
  const needsRefinements = ideation.status === 'done' && ideation.complexity && ideation.complexity !== 'small';

  const unansweredQA = ideation.qa_items?.filter((q) => !q.answer).length || 0;
  const tabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'stories', label: 'Stories', icon: <BookOpen size={14} />, count: ideation.stories?.length || 0 },
    { id: 'mockups', label: 'Mockups', icon: <Monitor size={14} />, count: ideation.screen_mockups?.length || 0 },
    { id: 'architecture', label: 'Architecture', icon: <GitBranch size={14} />, count: ideation.architecture_designs?.length || 0 },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: ideation.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'knowledge', label: 'Knowledge', icon: <BookOpen size={14} />, count: ideation.knowledge_bases?.length || 0 },
    { id: 'refinements', label: 'Refinements', icon: <Layers size={14} />, count: ideation.refinements?.length || 0 },
    { id: 'versions', label: 'Versions', icon: <Archive size={14} /> },
    { id: 'history', label: 'Activity', icon: <History size={14} /> },
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
        <div className="min-w-0 px-6 pt-3 border-b border-gray-200 dark:border-gray-700">
          <div className="flex min-w-0 items-center gap-1 overflow-x-auto overflow-y-hidden">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
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
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {activeTab === 'details' && (
            <div className="space-y-5">
              <ResourceGateSummary
                boardId={ideation.board_id || _boardId}
                entityType="ideation"
                entityId={ideationId}
              />
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

              {/* Scope Assessment Gauges */}
              {ideation.scope_assessment && (
                <div>
                  <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-1.5 mb-3">
                    <Gauge size={14} /> Scope Assessment
                  </h4>
                  <div className="space-y-3">
                    {(['domains', 'ambiguity', 'dependencies'] as const).map((dim) => {
                      const sa = ideation.scope_assessment as Record<string, unknown>;
                      const score = (sa[dim] as number) || 0;
                      const just = (sa[`${dim}_justification`] as string) || '';
                      return (
                        <div key={dim} className="flex items-start gap-3">
                          <ScopeGauge label={dim.charAt(0).toUpperCase() + dim.slice(1)} value={score} />
                          {just && (
                            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 flex-1 italic">
                              {just}
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Max Ambiguity Gate (spec 2485780b) — only when the board gate is enabled */}
              {(currentBoard?.settings?.require_ideation_ambiguity_gate ?? false) && (() => {
                const threshold = currentBoard?.settings?.max_ideation_ambiguity ?? 3;
                const current = ideation.scope_assessment?.ambiguity ?? null;
                const skip = ideation.skip_ambiguity_gate ?? false;
                const blocks = !skip && (current == null || current > threshold);
                const statusLabel = skip ? 'Skipped' : blocks ? 'Blocks completion' : 'Passing';
                const statusClass = skip
                  ? 'text-gray-500 dark:text-gray-400'
                  : blocks
                    ? 'text-red-600 dark:text-red-400'
                    : 'text-green-600 dark:text-green-400';
                return (
                  <div
                    className="rounded-lg border border-amber-200 bg-amber-50/60 p-3 dark:border-amber-500/30 dark:bg-amber-500/10"
                    data-testid="ambiguity-gate-panel"
                  >
                    <h4 className="text-sm font-semibold text-amber-800 dark:text-amber-200 flex items-center gap-1.5 mb-2">
                      <Shield size={14} /> Max Ambiguity Gate
                    </h4>
                    <div className="space-y-2">
                      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-600 dark:text-gray-300">
                        <span>Board threshold: <strong>{threshold}</strong>/5</span>
                        <span>
                          Current ambiguity:{' '}
                          <strong>{current ?? 'not evaluated'}</strong>{current != null ? '/5' : ''}
                        </span>
                        <span data-testid="ambiguity-gate-status">
                          Status: <strong className={statusClass}>{statusLabel}</strong>
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-3 rounded border border-amber-200/70 bg-white/50 px-3 py-2 dark:border-amber-500/20 dark:bg-gray-900/30">
                        <div>
                          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                            Skip Max ambiguity gate
                          </span>
                          <p className="text-[10px] text-gray-500 dark:text-gray-400">
                            Allow this ideation to complete without the board ambiguity threshold.
                          </p>
                        </div>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={skip}
                          aria-label="Skip the Max ambiguity gate for this ideation"
                          disabled={savingSkip}
                          onClick={() => handleToggleAmbiguitySkip(!skip)}
                          data-testid="toggle-skip-ambiguity-gate"
                          className={`relative inline-flex h-5 w-10 shrink-0 items-center rounded-full p-0.5 transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                            skip ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'
                          }`}
                        >
                          <span
                            className={`h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                              skip ? 'translate-x-5' : 'translate-x-0'
                            }`}
                          />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })()}

              {/* Complexity badge */}
              {ideation.complexity && (
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Complexity:</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${COMPLEXITY_COLORS[ideation.complexity]}`}>
                    {COMPLEXITY_LABELS[ideation.complexity]}
                  </span>
                </div>
              )}

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

          {activeTab === 'mockups' && (
            <MockupsTab
              screenMockups={ideation.screen_mockups}
              boardId={ideation.board_id}
              entityType="ideation"
              entityId={ideationId}
              expanded={expanded}
              onUpdate={async (mockups) => {
                await api.updateIdeation(ideationId, { screen_mockups: mockups });
                await loadIdeation();
              }}
            />
          )}
          {activeTab === 'architecture' && (
            <ArchitectureTab
              parentType="ideation"
              parentId={ideationId}
              boardId={ideation.board_id}
              entityType="ideation"
              entityId={ideationId}
              expanded={expanded}
              screenMockups={ideation.screen_mockups || []}
              onChanged={(items) => setIdeation((current) => current ? { ...current, architecture_designs: items } : current)}
            />
          )}
          {activeTab === 'qa' && <QATab ideationId={ideationId} mentionables={mentionables} />}
          {activeTab === 'knowledge' && <KnowledgeTab ideationId={ideationId} boardId={ideation.board_id} />}
          {activeTab === 'versions' && <VersionsTab ideationId={ideationId} />}
          {activeTab === 'history' && <HistoryTab ideationId={ideationId} />}

          {activeTab === 'stories' && (
            <div className="space-y-3">
              {(!ideation.stories || ideation.stories.length === 0) && (
                <div className="text-center py-6">
                  <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                  <p className="text-sm text-gray-500 dark:text-gray-400">No related stories</p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Stories converted or linked to this ideation will appear here</p>
                </div>
              )}

              {ideation.stories && ideation.stories.map((story: StorySummary) => (
                <div key={story.id} className="rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2.5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-2 min-w-0">
                      <BookOpen size={14} className="text-blue-500 shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{story.title}</p>
                        <p className="mt-1 max-h-10 overflow-hidden text-xs text-gray-500 dark:text-gray-400">{story.description}</p>
                      </div>
                    </div>
                    <span className={`text-xs px-1.5 py-0.5 rounded shrink-0 ${STORY_STATUS_COLORS[story.status] || ''}`}>
                      {STORY_STATUS_LABELS[story.status]}
                    </span>
                  </div>

                  {(story.actor || story.goal || (story.labels && story.labels.length > 0)) && (
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-gray-400">
                      {story.actor && <span>Actor: {story.actor}</span>}
                      {story.goal && <span>Goal: {story.goal}</span>}
                      {story.labels?.slice(0, 4).map((label) => (
                        <span key={label} className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                          {label}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {activeTab === 'refinements' && (
            <div className="space-y-3">
              {(!ideation.refinements || ideation.refinements.length === 0) && (
                <div className="text-center py-6">
                  <Layers size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                  <p className="text-sm text-gray-500 dark:text-gray-400">No refinements yet</p>
                  {ideation.status === 'done' ? (
                    <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Create refinements to break down this ideation into focused areas</p>
                  ) : (
                    <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Mark the ideation as "done" first to create refinements from it</p>
                  )}
                </div>
              )}

              {ideation.refinements && ideation.refinements.map((ref: RefinementSummary) => (
                <div key={ref.id} className="flex items-center justify-between py-2 px-3 rounded-lg border border-gray-200 dark:border-gray-700">
                  <div className="flex items-center gap-2 min-w-0">
                    <Layers size={14} className="text-violet-500 shrink-0" />
                    <span className="text-sm text-gray-700 dark:text-gray-300 truncate">{ref.title}</span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={`text-xs px-1.5 py-0.5 rounded ${REFINEMENT_STATUS_COLORS[ref.status] || ''}`}>
                      {ref.status.replace('_', ' ')}
                    </span>
                    <span className="text-[10px] text-gray-400">v{ref.version}</span>
                  </div>
                </div>
              ))}

              {ideation.status === 'done' && (
                <button
                  onClick={() => setSelectorTarget('refinement')}
                  className="flex items-center gap-1 text-sm text-violet-600 dark:text-violet-400 hover:text-violet-800 dark:hover:text-violet-300"
                >
                    <Plus size={14} /> Create Refinement
                  </button>
              )}
            </div>
          )}
        </div>

        {/* Evaluate Form (overlay at bottom of body) */}
        {showEvalForm && (
          <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-700/50 bg-amber-50/50 dark:bg-amber-900/10 overflow-y-auto max-h-[40vh]">
            <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Evaluate Scope</h4>
            <div className="space-y-4 mb-3">
              {([
                { label: 'Domains', sublabel: 'How many systems/services are impacted?', value: evalDomains, setValue: setEvalDomains, just: evalDomainsJust, setJust: setEvalDomainsJust },
                { label: 'Ambiguity', sublabel: 'How clear are the requirements?', value: evalAmbiguity, setValue: setEvalAmbiguity, just: evalAmbiguityJust, setJust: setEvalAmbiguityJust },
                { label: 'Dependencies', sublabel: 'How many external dependencies?', value: evalDependencies, setValue: setEvalDependencies, just: evalDependenciesJust, setJust: setEvalDependenciesJust },
              ] as const).map((dim) => (
                <div key={dim.label} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-xs font-semibold text-gray-700 dark:text-gray-300">{dim.label}</label>
                    <span className={`text-sm font-bold ${
                      dim.value >= 4 ? 'text-red-600' : dim.value >= 3 ? 'text-amber-600' : dim.value >= 2 ? 'text-yellow-600' : 'text-green-600'
                    }`}>{dim.value}/5</span>
                  </div>
                  <p className="text-[10px] text-gray-400 mb-2">{dim.sublabel}</p>
                  <input
                    type="range"
                    min={1}
                    max={5}
                    step={1}
                    value={dim.value}
                    onChange={(e) => dim.setValue(Number(e.target.value))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer accent-blue-600"
                  />
                  <div className="flex justify-between text-[9px] text-gray-400 mt-0.5 px-0.5">
                    <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span>
                  </div>
                  <textarea
                    value={dim.just}
                    onChange={(e) => dim.setJust(e.target.value)}
                    placeholder={`Justification: why ${dim.label.toLowerCase()} = ${dim.value}?`}
                    className="w-full mt-2 px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
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
              <p className="text-[10px] text-amber-600 dark:text-amber-400 mt-1 text-right">All justifications are required</p>
            )}
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete ideation
          </button>
          <div className="flex gap-2">
            {canEvaluate && (
              <button
                onClick={() => setShowEvalForm(!showEvalForm)}
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
    </div>
  );
}
