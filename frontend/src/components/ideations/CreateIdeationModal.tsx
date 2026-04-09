/**
 * CreateIdeationModal - Modal for creating a new ideation
 */

import { useState } from 'react';
import { X } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { Ideation } from '@/types';

interface CreateIdeationModalProps {
  boardId: string;
  onClose: () => void;
  onCreated: (ideation: Ideation) => void;
}

export function CreateIdeationModal({ boardId, onClose, onCreated }: CreateIdeationModalProps) {
  const api = useDashboardApi();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [problemStatement, setProblemStatement] = useState('');
  const [proposedApproach, setProposedApproach] = useState('');
  const [labels, setLabels] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    setSaving(true);
    try {
      const ideation = await api.createIdeation(boardId, {
        title: title.trim(),
        description: description.trim() || undefined,
        problem_statement: problemStatement.trim() || undefined,
        proposed_approach: proposedApproach.trim() || undefined,
        labels: labels ? labels.split(',').map((l) => l.trim()).filter(Boolean) : undefined,
      });
      onCreated(ideation);
      toast.success('Ideation created');
      onClose();
    } catch {
      toast.error('Failed to create ideation');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">New Ideation</h2>
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
              placeholder="What's the idea?"
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
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">What problem does this solve?</label>
            <textarea
              value={problemStatement}
              onChange={(e) => setProblemStatement(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              rows={3}
              placeholder="Describe the problem this ideation addresses..."
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">High-level approach</label>
            <textarea
              value={proposedApproach}
              onChange={(e) => setProposedApproach(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
              rows={3}
              placeholder="How would you approach solving this?"
            />
          </div>

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
            {saving ? 'Creating...' : 'Create Ideation'}
          </button>
        </div>
      </div>
    </div>
  );
}
