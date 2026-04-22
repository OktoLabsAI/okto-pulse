/**
 * SprintSuggestionModal — Appears when Validate is clicked on a spec with many tasks.
 * Shows AI-suggested sprint breakdown. User can Skip, Edit, or Accept & Create.
 */

import { useState } from 'react';
import { X, Layers, Pencil, Check, SkipForward } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';

interface Suggestion {
  title: string;
  description: string;
  card_ids: string[];
  card_titles: string[];
  test_scenario_ids: string[] | null;
  business_rule_ids: string[] | null;
}

interface SprintSuggestionModalProps {
  boardId: string;
  specId: string;
  suggestions: Suggestion[];
  onClose: () => void;
  onSkip: () => void;
  onCreated: () => void;
}

export function SprintSuggestionModal({
  boardId, specId, suggestions: initialSuggestions, onClose, onSkip, onCreated,
}: SprintSuggestionModalProps) {
  const api = useDashboardApi();
  const [suggestions, setSuggestions] = useState<Suggestion[]>(initialSuggestions);
  const [creating, setCreating] = useState(false);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);

  const handleTitleChange = (idx: number, title: string) => {
    const updated = [...suggestions];
    updated[idx] = { ...updated[idx], title };
    setSuggestions(updated);
  };

  const handleAccept = async () => {
    setCreating(true);
    try {
      for (const suggestion of suggestions) {
        const sprint = await api.createSprint(boardId, specId, {
          title: suggestion.title,
          description: suggestion.description,
          spec_id: specId,
          test_scenario_ids: suggestion.test_scenario_ids || undefined,
          business_rule_ids: suggestion.business_rule_ids || undefined,
        });
        // Assign cards to the created sprint
        if (sprint?.id && suggestion.card_ids.length > 0) {
          // Use update_card for each card to set sprint_id
          for (const cardId of suggestion.card_ids) {
            try {
              await api.updateCard(cardId, { sprint_id: sprint.id });
            } catch {
              // Card may not exist or other issue, continue
            }
          }
        }
      }
      toast.success(`${suggestions.length} sprint(s) created`);
      onCreated();
    } catch (e: any) {
      toast.error(e.message || 'Failed to create sprints');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60] p-4">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2">
            <Layers size={20} className="text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Sprint Suggestion</h2>
          </div>
          <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded-lg">
            <X size={16} />
          </button>
        </div>

        <div className="px-6 py-3 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-100 dark:border-blue-900/30">
          <p className="text-sm text-blue-700 dark:text-blue-300">
            This spec has {suggestions.reduce((acc, s) => acc + s.card_ids.length, 0)} tasks.
            We suggest breaking them into {suggestions.length} sprint(s) for incremental delivery.
          </p>
        </div>

        {/* Suggestions */}
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          {suggestions.map((s, i) => (
            <div key={i} className="border border-gray-200 dark:border-gray-600 rounded-lg overflow-hidden">
              <div className="flex items-center justify-between px-4 py-3 bg-gray-50 dark:bg-gray-700/50">
                {editingIdx === i ? (
                  <input
                    type="text"
                    value={s.title}
                    onChange={e => handleTitleChange(i, e.target.value)}
                    onBlur={() => setEditingIdx(null)}
                    onKeyDown={e => e.key === 'Enter' && setEditingIdx(null)}
                    className="flex-1 px-2 py-1 text-sm font-medium border border-blue-300 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-blue-500 outline-none"
                    autoFocus
                  />
                ) : (
                  <span className="text-sm font-medium text-gray-900 dark:text-white">{s.title}</span>
                )}
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-500">{s.card_ids.length} tasks</span>
                  <button
                    onClick={() => setEditingIdx(editingIdx === i ? null : i)}
                    className="p-1 text-gray-400 hover:text-blue-500 rounded"
                  >
                    <Pencil size={12} />
                  </button>
                </div>
              </div>
              <div className="px-4 py-2 space-y-1">
                {s.card_titles.map((title, j) => (
                  <div key={j} className="text-xs text-gray-600 dark:text-gray-400 flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-gray-300 dark:bg-gray-500 shrink-0" />
                    <span className="truncate">{title}</span>
                  </div>
                ))}
              </div>
              {(s.test_scenario_ids?.length || s.business_rule_ids?.length) ? (
                <div className="px-4 py-2 border-t border-gray-100 dark:border-gray-600 flex gap-3 text-[10px] text-gray-400">
                  {s.test_scenario_ids && <span>{s.test_scenario_ids.length} test scenarios</span>}
                  {s.business_rule_ids && <span>{s.business_rule_ids.length} business rules</span>}
                </div>
              ) : null}
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-700 flex justify-between items-center">
          <button
            onClick={onSkip}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
          >
            <SkipForward size={14} />
            Skip (no sprints)
          </button>
          <button
            onClick={handleAccept}
            disabled={creating}
            className="flex items-center gap-1.5 px-4 py-2 text-sm bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
          >
            <Check size={14} />
            {creating ? 'Creating...' : `Accept & Create ${suggestions.length} Sprint(s)`}
          </button>
        </div>
      </div>
    </div>
  );
}
