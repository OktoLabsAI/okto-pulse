/**
 * CardKnowledgeTab — Knowledge bases for a card/task.
 * Two options: add a new KB or link KBs from the parent spec.
 */

import { useState } from 'react';
import { BookOpen, Plus, Link2, X, ChevronDown, ChevronUp, Eye, Code, Pencil, Download, Save } from 'lucide-react';
import toast from 'react-hot-toast';
import type { Card } from '@/types';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

interface CardKnowledgeTabProps {
  card: Card;
  specKnowledgeBases: { id: string; title: string; description?: string; content: string; mime_type?: string }[];
  onUpdate: (kbs: any[]) => Promise<void>;
}

export function CardKnowledgeTab({ card, specKnowledgeBases, onUpdate }: CardKnowledgeTabProps) {
  const cardKBs: any[] = card.knowledge_bases || [];
  const [showAddForm, setShowAddForm] = useState(false);
  const [showLinkPicker, setShowLinkPicker] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Edit-in-place state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editContent, setEditContent] = useState('');

  // Add new KB form
  const [newTitle, setNewTitle] = useState('');
  const [newContent, setNewContent] = useState('');
  const newMimeType = 'text/markdown';
  const [previewMode, setPreviewMode] = useState(false);
  const [saving, setSaving] = useState(false);

  const startEdit = (kb: any) => {
    setEditingId(kb.id);
    setEditTitle(kb.title || '');
    setEditContent(kb.content || '');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditTitle('');
    setEditContent('');
  };

  const saveEdit = async () => {
    if (!editingId) return;
    if (!editTitle.trim() || !editContent.trim()) return;
    setSaving(true);
    try {
      const next = cardKBs.map((kb) =>
        kb.id === editingId ? { ...kb, title: editTitle.trim(), content: editContent } : kb,
      );
      await onUpdate(next);
      toast.success('Knowledge base updated');
      cancelEdit();
    } finally { setSaving(false); }
  };

  const downloadMarkdown = (kb: any) => {
    const safeTitle = (kb.title || 'knowledge').replace(/[^A-Za-z0-9._-]+/g, '_');
    const filename = `${safeTitle || 'knowledge'}.md`;
    const body = `# ${kb.title || ''}\n\n> ${kb.description || ''}\n\n${kb.content || ''}\n`;
    const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const handleAddNew = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    setSaving(true);
    try {
      const kb = {
        id: `kb_${Date.now()}`,
        title: newTitle.trim(),
        description: null,
        content: newContent,
        mime_type: newMimeType,
        source: 'manual',
      };
      await onUpdate([...cardKBs, kb]);
      setNewTitle('');
      setNewContent('');
      setShowAddForm(false);
      setPreviewMode(false);
      toast.success('Knowledge base added');
    } finally { setSaving(false); }
  };

  const handleLinkFromSpec = async (specKB: typeof specKnowledgeBases[0]) => {
    // Check if already linked
    if (cardKBs.some(kb => kb.id === specKB.id || kb.source_id === specKB.id)) {
      toast.error('Already linked');
      return;
    }
    const kb = {
      id: `kb_${Date.now()}`,
      source_id: specKB.id,
      title: specKB.title,
      description: specKB.description || null,
      content: specKB.content,
      mime_type: specKB.mime_type || 'text/markdown',
      source: 'spec',
    };
    await onUpdate([...cardKBs, kb]);
    toast.success(`Linked "${specKB.title}" from spec`);
  };

  const handleDelete = async (id: string) => {
    await onUpdate(cardKBs.filter(kb => kb.id !== id));
  };

  return (
    <div className="modal-body space-y-4">
      {/* Action buttons */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => { setShowAddForm(true); setShowLinkPicker(false); }}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-900/20 rounded-lg hover:bg-indigo-100 dark:hover:bg-indigo-900/40"
        >
          <Plus size={13} /> New KB
        </button>
        {specKnowledgeBases.length > 0 && (
          <button
            onClick={() => { setShowLinkPicker(true); setShowAddForm(false); }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/20 rounded-lg hover:bg-emerald-100 dark:hover:bg-emerald-900/40"
          >
            <Link2 size={13} /> Link from Spec ({specKnowledgeBases.length})
          </button>
        )}
      </div>

      {/* Add new KB form */}
      {showAddForm && (
        <div className="border border-indigo-200 dark:border-indigo-800 rounded-lg p-3 space-y-3 bg-indigo-50/30 dark:bg-indigo-900/10">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">New Knowledge Base</h4>
            <button onClick={() => setShowAddForm(false)} className="text-gray-400 hover:text-gray-600"><X size={14} /></button>
          </div>
          <input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="Title"
            className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
          />
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs text-gray-500">Content (Markdown)</span>
              <div className="flex items-center gap-1 border border-gray-200 dark:border-gray-700 rounded p-0.5">
                <button onClick={() => setPreviewMode(false)} className={`p-0.5 rounded text-xs ${!previewMode ? 'bg-gray-200 dark:bg-gray-700' : ''}`}><Code size={11} /></button>
                <button onClick={() => setPreviewMode(true)} className={`p-0.5 rounded text-xs ${previewMode ? 'bg-gray-200 dark:bg-gray-700' : ''}`}><Eye size={11} /></button>
              </div>
            </div>
            {previewMode ? (
              <div className="p-3 border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 min-h-[120px] text-sm">
                <MarkdownContent content={newContent} />
              </div>
            ) : (
              <textarea
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                rows={6}
                placeholder="Write your knowledge base content here..."
                className="w-full px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 resize-y"
              />
            )}
          </div>
          <div className="flex justify-end gap-2">
            <button onClick={() => setShowAddForm(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAddNew} disabled={!newTitle.trim() || !newContent.trim() || saving} className="btn btn-primary text-xs disabled:opacity-50">
              {saving ? 'Saving...' : 'Add'}
            </button>
          </div>
        </div>
      )}

      {/* Link from spec picker */}
      {showLinkPicker && (
        <div className="border border-emerald-200 dark:border-emerald-800 rounded-lg p-3 space-y-2 bg-emerald-50/30 dark:bg-emerald-900/10">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">Link KB from Spec</h4>
            <button onClick={() => setShowLinkPicker(false)} className="text-gray-400 hover:text-gray-600"><X size={14} /></button>
          </div>
          {specKnowledgeBases.map((kb) => {
            const alreadyLinked = cardKBs.some(c => c.source_id === kb.id);
            return (
              <div key={kb.id} className="flex items-center justify-between p-2 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{kb.title}</p>
                  {kb.description && <p className="text-xs text-gray-400 truncate">{kb.description}</p>}
                </div>
                <button
                  onClick={() => handleLinkFromSpec(kb)}
                  disabled={alreadyLinked}
                  className={`text-xs px-2 py-1 rounded ${alreadyLinked ? 'text-gray-400 cursor-not-allowed' : 'text-emerald-600 hover:bg-emerald-50 dark:hover:bg-emerald-900/30'}`}
                >
                  {alreadyLinked ? 'Linked' : '+ Link'}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* KB list */}
      {cardKBs.length === 0 && !showAddForm && !showLinkPicker ? (
        <div className="text-center py-8">
          <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge bases</p>
          <p className="text-xs text-gray-400 mt-1">Add new or link from the parent spec</p>
        </div>
      ) : (
        <div className="space-y-2">
          {cardKBs.map((kb: any) => (
            <div key={kb.id} data-testid={`kb-row-${kb.id}`} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
              <div
                className="flex items-center gap-2 p-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                onClick={() => setExpandedId(expandedId === kb.id ? null : kb.id)}
              >
                <BookOpen size={14} className="text-gray-400 shrink-0" />
                <span className="text-sm font-medium text-gray-800 dark:text-gray-200 flex-1 truncate">{kb.title}</span>
                {kb.source === 'spec' && (
                  <span className="text-[9px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 rounded">from spec</span>
                )}
                <span className="text-[9px] text-gray-400">{kb.mime_type || 'text/markdown'}</span>
                <button
                  onClick={(e) => { e.stopPropagation(); startEdit(kb); }}
                  className="text-gray-400 hover:text-indigo-600 p-0.5"
                  aria-label="Edit"
                  data-testid={`kb-edit-${kb.id}`}
                >
                  <Pencil size={12} />
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); downloadMarkdown(kb); }}
                  className="text-gray-400 hover:text-emerald-600 p-0.5"
                  aria-label="Download markdown"
                  data-testid={`kb-download-${kb.id}`}
                >
                  <Download size={12} />
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDelete(kb.id); }}
                  className="text-gray-400 hover:text-red-500 p-0.5"
                  aria-label="Delete"
                  data-testid={`kb-delete-${kb.id}`}
                >
                  <X size={12} />
                </button>
                {expandedId === kb.id ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
              </div>
              {editingId === kb.id ? (
                <div className="px-3 pb-3 space-y-2 border-t border-gray-100 dark:border-gray-700 bg-indigo-50/30 dark:bg-indigo-900/10">
                  <input
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
                    data-testid={`kb-edit-title-${kb.id}`}
                  />
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    rows={6}
                    className="w-full px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 resize-y"
                    data-testid={`kb-edit-content-${kb.id}`}
                  />
                  <div className="flex justify-end gap-2">
                    <button onClick={cancelEdit} className="btn btn-secondary text-xs">Cancel</button>
                    <button
                      onClick={saveEdit}
                      disabled={!editTitle.trim() || !editContent.trim() || saving}
                      className="btn btn-primary text-xs disabled:opacity-50 inline-flex items-center gap-1"
                      data-testid={`kb-edit-save-${kb.id}`}
                    >
                      <Save size={11} /> {saving ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                </div>
              ) : expandedId === kb.id && (
                <div className="px-3 pb-3 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/30">
                  <div className="pt-2 text-sm prose dark:prose-invert max-w-none">
                    <MarkdownContent content={kb.content} />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
