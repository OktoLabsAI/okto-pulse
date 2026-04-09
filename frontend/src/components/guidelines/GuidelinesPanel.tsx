/**
 * GuidelinesPanel - Two-tab modal for managing board + global guidelines
 */

import { useState, useEffect, useCallback } from 'react';
import {
  X, Plus, Search, BookOpen, Link, Unlink, Trash2,
  ChevronUp, ChevronDown, Tag, Globe, FileText, Edit3, Eye, EyeOff,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import toast from 'react-hot-toast';
import type { BoardGuidelineEntry, Guideline } from '@/types';

interface GuidelinesPanelProps {
  boardId: string;
  onClose: () => void;
}

type Tab = 'board' | 'global';

export function GuidelinesPanel({ boardId, onClose }: GuidelinesPanelProps) {
  const api = useDashboardApi();
  const [activeTab, setActiveTab] = useState<Tab>('board');

  // Board tab state
  const [entries, setEntries] = useState<BoardGuidelineEntry[]>([]);
  const [boardLoading, setBoardLoading] = useState(true);
  const [showLinkModal, setShowLinkModal] = useState(false);
  const [showInlineForm, setShowInlineForm] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Global tab state
  const [globals, setGlobals] = useState<Guideline[]>([]);
  const [globalLoading, setGlobalLoading] = useState(false);
  const [globalSearch, setGlobalSearch] = useState('');
  const [editingGlobal, setEditingGlobal] = useState<Guideline | null>(null);
  const [showGlobalForm, setShowGlobalForm] = useState(false);

  // Link modal state
  const [linkSearch, setLinkSearch] = useState('');
  const [linkCandidates, setLinkCandidates] = useState<Guideline[]>([]);
  const [linkLoading, setLinkLoading] = useState(false);

  // Form state (shared between inline create, global create, and edit)
  const [formTitle, setFormTitle] = useState('');
  const [formContent, setFormContent] = useState('');
  const [formTags, setFormTags] = useState('');

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

  // ==================== LINK MODAL ====================

  const fetchLinkCandidates = async () => {
    try {
      setLinkLoading(true);
      const all = await api.listGuidelines(0, 100);
      const linkedIds = new Set(entries.map(e => e.guideline.id));
      setLinkCandidates(all.filter(g => g.scope === 'global' && !linkedIds.has(g.id)));
    } catch { toast.error('Failed to load'); }
    finally { setLinkLoading(false); }
  };

  useEffect(() => { if (showLinkModal) fetchLinkCandidates(); }, [showLinkModal]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleLink = async (id: string) => {
    try {
      await api.linkGuidelineToBoard(boardId, id);
      toast.success('Guideline linked');
      setLinkCandidates(prev => prev.filter(g => g.id !== id));
      fetchBoard();
    } catch { toast.error('Failed to link'); }
  };

  const filteredLinkCandidates = linkCandidates.filter(g =>
    !linkSearch || g.title.toLowerCase().includes(linkSearch.toLowerCase())
  );

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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white dark:bg-gray-900 rounded-xl shadow-2xl w-full max-w-3xl h-[90vh] flex flex-col border border-gray-200 dark:border-gray-700">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <div className="flex items-center gap-2">
            <BookOpen size={20} className="text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Guidelines</h2>
          </div>
          <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
            <X size={18} />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-200 dark:border-gray-700 px-6 shrink-0">
          {([
            { id: 'board' as Tab, label: 'Board Guidelines', icon: <FileText size={14} />, count: entries.length },
            { id: 'global' as Tab, label: 'Global Catalog', icon: <Globe size={14} />, count: globals.length },
          ]).map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                activeTab === tab.id
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'
              }`}
            >
              {tab.icon} {tab.label}
              {tab.count > 0 && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">{tab.count}</span>}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6">

          {/* ==================== BOARD TAB ==================== */}
          {activeTab === 'board' && (
            <div className="space-y-4">
              {/* Actions */}
              <div className="flex items-center gap-2">
                <button onClick={() => { setShowLinkModal(true); }} className="btn btn-secondary flex items-center gap-1 text-sm">
                  <Link size={14} /> Link Global
                </button>
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
                  <p className="text-xs mt-1">Link a global guideline or create an inline one</p>
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
                    const linkedBoards = entries.some(e => e.guideline.id === g.id);
                    return (
                      <div key={g.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 bg-white dark:bg-gray-800/50">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate">{g.title}</h3>
                              <span className="text-[10px] text-gray-400 shrink-0">v{g.version || 1}</span>
                              {linkedBoards && <span className="text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">linked</span>}
                            </div>
                            <p className="text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{g.content.slice(0, 150)}{g.content.length > 150 ? '...' : ''}</p>
                            {tagBadges(g.tags)}
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
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
        </div>

        {/* Link Modal overlay */}
        {showLinkModal && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/30 rounded-xl">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md mx-6 max-h-[60vh] flex flex-col">
              <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 shrink-0">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Link Global Guideline</h3>
                <button onClick={() => setShowLinkModal(false)} className="p-1 text-gray-400 hover:text-gray-600"><X size={16} /></button>
              </div>
              <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 shrink-0">
                <div className="relative">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input type="text" value={linkSearch} onChange={e => setLinkSearch(e.target.value)} placeholder="Search guidelines..." className="w-full pl-9 pr-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none" autoFocus />
                </div>
              </div>
              <div className="flex-1 overflow-y-auto p-4 space-y-1.5">
                {linkLoading ? (
                  <p className="text-center text-gray-400 py-4 text-sm">Loading...</p>
                ) : filteredLinkCandidates.length === 0 ? (
                  <p className="text-center text-gray-400 py-4 text-sm">{linkSearch ? 'No matching guidelines' : 'No unlinked global guidelines available'}</p>
                ) : (
                  filteredLinkCandidates.map(g => (
                    <div key={g.id} className="flex items-center justify-between p-2.5 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 border border-transparent hover:border-gray-200 dark:hover:border-gray-600">
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-white truncate">{g.title}</p>
                        <p className="text-[10px] text-gray-400">v{g.version || 1}</p>
                      </div>
                      <button onClick={() => handleLink(g.id)} className="btn btn-primary text-xs flex items-center gap-1 shrink-0 ml-2">
                        <Link size={10} /> Link
                      </button>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
