/**
 * GuidelinesPanel - Two-tab modal for managing board + global guidelines
 */

import { useState, useEffect, useCallback } from 'react';
import {
  X, Plus, Search, BookOpen, Link, Unlink, Trash2,
  ChevronUp, ChevronDown, Tag, Globe, FileText, Edit3, Eye, EyeOff,
  HelpCircle,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import toast from 'react-hot-toast';
import type { BoardGuidelineEntry, DefaultGuidelineCandidatesResponse, Guideline } from '@/types';

interface GuidelinesPanelProps {
  boardId: string;
  onClose: () => void;
}

type Tab = 'board' | 'global';

export function GuidelinesPanel({ boardId, onClose }: GuidelinesPanelProps) {
  const api = useDashboardApi();
  const [activeTab, setActiveTab] = useState<Tab>('global');

  // Board tab state
  const [entries, setEntries] = useState<BoardGuidelineEntry[]>([]);
  const [boardLoading, setBoardLoading] = useState(true);
  const [showInlineForm, setShowInlineForm] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Global tab state
  const [globals, setGlobals] = useState<Guideline[]>([]);
  const [globalLoading, setGlobalLoading] = useState(false);
  const [globalSearch, setGlobalSearch] = useState('');
  const [editingGlobal, setEditingGlobal] = useState<Guideline | null>(null);
  const [showGlobalForm, setShowGlobalForm] = useState(false);

  // Form state (shared between inline create, global create, and edit)
  const [formTitle, setFormTitle] = useState('');
  const [formContent, setFormContent] = useState('');
  const [formTags, setFormTags] = useState('');
  const [showHelp, setShowHelp] = useState(false);

  // Guideline default state, derived from the umbrella template (spec 8a2fad91 /
  // card 5cb88511). Only GLOBAL catalog guidelines are eligible defaults; the
  // Set-default action is blocked for inline guidelines (FR5/AC7).
  const [defaultInfo, setDefaultInfo] = useState<DefaultGuidelineCandidatesResponse | null>(null);

  const fetchDefaults = useCallback(async () => {
    try {
      const info = await api.listDefaultGuidelineCandidates();
      setDefaultInfo(info);
      return info;
    } catch {
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { fetchDefaults(); }, [fetchDefaults]);

  const isGuidelineDefault = (guidelineId: string) =>
    (defaultInfo?.candidates ?? []).some((c) => c.guideline_id === guidelineId && c.is_default);

  const toggleDefault = async (guidelineId: string) => {
    let templateId = defaultInfo?.template_id;
    const current = (defaultInfo?.candidates ?? [])
      .filter((c) => c.is_default)
      .map((c) => ({ guideline_id: c.guideline_id, priority: c.priority ?? 0 }));
    const already = current.some((r) => r.guideline_id === guidelineId);
    const refs = already
      ? current.filter((r) => r.guideline_id !== guidelineId)
      : [...current, { guideline_id: guidelineId, priority: current.reduce((m, r) => Math.max(m, r.priority ?? 0), 0) + 1 }];
    try {
      if (!templateId) {
        const template = await api.createDefaultBoardConfigVersion({ activate: true });
        templateId = template.id;
      }
      await api.updateDefaultGuidelineRefs(templateId, refs);
      setDefaultInfo((prev) => {
        if (!prev) return prev;
        const nextRef = refs.find((ref) => ref.guideline_id === guidelineId);
        return {
          ...prev,
          template_id: templateId ?? prev.template_id,
          candidates: prev.candidates.map((candidate) =>
            candidate.guideline_id === guidelineId
              ? {
                  ...candidate,
                  is_default: !already,
                  priority: nextRef?.priority ?? null,
                }
              : candidate,
          ),
        };
      });
      toast.success(already ? 'Removed from defaults' : 'Set as board default');
      await fetchDefaults();
    } catch { toast.error('Failed to update default'); }
  };

  const resetForm = () => { setFormTitle(''); setFormContent(''); setFormTags(''); };

  const parseTags = () => formTags.split(',').map(t => t.trim()).filter(Boolean);

  // ==================== BOARD TAB ====================

  const fetchBoard = useCallback(async () => {
    try {
      setBoardLoading(true);
      const data = await api.getBoardGuidelines(boardId);
      setEntries(data.sort((a, b) => a.priority - b.priority));
    } catch { toast.error('Failed to load board guidelines'); }
    finally { setBoardLoading(false); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  useEffect(() => { fetchBoard(); }, [fetchBoard]);

  const handleUnlink = async (entry: BoardGuidelineEntry) => {
    try {
      await api.unlinkGuidelineFromBoard(boardId, entry.guideline.id);
      toast.success('Guideline removed from board');
      fetchBoard();
    } catch { toast.error('Failed to remove'); }
  };

  const handleDeleteInline = async (entry: BoardGuidelineEntry) => {
    if (!confirm(`Delete "${entry.guideline.title}"? This cannot be undone.`)) return;
    try {
      await api.deleteGuideline(entry.guideline.id);
      toast.success('Guideline deleted');
      fetchBoard();
    } catch { toast.error('Failed to delete'); }
  };

  const handlePriority = async (entry: BoardGuidelineEntry, dir: 'up' | 'down') => {
    const sorted = [...entries].sort((a, b) => a.priority - b.priority);
    const idx = sorted.findIndex(e => e.id === entry.id);
    const swapIdx = dir === 'up' ? idx - 1 : idx + 1;
    if (swapIdx < 0 || swapIdx >= sorted.length) return;
    const other = sorted[swapIdx];
    try {
      await api.updateGuidelinePriority(boardId, entry.guideline.id, other.priority);
      await api.updateGuidelinePriority(boardId, other.guideline.id, entry.priority);
      fetchBoard();
    } catch { toast.error('Failed to reorder'); }
  };

  const handleCreateInline = async () => {
    if (!formTitle.trim() || !formContent.trim()) { toast.error('Title and content required'); return; }
    try {
      const tags = parseTags();
      await api.createInlineGuideline(boardId, { title: formTitle.trim(), content: formContent.trim(), tags: tags.length ? tags : undefined });
      toast.success('Inline guideline created');
      resetForm();
      setShowInlineForm(false);
      fetchBoard();
    } catch { toast.error('Failed to create'); }
  };

  const handleLink = async (id: string) => {
    try {
      await api.linkGuidelineToBoard(boardId, id);
      toast.success('Guideline linked');
      fetchBoard();
    } catch { toast.error('Failed to link'); }
  };

  const handleUnlinkByGuidelineId = async (guidelineId: string) => {
    try {
      await api.unlinkGuidelineFromBoard(boardId, guidelineId);
      toast.success('Guideline removed from board');
      fetchBoard();
    } catch { toast.error('Failed to remove'); }
  };

  // ==================== GLOBAL TAB ====================

  const fetchGlobals = useCallback(async () => {
    try {
      setGlobalLoading(true);
      const all = await api.listGuidelines(0, 200);
      setGlobals(all.filter(g => g.scope === 'global'));
    } catch { toast.error('Failed to load global guidelines'); }
    finally { setGlobalLoading(false); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { if (activeTab === 'global') fetchGlobals(); }, [activeTab, fetchGlobals]);

  const handleCreateGlobal = async () => {
    if (!formTitle.trim() || !formContent.trim()) { toast.error('Title and content required'); return; }
    try {
      const tags = parseTags();
      await api.createGuideline({ title: formTitle.trim(), content: formContent.trim(), tags: tags.length ? tags : undefined, scope: 'global' });
      toast.success('Global guideline created');
      resetForm();
      setShowGlobalForm(false);
      fetchGlobals();
    } catch { toast.error('Failed to create'); }
  };

  const handleUpdateGlobal = async () => {
    if (!editingGlobal || !formTitle.trim() || !formContent.trim()) { toast.error('Title and content required'); return; }
    try {
      const tags = parseTags();
      await api.updateGuideline(editingGlobal.id, { title: formTitle.trim(), content: formContent.trim(), tags: tags.length ? tags : [] });
      toast.success('Guideline updated (version bumped)');
      setEditingGlobal(null);
      resetForm();
      fetchGlobals();
    } catch { toast.error('Failed to update'); }
  };

  const handleDeleteGlobal = async (g: Guideline) => {
    if (!confirm(`Delete "${g.title}"? This will remove it from all linked boards.`)) return;
    try {
      await api.deleteGuideline(g.id);
      toast.success('Guideline deleted');
      fetchGlobals();
      fetchBoard(); // refresh board tab too
    } catch { toast.error('Failed to delete'); }
  };

  const openEditGlobal = (g: Guideline) => {
    setEditingGlobal(g);
    setFormTitle(g.title);
    setFormContent(g.content);
    setFormTags(g.tags?.join(', ') ?? '');
    setShowGlobalForm(false);
  };

  const filteredGlobals = globals.filter(g =>
    !globalSearch || g.title.toLowerCase().includes(globalSearch.toLowerCase())
  );

  // ==================== SHARED RENDERERS ====================

  const tagBadges = (tags: string[] | null) => {
    if (!tags?.length) return null;
    return (
      <div className="flex flex-wrap gap-1 mt-1">
        {tags.map(t => <span key={t} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"><Tag size={8} />{t}</span>)}
      </div>
    );
  };

  const guidelineForm = (onSave: () => void, onCancel: () => void, saveLabel: string) => (
    <div className="space-y-3 border border-gray-200 dark:border-gray-700 rounded-lg p-4 bg-white dark:bg-gray-800">
      <input type="text" value={formTitle} onChange={e => setFormTitle(e.target.value)} placeholder="Guideline title" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300" />
      <textarea value={formContent} onChange={e => setFormContent(e.target.value)} rows={8} placeholder="Content (Markdown supported)" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300 font-mono" />
      <input type="text" value={formTags} onChange={e => setFormTags(e.target.value)} placeholder="Tags (comma-separated)" className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-2 focus:ring-blue-300" />
      <div className="flex gap-2">
        <button onClick={onSave} className="btn btn-primary text-sm">{saveLabel}</button>
        <button onClick={onCancel} className="btn btn-secondary text-sm">Cancel</button>
      </div>
    </div>
  );

  const helpPanel = showHelp && (
    <section
      data-testid="guideline-help-examples"
      className="mb-4 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-950 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
    >
      <div className="mb-2 flex items-center gap-2 font-semibold">
        <HelpCircle size={15} />
        Assistant context examples
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Board workflow</div>
          <p className="mt-1 text-xs leading-5">
            Agents must read board guidelines before moving entities, request validator review at every review gate,
            and document blockers as comments with the responsible owner mentioned.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Engineering policy</div>
          <p className="mt-1 text-xs leading-5">
            Preserve existing architecture, keep changes scoped to the card objective, and run focused tests before validation.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Ownership</div>
          <p className="mt-1 text-xs leading-5">
            Use guidelines for repo boundaries, agent responsibilities, approval roles, and board-specific escalation rules.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Default usage</div>
          <p className="mt-1 text-xs leading-5">
            Mark global catalog guidelines as default when every new board should inherit that assistant context.
          </p>
        </div>
      </div>
    </section>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="flex h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <div className="flex items-center gap-2">
            <BookOpen size={20} className="text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Guidelines</h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowHelp((value) => !value)}
              data-testid="guideline-help-toggle"
              className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              <HelpCircle size={14} />
              Help
            </button>
            <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-[240px_minmax(0,1fr)]">
          <aside className="border-r border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-950/30">
            <nav className="space-y-1 text-sm">
              {([
                { id: 'global' as Tab, label: 'Global Catalog', icon: <Globe size={14} />, count: globals.length || defaultInfo?.candidates?.length || 0 },
                { id: 'board' as Tab, label: 'Board Guidelines', icon: <FileText size={14} />, count: entries.length },
              ]).map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 font-medium transition-colors ${
                    activeTab === tab.id
                      ? 'bg-white text-gray-900 shadow-sm ring-1 ring-gray-200 dark:bg-gray-800 dark:text-white dark:ring-gray-700'
                      : 'text-gray-600 hover:bg-white/70 dark:text-gray-400 dark:hover:bg-gray-800/60'
                  }`}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    {tab.icon}
                    <span className="truncate">{tab.label}</span>
                  </span>
                  <span className="shrink-0 rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                    {tab.count}
                  </span>
                </button>
              ))}
              <div className="mt-3 rounded-md border border-gray-200 bg-white px-3 py-2 text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
                <div className="font-medium text-gray-700 dark:text-gray-200">Default template</div>
                <div className="mt-0.5">
                  {defaultInfo?.template_version ? `v${defaultInfo.template_version}` : 'No active template'}
                </div>
              </div>
            </nav>
          </aside>

          {/* Body */}
          <main className="min-w-0 flex-1 overflow-y-auto p-6">
          {helpPanel}

          {/* ==================== BOARD TAB ==================== */}
          {activeTab === 'board' && (
            <div className="space-y-4">
              {/* Actions */}
              <div className="flex items-center gap-2">
                <button onClick={() => { resetForm(); setShowInlineForm(!showInlineForm); }} className="btn btn-secondary flex items-center gap-1 text-sm">
                  <Plus size={14} /> Create Inline
                </button>
              </div>

              {/* Inline create form */}
              {showInlineForm && guidelineForm(handleCreateInline, () => setShowInlineForm(false), 'Create Inline')}

              {/* Guidelines list */}
              {boardLoading ? (
                <div className="text-center py-8 text-gray-400">Loading...</div>
              ) : entries.length === 0 && !showInlineForm ? (
                <div className="text-center py-12 text-gray-400">
                  <BookOpen size={36} className="mx-auto mb-2 opacity-40" />
                  <p className="text-sm">No guidelines on this board</p>
                  <p className="text-xs mt-1">Use Global Catalog to link a global guideline, or create an inline one</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {entries.map((entry, idx) => {
                    const isGlobal = entry.scope === 'global';
                    const isExpanded = expandedId === entry.id;
                    return (
                      <div
                        key={entry.id}
                        className={`rounded-lg overflow-hidden border ${
                          isGlobal
                            ? 'border-blue-200 dark:border-blue-800 bg-blue-50/30 dark:bg-blue-900/10'
                            : 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50'
                        }`}
                      >
                        {/* Header row */}
                        <div className="flex items-center gap-2 px-3 py-2.5 cursor-pointer" onClick={() => setExpandedId(isExpanded ? null : entry.id)}>
                          {/* Priority */}
                          <div className="flex flex-col items-center gap-0.5 shrink-0" onClick={e => e.stopPropagation()}>
                            <button onClick={() => handlePriority(entry, 'up')} disabled={idx === 0} className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-20"><ChevronUp size={12} /></button>
                            <span className="text-[9px] text-gray-400 font-mono">{entry.priority}</span>
                            <button onClick={() => handlePriority(entry, 'down')} disabled={idx === entries.length - 1} className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-20"><ChevronDown size={12} /></button>
                          </div>

                          {/* Scope indicator */}
                          {isGlobal ? (
                            <Globe size={14} className="text-blue-500 shrink-0" />
                          ) : (
                            <FileText size={14} className="text-gray-400 shrink-0" />
                          )}

                          {/* Title */}
                          <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">{entry.guideline.title}</h3>

                          {/* Scope badge */}
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${
                            isGlobal ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                          }`}>
                            {isGlobal ? 'Global' : 'Inline'}
                          </span>

                          {/* Default state + Set-default action (blocked for inline, FR5/AC7) */}
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleDefault(entry.guideline.id); }}
                            disabled={!isGlobal}
                            title={isGlobal ? 'Toggle as a global default for new boards' : 'Inline guidelines cannot be defaults'}
                            data-testid={`guideline-set-default-${entry.guideline.id}`}
                            className={`text-[10px] px-1.5 py-0.5 rounded border shrink-0 disabled:opacity-40 disabled:cursor-not-allowed ${
                              isGuidelineDefault(entry.guideline.id)
                                ? 'bg-blue-500 text-white border-blue-500'
                                : 'text-gray-500 border-gray-300 dark:border-gray-600'
                            }`}
                          >
                            {isGuidelineDefault(entry.guideline.id) ? 'Default ✓' : 'Set default'}
                          </button>

                          {entry.guideline.version && entry.guideline.version > 1 && (
                            <span className="text-[10px] text-gray-400 shrink-0">v{entry.guideline.version}</span>
                          )}

                          {/* Toggle expand */}
                          {isExpanded ? <EyeOff size={12} className="text-gray-400 shrink-0" /> : <Eye size={12} className="text-gray-400 shrink-0" />}
                        </div>

                        {/* Expanded content */}
                        {isExpanded && (
                          <div className="px-4 pb-3 border-t border-gray-100 dark:border-gray-700/50 pt-2">
                            <MarkdownContent content={entry.guideline.content} />
                            {tagBadges(entry.guideline.tags)}
                            <div className="flex items-center gap-1 mt-3 pt-2 border-t border-gray-100 dark:border-gray-700/50">
                              {isGlobal ? (
                                <button onClick={() => handleUnlink(entry)} className="text-xs text-orange-500 hover:text-orange-600 flex items-center gap-1">
                                  <Unlink size={11} /> Unlink from board
                                </button>
                              ) : (
                                <button onClick={() => handleDeleteInline(entry)} className="text-xs text-red-500 hover:text-red-600 flex items-center gap-1">
                                  <Trash2 size={11} /> Delete
                                </button>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* ==================== GLOBAL TAB ==================== */}
          {activeTab === 'global' && (
            <div className="space-y-4">
              {/* Actions */}
              <div className="flex items-center gap-2">
                <button onClick={() => { resetForm(); setEditingGlobal(null); setShowGlobalForm(!showGlobalForm); }} className="btn btn-primary flex items-center gap-1 text-sm">
                  <Plus size={14} /> New Global Guideline
                </button>
                <div className="relative flex-1">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input type="text" value={globalSearch} onChange={e => setGlobalSearch(e.target.value)} placeholder="Search..." className="w-full pl-9 pr-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white outline-none" />
                </div>
              </div>

              {/* Create form */}
              {showGlobalForm && !editingGlobal && guidelineForm(handleCreateGlobal, () => setShowGlobalForm(false), 'Create Global')}

              {/* Edit form */}
              {editingGlobal && guidelineForm(handleUpdateGlobal, () => { setEditingGlobal(null); resetForm(); }, 'Save (bumps version)')}

              {/* List */}
              {globalLoading ? (
                <div className="text-center py-8 text-gray-400">Loading...</div>
              ) : filteredGlobals.length === 0 ? (
                <div className="text-center py-12 text-gray-400">
                  <Globe size={36} className="mx-auto mb-2 opacity-40" />
                  <p className="text-sm">{globalSearch ? 'No matching guidelines' : 'No global guidelines yet'}</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {filteredGlobals.map(g => {
                    const linkedToBoard = entries.some(e => e.guideline.id === g.id);
                    const isDefault = isGuidelineDefault(g.id);
                    return (
                      <div key={g.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 bg-white dark:bg-gray-800/50">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate">{g.title}</h3>
                              <span className="text-[10px] text-gray-400 shrink-0">v{g.version || 1}</span>
                              {linkedToBoard && <span className="text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">linked</span>}
                              {isDefault && <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 shrink-0">default</span>}
                            </div>
                            <p className="text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{g.content.slice(0, 150)}{g.content.length > 150 ? '...' : ''}</p>
                            {tagBadges(g.tags)}
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
                            {linkedToBoard ? (
                              <button
                                onClick={() => handleUnlinkByGuidelineId(g.id)}
                                className="inline-flex items-center gap-1 rounded border border-orange-200 px-2 py-1 text-[10px] text-orange-600 hover:bg-orange-50 dark:border-orange-800 dark:text-orange-300 dark:hover:bg-orange-900/20"
                                title="Unlink this guideline from the current board"
                                data-testid={`guideline-unlink-board-${g.id}`}
                              >
                                <Unlink size={11} />
                                Unlink
                              </button>
                            ) : (
                              <button
                                onClick={() => handleLink(g.id)}
                                className="inline-flex items-center gap-1 rounded border border-green-200 px-2 py-1 text-[10px] text-green-700 hover:bg-green-50 dark:border-green-800 dark:text-green-300 dark:hover:bg-green-900/20"
                                title="Link this guideline to the current board"
                                data-testid={`guideline-link-board-${g.id}`}
                              >
                                <Link size={11} />
                                Link
                              </button>
                            )}
                            <button
                              onClick={() => toggleDefault(g.id)}
                              title="Toggle as a global default for new boards"
                              data-testid={`guideline-set-default-${g.id}`}
                              className={`text-[10px] px-2 py-1 rounded border shrink-0 ${
                                isDefault
                                  ? 'bg-blue-500 text-white border-blue-500'
                                  : 'text-gray-500 border-gray-300 dark:border-gray-600'
                              }`}
                            >
                              {isDefault ? 'Default' : 'Set default'}
                            </button>
                            <button onClick={() => openEditGlobal(g)} className="p-1.5 text-gray-400 hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded" title="Edit">
                              <Edit3 size={14} />
                            </button>
                            <button onClick={() => handleDeleteGlobal(g)} className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded" title="Delete">
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          </main>
        </div>
      </div>
    </div>
  );
}
