/**
 * SprintsTab — Sprint roadmap inside SpecModal
 */

import { useEffect, useState } from 'react';
import { Plus, Layers, ChevronRight } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { SprintSummary } from '@/types';
import { SPRINT_STATUS_LABELS, SPRINT_STATUS_COLORS } from '@/types';
import { SprintModal } from '@/components/sprints/SprintModal';

interface SprintsTabProps {
  specId: string;
  boardId: string;
}

export function SprintsTab({ specId, boardId }: SprintsTabProps) {
  const api = useDashboardApi();
  const [sprints, setSprints] = useState<SprintSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [creating, setCreating] = useState(false);
  const [selectedSprintId, setSelectedSprintId] = useState<string | null>(null);

  const loadSprints = async () => {
    try {
      setLoading(true);
      const data = await api.listSprints(boardId, specId);
      setSprints(data);
    } catch {
      toast.error('Failed to load sprints');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadSprints(); }, [specId]);

  const handleCreate = async () => {
    if (!newTitle.trim()) return;
    setCreating(true);
    try {
      await api.createSprint(boardId, specId, {
        title: newTitle.trim(),
        description: newDescription.trim() || undefined,
        spec_id: specId,
      });
      toast.success('Sprint created');
      setNewTitle('');
      setNewDescription('');
      setShowCreate(false);
      loadSprints();
    } catch (e: any) {
      toast.error(e.message || 'Failed to create sprint');
    } finally {
      setCreating(false);
    }
  };

  // Progress summary
  const closed = sprints.filter(s => s.status === 'closed').length;
  const active = sprints.filter(s => s.status === 'active').length;
  const total = sprints.length;
  const progress = total > 0 ? Math.round((closed / total) * 100) : 0;

  if (loading) {
    return <div className="flex items-center justify-center py-12 text-gray-400"><Layers className="animate-pulse" size={24} /></div>;
  }

  return (
    <div className="space-y-4">
      {/* Progress Overview */}
      {total > 0 && (
        <div className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
          <div className="flex-1">
            <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
              <span>{closed}/{total} sprints closed</span>
              <span>{progress}%</span>
            </div>
            <div className="h-2 bg-gray-200 dark:bg-gray-600 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${progress}%`,
                  background: `linear-gradient(90deg, #22c55e ${progress}%, #3b82f6 ${progress}%)`,
                }}
              />
            </div>
          </div>
          {active > 0 && (
            <span className="text-xs px-2 py-1 bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded-full">
              {active} active
            </span>
          )}
        </div>
      )}

      {/* Sprint Cards */}
      {sprints.length === 0 ? (
        <div className="text-center py-8">
          <Layers size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No sprints yet</p>
          <p className="text-xs text-gray-400 mt-1">Create sprints to break this spec into incremental deliverables</p>
        </div>
      ) : (
        <div className="space-y-2">
          {sprints.map(sprint => (
            <div
              key={sprint.id}
              onClick={() => setSelectedSprintId(sprint.id)}
              className="flex items-center gap-3 p-3 bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-lg hover:border-blue-300 dark:hover:border-blue-600 transition-colors cursor-pointer"
            >
              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium text-white ${SPRINT_STATUS_COLORS[sprint.status]}`}>
                {SPRINT_STATUS_LABELS[sprint.status]}
              </span>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-white truncate">{sprint.title}</p>
                {sprint.description && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 truncate">{sprint.description}</p>
                )}
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                {sprint.test_scenario_ids && (
                  <span>{sprint.test_scenario_ids.length} tests</span>
                )}
                <span>v{sprint.spec_version}</span>
                <ChevronRight size={14} />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Sprint */}
      {showCreate ? (
        <div className="p-3 border border-blue-200 dark:border-blue-700 rounded-lg bg-blue-50/50 dark:bg-blue-900/20 space-y-2">
          <input
            type="text"
            value={newTitle}
            onChange={e => setNewTitle(e.target.value)}
            placeholder="Sprint title..."
            className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-blue-500 outline-none"
            autoFocus
          />
          <textarea
            value={newDescription}
            onChange={e => setNewDescription(e.target.value)}
            placeholder="Description (optional)..."
            className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-blue-500 outline-none resize-none"
            rows={2}
          />
          <div className="flex justify-end gap-2">
            <button onClick={() => setShowCreate(false)} className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">Cancel</button>
            <button
              onClick={handleCreate}
              disabled={!newTitle.trim() || creating}
              className="px-3 py-1.5 text-xs bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
            >
              {creating ? 'Creating...' : 'Create Sprint'}
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setShowCreate(true)}
          className="w-full flex items-center justify-center gap-1.5 py-2 text-sm text-blue-500 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-lg transition-colors"
        >
          <Plus size={14} />
          New Sprint
        </button>
      )}

      {selectedSprintId && (
        <SprintModal
          sprintId={selectedSprintId}
          onClose={() => { setSelectedSprintId(null); loadSprints(); }}
        />
      )}
    </div>
  );
}
