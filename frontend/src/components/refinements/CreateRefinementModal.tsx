/**
 * CreateRefinementModal - Modal for creating a new refinement
 */

import { useEffect, useState } from 'react';
import { X, Plus, Trash2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { Refinement, IdeationSummary } from '@/types';

interface CreateRefinementModalProps {
  boardId: string;
  ideationId?: string;
  onClose: () => void;
  onCreated: (refinement: Refinement) => void;
}

function ListEditor({
  label,
  placeholder,
  items,
  onChange,
}: {
  label: string;
  placeholder: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed) {
      onChange([...items, trimmed]);
      setDraft('');
    }
  };

  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
        {label}
      </label>
      <div className="space-y-1">
        {items.map((item, i) => (
          <div key={i} className="flex items-start gap-2 group">
            <span className="text-xs text-gray-400 mt-1.5 w-4 shrink-0">{i + 1}.</span>
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-700 rounded px-2 py-1">
              {item}
            </span>
            <button
              type="button"
              onClick={() => onChange(items.filter((_, idx) => idx !== i))}
              className="opacity-0 group-hover:opacity-100 p-1 text-red-400 hover:text-red-600 transition-opacity"
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>
      <div className="flex gap-2 mt-1">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          className="flex-1 input text-sm"
        />
        <button
          type="button"
          onClick={add}
          className="p-2 text-gray-500 hover:text-blue-600 dark:hover:text-blue-400"
        >
          <Plus size={16} />
        </button>
      </div>
    </div>
  );
}

export function CreateRefinementModal({ boardId, ideationId: preselectedIdeationId, onClose, onCreated }: CreateRefinementModalProps) {
  const api = useDashboardApi();
  const [ideations, setIdeations] = useState<IdeationSummary[]>([]);
  const [ideationId, setIdeationId] = useState(preselectedIdeationId || '');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [inScope, setInScope] = useState('');
  const [analysis, setAnalysis] = useState('');
  const [decisions, setDecisions] = useState<string[]>([]);
  const [labels, setLabels] = useState('');
  const [saving, setSaving] = useState(false);
  const [loadingIdeations, setLoadingIdeations] = useState(!preselectedIdeationId);

  useEffect(() => {
    if (!preselectedIdeationId) {
      loadIdeations();
    }
  }, [boardId]);

  const loadIdeations = async () => {
    setLoadingIdeations(true);
    try {
      const data = await api.listIdeations(boardId);
      setIdeations(data);
      if (data.length === 1) {
        setIdeationId(data[0].id);
      }
    } catch {
      toast.error('Failed to load ideations');
    } finally {
      setLoadingIdeations(false);
    }
  };

  const parsedInScope = inScope
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const inScopeValid = parsedInScope.length > 0;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !ideationId || !inScopeValid) return;

    setSaving(true);
    try {
      const refinement = await api.createRefinement(ideationId, {
        ideation_id: ideationId,
        title: title.trim(),
        description: description.trim() || undefined,
        in_scope: parsedInScope,
        analysis: analysis.trim() || undefined,
        decisions: decisions.length > 0 ? decisions : undefined,
        labels: labels ? labels.split(',').map((l) => l.trim()).filter(Boolean) : undefined,
      });
      onCreated(refinement);
      toast.success('Refinement created');
      onClose();
    } catch (err) {
      // Surface the backend validation message when the frontend guard is
      // bypassed somehow (e.g. direct API call from a test, or a race).
      const detail =
        err && typeof err === 'object' && 'detail' in err
          ? String((err as { detail: unknown }).detail)
          : 'Failed to create refinement';
      toast.error(detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">New Refinement</h2>
          <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <form onSubmit={handleSubmit} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {/* Ideation selector (if not pre-selected) */}
          {!preselectedIdeationId && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Ideation *</label>
              {loadingIdeations ? (
                <div className="text-sm text-gray-400">Loading ideations...</div>
              ) : ideations.length === 0 ? (
                <div className="text-sm text-gray-400">No ideations found. Create an ideation first.</div>
              ) : (
                <select
                  value={ideationId}
                  onChange={(e) => setIdeationId(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
                >
                  <option value="">Select an ideation...</option>
                  {ideations.map((ideation) => (
                    <option key={ideation.id} value={ideation.id}>
                      {ideation.title}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Title *</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              placeholder="What area does this refinement focus on?"
              autoFocus
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              rows={2}
              placeholder="High-level summary..."
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              In Scope (comma-separated) <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={inScope}
              onChange={(e) => setInScope(e.target.value)}
              required
              aria-invalid={!inScopeValid}
              className={`w-full px-3 py-2 border rounded-lg text-sm dark:bg-gray-700 ${
                inScopeValid
                  ? 'border-gray-300 dark:border-gray-600'
                  : 'border-red-400 dark:border-red-500/60'
              }`}
              placeholder="Auth flow, Token refresh, Session management"
            />
            {!inScopeValid && (
              <p className="mt-1 text-xs text-red-500">
                At least one non-empty in-scope item is required.
              </p>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Analysis</label>
            <textarea
              value={analysis}
              onChange={(e) => setAnalysis(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              rows={2}
              placeholder="Analysis and findings"
            />
          </div>

          <ListEditor
            label="Decisions"
            placeholder="Add a decision..."
            items={decisions}
            onChange={setDecisions}
          />

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Labels</label>
            <input
              type="text"
              value={labels}
              onChange={(e) => setLabels(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              placeholder="Comma-separated labels..."
            />
          </div>
        </form>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button type="button" onClick={onClose} className="btn btn-secondary">
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!title.trim() || !ideationId || !inScopeValid || saving}
            className="btn btn-primary"
          >
            {saving ? 'Creating...' : 'Create Refinement'}
          </button>
        </div>
      </div>
    </div>
  );
}
