/**
 * RefinementsPanel - List of refinements across all ideations for a board
 */

import { useEffect, useState } from 'react';
import {
  Plus,
  Layers,
  Clock,
  CheckCircle2,
  Ban,
  FileText,
  ChevronRight,
  Archive,
  ArchiveRestore,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { IdeationSummary, RefinementSummary, RefinementStatus } from '@/types';
import { REFINEMENT_STATUS_LABELS } from '@/types';
import { useListSearch } from '@/hooks/useListSearch';
import { SearchInput } from '@/components/shared/SearchInput';
import { useViewMode } from '@/hooks/useViewMode';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { CreateRefinementModal } from './CreateRefinementModal';
import { RefinementModal } from './RefinementModal';

interface RefinementsPanelProps {
  boardId: string;
}

const STATUS_ICON: Record<RefinementStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<RefinementStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

interface GroupedRefinement {
  refinement: RefinementSummary;
  ideationTitle: string;
}

export function RefinementsPanel({ boardId }: RefinementsPanelProps) {
  const api = useDashboardApi();
  const [groups, setGroups] = useState<Map<string, { ideation: IdeationSummary; refinements: RefinementSummary[] }>>(new Map());
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedRefinementId, setSelectedRefinementId] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => {
    loadRefinements();
  }, [boardId, showArchived]);

  const loadRefinements = async () => {
    setLoading(true);
    try {
      const ideations = await api.listIdeations(boardId, undefined, showArchived);
      const groupMap = new Map<string, { ideation: IdeationSummary; refinements: RefinementSummary[] }>();

      // For each ideation, load full details to get refinements
      const details = await Promise.all(
        ideations.map((ideation) => api.getIdeation(ideation.id))
      );

      for (let i = 0; i < ideations.length; i++) {
        const ideation = ideations[i];
        const detail = details[i];
        if (detail.refinements && detail.refinements.length > 0) {
          groupMap.set(ideation.id, {
            ideation,
            refinements: detail.refinements,
          });
        }
      }

      setGroups(groupMap);
    } catch {
      toast.error('Failed to load refinements');
    } finally {
      setLoading(false);
    }
  };

  const statusFilters = [
    { value: '', label: 'All' },
    { value: 'draft', label: 'Draft' },
    { value: 'review', label: 'Review' },
    { value: 'approved', label: 'Approved' },
    { value: 'done', label: 'Done' },
  ];

  // Flatten and filter
  const allRefinements: GroupedRefinement[] = [];
  groups.forEach(({ ideation, refinements }) => {
    for (const refinement of refinements) {
      if (!showArchived && refinement.archived) continue;
      if (!filterStatus || refinement.status === filterStatus) {
        allRefinements.push({ refinement, ideationTitle: ideation.title });
      }
    }
  });

  const { viewMode, setViewMode } = useViewMode('refinements', 'list');
  const search = useListSearch<GroupedRefinement>(allRefinements, {
    matcher: (it, q) => {
      const needle = q.toLowerCase();
      const r = it.refinement;
      return (
        (r.title || '').toLowerCase().includes(needle) ||
        (r.description || '').toLowerCase().includes(needle) ||
        (r.labels || []).some((l) => (l || '').toLowerCase().includes(needle)) ||
        (it.ideationTitle || '').toLowerCase().includes(needle)
      );
    },
    urlParam: 'q_refinements',
  });

  // Group by ideation for display
  const displayGroups = new Map<string, { ideationTitle: string; refinements: GroupedRefinement[] }>();
  for (const item of search.filtered) {
    const key = item.refinement.ideation_id;
    if (!displayGroups.has(key)) {
      displayGroups.set(key, { ideationTitle: item.ideationTitle, refinements: [] });
    }
    displayGroups.get(key)!.refinements.push(item);
  }

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Refinements</h2>
          <span className="text-sm text-gray-400">
            ({search.filtered.length}
            {search.query ? ` of ${allRefinements.length}` : ''})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={search.query}
            onChange={search.setQuery}
            placeholder="Search refinements…"
            testId="refinements-search"
          />
          <ViewModeToggle value={viewMode} onChange={setViewMode} testId="refinements-view-mode" />
          <button
            onClick={() => setCreateOpen(true)}
            className="btn btn-primary flex items-center gap-1 text-sm"
          >
            <Plus size={16} />
            New Refinement
          </button>
        </div>
      </div>

      {/* Status filter pills */}
      <div className="flex gap-1.5 mb-4 flex-wrap">
        {statusFilters.map((f) => (
          <button
            key={f.value}
            onClick={() => setFilterStatus(f.value)}
            className={`text-xs px-2.5 py-1 rounded-full transition-colors ${
              filterStatus === f.value
                ? 'bg-accent-500 text-white shadow-sm'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
            }`}
          >
            {f.label}
          </button>
        ))}
        <button
          onClick={() => setShowArchived(!showArchived)}
          className={`text-xs px-2.5 py-1 rounded-full transition-colors ml-2 ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      {/* Refinement list grouped by ideation */}
      <div className="flex-1 overflow-y-auto space-y-4">
        {loading ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-8">Loading refinements...</div>
        ) : allRefinements.length === 0 ? (
          <div className="text-center py-12">
            <Layers size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {filterStatus ? 'No refinements with this status' : 'No refinements yet'}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mb-4">
              Refinements break down ideations into focused areas
            </p>
            <button
              onClick={() => setCreateOpen(true)}
              className="btn btn-primary text-sm"
            >
              Create your first refinement
            </button>
          </div>
        ) : (
          Array.from(displayGroups.entries()).map(([ideationId, group]) => (
            <div key={ideationId}>
              {/* Ideation header */}
              <div className="flex items-center gap-2 mb-2">
                <Layers size={14} className="text-indigo-500 shrink-0" />
                <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide truncate">
                  {group.ideationTitle}
                </h3>
              </div>

              {/* Refinement cards */}
              <div
                className={`ml-1 ${
                  viewMode === 'grid'
                    ? 'grid gap-3 grid-cols-1 sm:grid-cols-2 md:grid-cols-3'
                    : 'space-y-2'
                }`}
                data-testid={`refinements-${viewMode}`}
              >
                {group.refinements.map(({ refinement }) => (
                  <div
                    key={refinement.id}
                    onClick={() => setSelectedRefinementId(refinement.id)}
                    className={`group bg-white dark:bg-surface-800/80 border border-surface-200/80 dark:border-surface-700/40 rounded-xl p-4 cursor-pointer
                      hover:border-accent-300 dark:hover:border-accent-600/40 hover:shadow-card-hover dark:hover:shadow-card-dark-hover transition-all duration-200 ${refinement.archived ? 'opacity-50' : ''}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[refinement.status]}`}>
                            {STATUS_ICON[refinement.status]}
                            {REFINEMENT_STATUS_LABELS[refinement.status]}
                          </span>
                          <span className="text-xs text-gray-400">v{refinement.version}</span>
                        </div>
                        <h3 className="font-medium text-gray-900 dark:text-white text-sm truncate">
                          {refinement.title}
                        </h3>
                        {refinement.description && (
                          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                            {refinement.description}
                          </p>
                        )}
                        {/* Labels */}
                        {refinement.labels && refinement.labels.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-2">
                            {refinement.labels.map((label, i) => (
                              <span
                                key={i}
                                className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                              >
                                {label}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {refinement.archived && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">archived</span>
                        )}
                        <button
                          onClick={async (e) => {
                            e.stopPropagation();
                            try {
                              if (refinement.archived) {
                                await api.restoreTree(boardId, 'refinement', refinement.id);
                                toast.success('Tree restored');
                              } else {
                                await api.archiveTree(boardId, 'refinement', refinement.id);
                                toast.success('Tree archived');
                              }
                              loadRefinements();
                            } catch { toast.error('Failed'); }
                          }}
                          className="p-1 text-gray-400 hover:text-blue-500 opacity-0 group-hover:opacity-100 transition-opacity"
                          title={refinement.archived ? 'Restore tree' : 'Archive tree'}
                        >
                          {refinement.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                        </button>
                        <ChevronRight
                          size={16}
                          className="text-gray-300 dark:text-gray-600 group-hover:text-blue-500 mt-1 transition-colors"
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Modals */}
      {createOpen && (
        <CreateRefinementModal
          boardId={boardId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => loadRefinements()}
        />
      )}

      {selectedRefinementId && (
        <RefinementModal
          refinementId={selectedRefinementId}
          boardId={boardId}
          onClose={() => setSelectedRefinementId(null)}
          onChanged={loadRefinements}
        />
      )}
    </div>
  );
}
