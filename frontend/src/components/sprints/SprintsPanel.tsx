/**
 * SprintsPanel — Board-level list of all sprints across specs
 */

import { useEffect, useState } from 'react';
import { Layers, ChevronRight, Filter } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { SprintStatus, SpecSummary } from '@/types';
import { SPRINT_STATUS_LABELS, SPRINT_STATUS_COLORS, SPRINT_STATUSES } from '@/types';
import { SprintModal } from './SprintModal';

interface SprintsPanelProps {
  boardId: string;
}

export function SprintsPanel({ boardId }: SprintsPanelProps) {
  const api = useDashboardApi();
  const [sprints, setSprints] = useState<any[]>([]);
  const [specs, setSpecs] = useState<SpecSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [filterSpecId, setFilterSpecId] = useState<string>('');
  const [selectedSprintId, setSelectedSprintId] = useState<string | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createTitle, setCreateTitle] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createSpecId, setCreateSpecId] = useState('');

  useEffect(() => {
    loadSpecs();
  }, [boardId]);

  useEffect(() => {
    loadSprints();
  }, [boardId, filterStatus, filterSpecId]);

  const loadSpecs = async () => {
    try {
      const data = await api.listSpecs(boardId);
      setSpecs(data);
    } catch {
      // Specs list is best-effort for filtering
    }
  };

  const loadSprints = async () => {
    setLoading(true);
    try {
      const data = await api.listBoardSprints(
        boardId,
        filterStatus || undefined,
        filterSpecId || undefined,
      );
      setSprints(data);
    } catch {
      toast.error('Failed to load sprints');
    } finally {
      setLoading(false);
    }
  };

  const statusFilters = [
    { value: '', label: 'All' },
    ...SPRINT_STATUSES.map(s => ({ value: s, label: SPRINT_STATUS_LABELS[s] })),
  ];

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-bold text-gray-900 dark:text-white">Sprints</h2>
          <span className="text-sm text-gray-400">({sprints.length})</span>
        </div>
        <button
          onClick={() => setShowCreateForm(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-indigo-500 text-white hover:bg-indigo-600 transition-colors"
        >
          <Layers size={14} /> New Sprint
        </button>
      </div>

      {/* Create sprint form */}
      {showCreateForm && (
        <div className="mb-4 p-4 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl space-y-3">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Create Sprint</h3>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Spec *</label>
            <select
              value={createSpecId}
              onChange={(e) => setCreateSpecId(e.target.value)}
              className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700"
            >
              <option value="">Select a spec...</option>
              {specs.filter(s => ['validated', 'in_progress'].includes(s.status)).map(s => (
                <option key={s.id} value={s.id}>{s.title} ({s.status})</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Title *</label>
            <input
              value={createTitle}
              onChange={(e) => setCreateTitle(e.target.value)}
              placeholder="Sprint title"
              className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Description</label>
            <input
              value={createDescription}
              onChange={(e) => setCreateDescription(e.target.value)}
              placeholder="Optional description"
              className="w-full px-2.5 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button onClick={() => setShowCreateForm(false)} className="btn btn-secondary text-sm">Cancel</button>
            <button
              onClick={async () => {
                if (!createSpecId || !createTitle.trim()) { toast.error('Spec and title required'); return; }
                try {
                  await api.createSprint(boardId, createSpecId, { title: createTitle.trim(), description: createDescription.trim() || undefined, spec_id: createSpecId });
                  toast.success('Sprint created');
                  setShowCreateForm(false);
                  setCreateTitle('');
                  setCreateDescription('');
                  setCreateSpecId('');
                  loadSprints();
                } catch (e: any) { toast.error(e?.message || 'Failed to create sprint'); }
              }}
              disabled={!createSpecId || !createTitle.trim()}
              className="btn btn-primary text-sm disabled:opacity-50"
            >
              Create
            </button>
          </div>
        </div>
      )}

      {/* Filters row */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        {/* Status pills */}
        <div className="flex items-center gap-1 flex-wrap">
          {statusFilters.map(f => (
            <button
              key={f.value}
              onClick={() => setFilterStatus(f.value)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                filterStatus === f.value
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {/* Spec filter dropdown */}
        {specs.length > 0 && (
          <div className="flex items-center gap-1.5">
            <Filter size={13} className="text-gray-400" />
            <select
              value={filterSpecId}
              onChange={(e) => setFilterSpecId(e.target.value)}
              className="text-xs bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg px-2.5 py-1.5 border border-gray-200 dark:border-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              <option value="">All Specs</option>
              {specs.map(s => (
                <option key={s.id} value={s.id}>
                  {s.title.length > 50 ? s.title.substring(0, 50) + '...' : s.title}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Sprint list */}
      {loading ? (
        <div className="flex justify-center py-12">
          <Layers className="animate-pulse text-gray-400" size={24} />
        </div>
      ) : sprints.length === 0 ? (
        <div className="text-center py-12">
          <Layers size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
          <p className="text-gray-500 dark:text-gray-400">No sprints found</p>
          <p className="text-sm text-gray-400 mt-1">Sprints are created from within a Spec</p>
        </div>
      ) : (
        <div className="space-y-2">
          {sprints.map(sprint => (
            <div
              key={sprint.id}
              onClick={() => setSelectedSprintId(sprint.id)}
              className="flex items-center gap-3 p-4 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl hover:border-blue-300 dark:hover:border-blue-600 transition-colors cursor-pointer group"
            >
              <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium text-white ${SPRINT_STATUS_COLORS[sprint.status as SprintStatus]}`}>
                {SPRINT_STATUS_LABELS[sprint.status as SprintStatus]}
              </span>
              <div className="flex-1 min-w-0">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-white truncate">{sprint.title}</h3>
                {sprint.description && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">{sprint.description}</p>
                )}
                <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                  {sprint.spec && (
                    <span>Spec: {sprint.spec.title?.substring(0, 40)}{sprint.spec.title?.length > 40 ? '...' : ''}</span>
                  )}
                  <span>v{sprint.spec_version}</span>
                </div>
              </div>
              <div className="flex items-center gap-3 text-xs text-gray-400">
                {sprint.test_scenario_ids?.length > 0 && (
                  <span>{sprint.test_scenario_ids.length} tests</span>
                )}
                {sprint.labels?.length > 0 && (
                  <div className="flex gap-1">
                    {sprint.labels.slice(0, 2).map((l: string) => (
                      <span key={l} className="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-[10px]">{l}</span>
                    ))}
                  </div>
                )}
                <ChevronRight size={14} className="opacity-0 group-hover:opacity-100 transition-opacity" />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Sprint detail modal */}
      {selectedSprintId && (
        <SprintModal
          sprintId={selectedSprintId}
          onClose={() => { setSelectedSprintId(null); loadSprints(); }}
        />
      )}
    </div>
  );
}
