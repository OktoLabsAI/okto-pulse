/**
 * CreateSpecModal - Modal for creating a new spec
 */

import { useState } from 'react';
import { X, Plus, Trash2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { Spec } from '@/types';

interface CreateSpecModalProps {
  boardId: string;
  onClose: () => void;
  onCreated: (spec: Spec) => void;
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

export function CreateSpecModal({ boardId, onClose, onCreated }: CreateSpecModalProps) {
  const api = useDashboardApi();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [context, setContext] = useState('');
  const [funcReqs, setFuncReqs] = useState<string[]>([]);
  const [techReqs, setTechReqs] = useState<string[]>([]);
  const [acceptCriteria, setAcceptCriteria] = useState<string[]>([]);
  const [labels, setLabels] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    setSaving(true);
    try {
      const spec = await api.createSpec(boardId, {
        title: title.trim(),
        description: description.trim() || undefined,
        context: context.trim() || undefined,
        functional_requirements: funcReqs.length > 0 ? funcReqs : undefined,
        technical_requirements: techReqs.length > 0 ? techReqs : undefined,
        acceptance_criteria: acceptCriteria.length > 0 ? acceptCriteria : undefined,
        labels: labels ? labels.split(',').map((l) => l.trim()).filter(Boolean) : undefined,
      });
      onCreated(spec);
      toast.success('Spec created');
      onClose();
    } catch {
      toast.error('Failed to create spec');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">New Spec</h2>
          <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <form onSubmit={handleSubmit} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Title *</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              placeholder="What needs to be built?"
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
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Context</label>
            <textarea
              value={context}
              onChange={(e) => setContext(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              rows={2}
              placeholder="Business context — why does this exist?"
            />
          </div>

          <ListEditor
            label="Functional Requirements"
            placeholder="Add a functional requirement..."
            items={funcReqs}
            onChange={setFuncReqs}
          />

          <ListEditor
            label="Technical Requirements"
            placeholder="Add a technical constraint..."
            items={techReqs}
            onChange={setTechReqs}
          />

          <ListEditor
            label="Acceptance Criteria"
            placeholder="Add an acceptance criterion..."
            items={acceptCriteria}
            onChange={setAcceptCriteria}
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
            disabled={!title.trim() || saving}
            className="btn btn-primary"
          >
            {saving ? 'Creating...' : 'Create Spec'}
          </button>
        </div>
      </div>
    </div>
  );
}
