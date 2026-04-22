/**
 * MockupsTab — Renders screen mockups as HTML iframes with Tailwind CDN.
 * Supports viewing existing mockups and creating new ones via HTML editor.
 */

import { useState } from 'react';
import { Monitor, Smartphone, MessageSquare, Plus, X, Eye, Code } from 'lucide-react';
import type { ScreenMockup } from '@/types';

function sanitizeHtml(html: string): string {
  // Strip <script> tags and on* event handlers
  let clean = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
  clean = clean.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  return clean;
}

function buildSrcDoc(html: string): string {
  return `<!DOCTYPE html><html><head><script src="https://cdn.tailwindcss.com"><\/script></head><body class="p-4 bg-white">${sanitizeHtml(html)}</body></html>`;
}

const SCREEN_TYPES = [
  { value: 'page', label: 'Page' },
  { value: 'modal', label: 'Modal' },
  { value: 'drawer', label: 'Drawer' },
  { value: 'popover', label: 'Popover' },
  { value: 'panel', label: 'Panel' },
] as const;

interface MockupsTabProps {
  screenMockups: ScreenMockup[] | null;
  expanded?: boolean;
  onUpdate?: (mockups: ScreenMockup[]) => Promise<void>;
}

export function MockupsTab({ screenMockups, expanded = false, onUpdate }: MockupsTabProps) {
  const screens = (screenMockups || []).sort((a, b) => a.order - b.order);
  const [selectedId, setSelectedId] = useState<string>(screens[0]?.id || '');
  const [viewMode, setViewMode] = useState<'desktop' | 'mobile'>('desktop');
  const selected = screens.find((s) => s.id === selectedId);

  // Create form state
  const [showForm, setShowForm] = useState(false);
  const [formTitle, setFormTitle] = useState('');
  const [formDescription, setFormDescription] = useState('');
  const [formType, setFormType] = useState<ScreenMockup['screen_type']>('page');
  const [formHtml, setFormHtml] = useState('');
  const [formPreview, setFormPreview] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleCreate = async () => {
    if (!formTitle.trim() || !formHtml.trim() || !onUpdate) return;
    setSaving(true);
    try {
      const newMockup: ScreenMockup = {
        id: `mockup_${Date.now()}`,
        title: formTitle.trim(),
        description: formDescription.trim() || null,
        screen_type: formType,
        html_content: formHtml,
        annotations: null,
        order: screens.length,
      };
      await onUpdate([...screens, newMockup]);
      setSelectedId(newMockup.id);
      setShowForm(false);
      setFormTitle('');
      setFormDescription('');
      setFormType('page');
      setFormHtml('');
      setFormPreview(false);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!onUpdate) return;
    if (!confirm('Delete this mockup?')) return;
    const updated = screens.filter((s) => s.id !== id);
    await onUpdate(updated);
    if (selectedId === id) {
      setSelectedId(updated[0]?.id || '');
    }
  };

  // Empty state
  if (screens.length === 0 && !showForm) {
    return (
      <div className="text-center py-12">
        <Monitor size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No screen mockups yet</p>
        {onUpdate && (
          <button
            onClick={() => setShowForm(true)}
            className="mt-3 text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 flex items-center gap-1 mx-auto"
          >
            <Plus size={14} /> Add Mockup
          </button>
        )}
      </div>
    );
  }

  // Create form
  if (showForm) {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">New Mockup</h4>
          <button onClick={() => setShowForm(false)} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded">
            <X size={16} />
          </button>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Title *</label>
            <input
              value={formTitle}
              onChange={(e) => setFormTitle(e.target.value)}
              placeholder="e.g. Login Page"
              className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Type</label>
            <select
              value={formType}
              onChange={(e) => setFormType(e.target.value as ScreenMockup['screen_type'])}
              className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
            >
              {SCREEN_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Description</label>
          <input
            value={formDescription}
            onChange={(e) => setFormDescription(e.target.value)}
            placeholder="Brief description of this screen"
            className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
          />
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">HTML Content * <span className="font-normal text-gray-400">(Tailwind CSS available)</span></label>
            <div className="flex items-center gap-1 border border-gray-200 dark:border-gray-700 rounded p-0.5">
              <button
                onClick={() => setFormPreview(false)}
                className={`p-1 rounded text-xs flex items-center gap-1 ${!formPreview ? 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300' : 'text-gray-400'}`}
              >
                <Code size={12} /> Code
              </button>
              <button
                onClick={() => setFormPreview(true)}
                className={`p-1 rounded text-xs flex items-center gap-1 ${formPreview ? 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300' : 'text-gray-400'}`}
              >
                <Eye size={12} /> Preview
              </button>
            </div>
          </div>

          {formPreview ? (
            <iframe
              srcDoc={buildSrcDoc(formHtml)}
              sandbox="allow-same-origin allow-scripts"
              className="w-full border border-gray-200 dark:border-gray-700 rounded-lg"
              style={{ height: '300px' }}
              title="Mockup preview"
            />
          ) : (
            <textarea
              value={formHtml}
              onChange={(e) => setFormHtml(e.target.value)}
              placeholder='<div class="max-w-md mx-auto bg-white rounded-xl shadow-md p-6">&#10;  <h1 class="text-2xl font-bold text-gray-900">Title</h1>&#10;  <p class="text-gray-500 mt-2">Description</p>&#10;</div>'
              rows={12}
              className="w-full px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-600 rounded-lg bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100 resize-y"
            />
          )}
        </div>

        <div className="flex justify-end gap-2">
          <button onClick={() => setShowForm(false)} className="btn btn-secondary text-sm">Cancel</button>
          <button
            onClick={handleCreate}
            disabled={!formTitle.trim() || !formHtml.trim() || saving}
            className="btn btn-primary text-sm disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Add Mockup'}
          </button>
        </div>
      </div>
    );
  }

  // View mode
  const sanitizedHtml = selected ? sanitizeHtml(selected.html_content) : '';
  const srcDoc = buildSrcDoc(sanitizedHtml);

  return (
    <div className="space-y-3">
      {/* Screen selector + controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1 flex-wrap">
          {screens.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelectedId(s.id)}
              className={`px-2.5 py-1 rounded text-xs flex items-center gap-1.5 transition-colors ${
                s.id === selectedId
                  ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 font-medium'
                  : 'bg-gray-100 dark:bg-gray-800 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
              }`}
            >
              {s.title}
              <span className="text-[9px] text-gray-400">{s.screen_type}</span>
            </button>
          ))}
          {onUpdate && (
            <button
              onClick={() => setShowForm(true)}
              className="px-2 py-1 rounded text-xs text-indigo-500 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 flex items-center gap-1"
            >
              <Plus size={12} /> Add
            </button>
          )}
        </div>
        <div className="flex items-center gap-1">
          {onUpdate && selected && (
            <button
              onClick={() => handleDelete(selected.id)}
              className="p-1 text-gray-400 hover:text-red-500 rounded mr-1"
              title="Delete mockup"
            >
              <X size={14} />
            </button>
          )}
          <div className="flex items-center gap-1 border border-gray-200 dark:border-gray-700 rounded-lg p-0.5">
            <button
              onClick={() => setViewMode('desktop')}
              className={`p-1 rounded ${viewMode === 'desktop' ? 'bg-gray-200 dark:bg-gray-700' : ''}`}
              title="Desktop view"
            >
              <Monitor size={14} className={viewMode === 'desktop' ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400'} />
            </button>
            <button
              onClick={() => setViewMode('mobile')}
              className={`p-1 rounded ${viewMode === 'mobile' ? 'bg-gray-200 dark:bg-gray-700' : ''}`}
              title="Mobile view"
            >
              <Smartphone size={14} className={viewMode === 'mobile' ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400'} />
            </button>
          </div>
        </div>
      </div>

      {/* Iframe viewport */}
      {selected && (
        <div className={`mx-auto transition-all ${viewMode === 'mobile' ? 'max-w-sm' : 'w-full'}`}>
          {selected.description && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2 italic">{selected.description}</p>
          )}
          <iframe
            srcDoc={srcDoc}
            sandbox="allow-same-origin allow-scripts"
            className="w-full border border-gray-200 dark:border-gray-700 rounded-lg"
            style={{ height: expanded ? '70vh' : '400px' }}
            title={selected.title}
          />
        </div>
      )}

      {/* Annotations */}
      {selected?.annotations && selected.annotations.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Annotations</p>
          {selected.annotations.map((a) => (
            <div key={a.id} className="flex items-start gap-1.5 text-[10px] text-gray-500 dark:text-gray-400">
              <MessageSquare size={10} className="shrink-0 mt-0.5 text-amber-400" />
              <span>{a.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
