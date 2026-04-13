/**
 * RefinementModal - View and manage a refinement, derive specs
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
  BookOpen,
  Plus,
  RefreshCw,
  Monitor,
  Maximize2,
  Minimize2,
  Download,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { exportRefinement, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import type { Refinement, RefinementStatus, RefinementQAItem, RefinementHistoryEntry, RefinementSnapshot, RefinementSnapshotSummary, RefinementKnowledgeSummary } from '@/types';
import { REFINEMENT_STATUSES, REFINEMENT_STATUS_LABELS } from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { IdeationModal } from '@/components/ideations/IdeationModal';
import { ContextSelector, buildRefinementItems, type SelectableItem } from '@/components/shared/ContextSelector';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { EditableField } from '@/components/shared/EditableField';

interface RefinementModalProps {
  refinementId: string;
  boardId: string;
  onClose: () => void;
  onChanged: () => void;
}

type ModalTab = 'details' | 'mockups' | 'qa' | 'knowledge' | 'specs' | 'versions' | 'history';

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

const SPEC_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  review: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  in_progress: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
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
    return val.map((v, i) => `${i + 1}. ${v}`).join('\n');
  }
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
            onKeyDown={(e) => { if (e.key === 'Enter') add(); if (e.key === 'Escape') { setEditing(false); setDraft(''); } }}
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

function KnowledgeTab({ refinementId }: { refinementId: string }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<RefinementKnowledgeSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [viewContent, setViewContent] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  useEffect(() => { load(); }, [refinementId]);

  const load = async () => {
    setLoading(true);
    try { setItems(await api.listRefinementKnowledge(refinementId)); } catch { /* */ } finally { setLoading(false); }
  };

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    try {
      await api.createRefinementKnowledge(refinementId, { title: newTitle.trim(), description: newDesc.trim() || undefined, content: newContent.trim() });
      toast.success('Knowledge added'); setAdding(false); setNewTitle(''); setNewDesc(''); setNewContent(''); await load();
    } catch { toast.error('Failed to add knowledge'); }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this knowledge base item?')) return;
    try { await api.deleteRefinementKnowledge(refinementId, id); if (viewingId === id) { setViewingId(null); setViewContent(''); } await load(); } catch { toast.error('Failed to delete'); }
  };

  const handleView = async (id: string) => {
    if (viewingId === id) { setViewingId(null); setViewContent(''); return; }
    try { const kb = await api.getRefinementKnowledge(refinementId, id); setViewingId(id); setViewContent(kb.content); } catch { toast.error('Failed to load'); }
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
          <div className="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-700/50 cursor-pointer" onClick={() => handleView(item.id)}>
            <div className="flex items-center gap-2 min-w-0">
              <BookOpen size={14} className="text-amber-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate">{item.title}</span>
              <span className="text-[10px] px-1 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-600 dark:text-gray-400">{item.mime_type}</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }} className="p-1 text-gray-400 hover:text-red-500"><Trash2 size={14} /></button>
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
          <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Title" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <input type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Description (optional)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <textarea value={newContent} onChange={(e) => setNewContent(e.target.value)} placeholder="Content..." className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" rows={6} />
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
    if (qa.question_type === 'choice') {
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

  const isAnswered = (qa: RefinementQAItem) => qa.answer || (qa.selected && qa.selected.length > 0);
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

export function RefinementModal({ refinementId, boardId: _boardId, onClose, onChanged }: RefinementModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();
  const [refinement, setRefinement] = useState<Refinement | null>(null);
  const [loading, setLoading] = useState(true);
  const [derivingSpec, setDerivingSpec] = useState(false);
  const [movingTo, setMovingTo] = useState<RefinementStatus | null>(null);
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [expanded, setExpanded] = useState(false);

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
  const [viewingIdeationId, setViewingIdeationId] = useState<string | null>(null);

  useEffect(() => { loadRefinement(); }, [refinementId]);

  const loadRefinement = async () => {
    setLoading(true);
    try {
      const data = await api.getRefinement(refinementId);
      setRefinement(data);
      if (data.ideation_id) {
        try {
          const ideation = await api.getIdeation(data.ideation_id);
          setParentIdeation({ id: ideation.id, title: ideation.title, version: ideation.version });
        } catch { setParentIdeation(null); }
      }
    } catch { toast.error('Failed to load refinement'); } finally { setLoading(false); }
  };

  const handleMove = async (status: RefinementStatus) => {
    if (!refinement) return;
    setMovingTo(status);
    try {
      const updated = await api.moveRefinement(refinementId, { status });
      setRefinement(updated);
      onChanged();
      toast.success(`Refinement moved to ${REFINEMENT_STATUS_LABELS[status]}`);
    } catch { toast.error('Failed to move refinement'); } finally { setMovingTo(null); }
  };

  const [showSpecSelector, setShowSpecSelector] = useState(false);

  const handleDeriveSpec = async () => {
    if (!refinement) return;
    setDerivingSpec(true);
    try {
      // Use derive endpoint — propagates KBs, mockups, and compiles context server-side
      await api.deriveSpecFromRefinement(refinementId);
      toast.success('Spec draft created');
      await loadRefinement();
      onChanged();
    } catch { toast.error('Failed to create spec'); } finally { setDerivingSpec(false); }
  };

  const handleSpecSelectorConfirm = async (_selectedItems: SelectableItem[], _title: string) => {
    await handleDeriveSpec();
    setShowSpecSelector(false);
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

  const getNextStatuses = (current: RefinementStatus): RefinementStatus[] => {
    const flow: Record<RefinementStatus, RefinementStatus[]> = {
      draft: ['review', 'cancelled'],
      review: ['approved', 'draft', 'cancelled'],
      approved: ['done', 'review', 'cancelled'],
      done: ['draft'],
      cancelled: [],
    };
    return (flow[current] || []).filter((s) => REFINEMENT_STATUSES.includes(s));
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

  const nextStatuses = getNextStatuses(refinement.status);
  const canDeriveSpec = refinement.status === 'done';

  const unansweredQA = refinement.qa_items?.filter((q) => !q.answer).length || 0;
  const tabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean }[] = [
    { id: 'details', label: 'Details', icon: <Layers size={14} /> },
    { id: 'mockups', label: 'Mockups', icon: <Monitor size={14} />, count: refinement.screen_mockups?.length || 0 },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: refinement.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'knowledge', label: 'Knowledge', icon: <BookOpen size={14} />, count: refinement.knowledge_bases?.length || 0 },
    { id: 'specs', label: 'Specs', icon: <Link2 size={14} />, count: refinement.specs?.length || 0 },
    { id: 'versions', label: 'Versions', icon: <Archive size={14} /> },
    { id: 'history', label: 'Activity', icon: <History size={14} /> },
  ];

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
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{refinement.title}</h2>
            <span className="text-xs text-gray-400 shrink-0">v{refinement.version}</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => { const md = exportRefinement(refinement); downloadMarkdown(md, `refinement_${slugify(refinement.title)}_v${refinement.version}.md`); }}
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

        {/* Provenance breadcrumb */}
        {parentIdeation && (
          <div className="px-6 py-2 border-b border-gray-100 dark:border-gray-700/50 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            <span className="text-gray-400">From:</span>
            <button
              onClick={() => setViewingIdeationId(parentIdeation.id)}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300 hover:ring-2 hover:ring-amber-300 dark:hover:ring-amber-600 transition-all cursor-pointer"
            >
              <Lightbulb size={11} />
              {parentIdeation.title}
              <span className="text-[10px] text-amber-500 dark:text-amber-400">v{parentIdeation.version}</span>
            </button>
          </div>
        )}

        {/* Tabs */}
        <div className="flex items-center gap-1 px-6 pt-3 border-b border-gray-200 dark:border-gray-700">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
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
              {refinement.decisions && refinement.decisions.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Decisions</h4>
                  <ol className="space-y-1.5 ml-1">
                    {refinement.decisions.map((decision, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-400">
                        <span className="text-xs text-gray-400 mt-0.5 w-4 shrink-0">{i + 1}.</span>
                        <span>{decision}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {/* Parent ideation link */}
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Parent Ideation</h4>
                <span className="text-sm text-indigo-600 dark:text-indigo-400">{refinement.ideation_id}</span>
              </div>
              {refinement.labels && refinement.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {refinement.labels.map((label, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{label}</span>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeTab === 'mockups' && (
            <MockupsTab
              screenMockups={refinement.screen_mockups}
              expanded={expanded}
              onUpdate={async (mockups) => {
                await api.updateRefinement(refinementId, { screen_mockups: mockups });
                await loadRefinement();
              }}
            />
          )}
          {activeTab === 'knowledge' && <KnowledgeTab refinementId={refinementId} />}
          {activeTab === 'versions' && <VersionsTab refinementId={refinementId} />}
          {activeTab === 'history' && <HistoryTab refinementId={refinementId} />}
          {activeTab === 'qa' && <QATab refinementId={refinementId} mentionables={mentionables} />}

          {activeTab === 'specs' && (
            <div className="space-y-2">
              {(!refinement.specs || refinement.specs.length === 0) ? (
                <div className="text-center py-6">
                  <Link2 size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                  <p className="text-sm text-gray-500 dark:text-gray-400">No derived specs</p>
                  {canDeriveSpec && <p className="text-xs text-gray-400 mt-1">Use "Create Spec Draft" to start a structured spec from this refinement</p>}
                </div>
              ) : (
                refinement.specs.map((spec) => (
                  <div key={spec.id} className="flex items-center justify-between py-1.5 px-2 rounded bg-gray-50 dark:bg-gray-700/50">
                    <span className="text-sm text-gray-700 dark:text-gray-300 truncate">{spec.title}</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${SPEC_STATUS_COLORS[spec.status] || ''}`}>
                      {spec.status.replace('_', ' ')}
                    </span>
                  </div>
                ))
              )}
              {canDeriveSpec && (
                <button
                  onClick={handleDeriveSpec}
                  disabled={derivingSpec}
                  className="flex items-center gap-1.5 text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 mt-3"
                >
                  <Zap size={14} />
                  {derivingSpec ? 'Creating...' : 'Create Spec Draft'}
                </button>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete refinement
          </button>
          <div className="flex gap-2">
            {canDeriveSpec && (
              <button onClick={handleDeriveSpec} disabled={derivingSpec} className="btn btn-primary flex items-center gap-1.5">
                <Zap size={16} />
                {derivingSpec ? 'Creating...' : 'Create Spec Draft'}
              </button>
            )}
            <button onClick={onClose} className="btn btn-secondary">Close</button>
          </div>
        </div>
      </div>

      {/* Parent ideation modal */}
      {viewingIdeationId && (
        <IdeationModal
          ideationId={viewingIdeationId}
          boardId={_boardId}
          onClose={() => setViewingIdeationId(null)}
          onChanged={loadRefinement}
        />
      )}

      {/* Context selector for spec creation */}
      {showSpecSelector && refinement && (
        <ContextSelector
          title={refinement.title}
          description="Select which parts of the refinement to include in the spec draft context"
          items={buildRefinementItems(refinement)}
          targetLabel="Spec Draft"
          onConfirm={handleSpecSelectorConfirm}
          onCancel={() => setShowSpecSelector(false)}
        />
      )}
    </div>
  );
}
