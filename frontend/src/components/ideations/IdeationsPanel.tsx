/**
 * IdeationsPanel - List of ideations for the current board
 */

import { useEffect, useState } from 'react';
import {
  Plus,
  Lightbulb,
  Clock,
  CheckCircle2,
  Sparkles,
  Ban,
  ChevronRight,
  Archive,
  ArchiveRestore,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { IdeationSummary, IdeationStatus, IdeationComplexity } from '@/types';
import { IDEATION_STATUS_LABELS } from '@/types';
import { sanitizePreview } from '@/lib/sanitizePreview';
import { useListSearch } from '@/hooks/useListSearch';
import { SearchInput } from '@/components/shared/SearchInput';
import { useViewMode } from '@/hooks/useViewMode';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { CreateIdeationModal } from './CreateIdeationModal';
import { IdeationModal } from './IdeationModal';

interface IdeationsPanelProps {
  boardId: string;
}

const STATUS_ICON: Record<IdeationStatus, React.ReactNode> = {
  draft: <Lightbulb size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  evaluating: <Sparkles size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<IdeationStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  evaluating: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

const COMPLEXITY_COLORS: Record<IdeationComplexity, string> = {
  small: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  medium: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  large: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

export function IdeationsPanel({ boardId }: IdeationsPanelProps) {
  const api = useDashboardApi();
  const [ideations, setIdeations] = useState<IdeationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedIdeationId, setSelectedIdeationId] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [showArchived, setShowArchived] = useState(false);

  const search = useListSearch<IdeationSummary>(ideations, {
    fields: ['title', 'description', 'problem_statement', 'labels'],
    urlParam: 'q_ideations',
  });
  const { viewMode, setViewMode } = useViewMode('ideations', 'list');

  useEffect(() => {
    loadIdeations();
  }, [boardId, filterStatus, showArchived]);

  const loadIdeations = async () => {
    setLoading(true);
    try {
      const data = await api.listIdeations(boardId, filterStatus || undefined, showArchived);
      setIdeations(data);
    } catch {
      toast.error('Failed to load ideations');
    } finally {
      setLoading(false);
    }
  };

  const statusFilters = [
    { value: '', label: 'All' },
    { value: 'draft', label: 'Draft' },
    { value: 'review', label: 'Review' },
    { value: 'approved', label: 'Approved' },
    { value: 'evaluating', label: 'Evaluating' },
    { value: 'done', label: 'Done' },
  ];

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Ideations</h2>
          <span className="text-sm text-gray-400">
            ({search.filtered.length}
            {search.query ? ` of ${ideations.length}` : ''})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={search.query}
            onChange={search.setQuery}
            placeholder="Search ideations…"
            testId="ideations-search"
          />
          <ViewModeToggle value={viewMode} onChange={setViewMode} testId="ideations-view-mode" />
          <button
            onClick={() => setCreateOpen(true)}
            className="btn btn-primary flex items-center gap-1 text-sm"
          >
            <Plus size={16} />
            New Ideation
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

      {/* Ideation list */}
      <div
        className={`flex-1 overflow-y-auto animate-list ${
          viewMode === 'grid'
            ? 'grid gap-3 grid-cols-1 sm:grid-cols-2 md:grid-cols-3'
            : 'space-y-2'
        }`}
        data-testid={`ideations-${viewMode}`}
      >
        {loading ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-8">Loading ideations...</div>
        ) : ideations.length === 0 ? (
          <div className="text-center py-12">
            <Lightbulb size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {filterStatus ? 'No ideations with this status' : 'No ideations yet'}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mb-4">
              Ideations are the starting point of your development pipeline
            </p>
            <button
              onClick={() => setCreateOpen(true)}
              className="btn btn-primary text-sm"
            >
              Create your first ideation
            </button>
          </div>
        ) : search.filtered.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              No results for “{search.query}”
            </p>
            <button onClick={search.clear} className="btn btn-ghost text-sm">
              Clear search
            </button>
          </div>
        ) : (
          search.filtered.map((ideation) => (
            <div
              key={ideation.id}
              onClick={() => setSelectedIdeationId(ideation.id)}
              className={`group bg-white dark:bg-surface-800/80 border border-surface-200/80 dark:border-surface-700/40 rounded-xl p-4 cursor-pointer
                hover:border-accent-300 dark:hover:border-accent-600/40 hover:shadow-card-hover dark:hover:shadow-card-dark-hover transition-all duration-200 ${ideation.archived ? 'opacity-50' : ''}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[ideation.status]}`}>
                      {STATUS_ICON[ideation.status]}
                      {IDEATION_STATUS_LABELS[ideation.status]}
                    </span>
                    {ideation.complexity && (
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${COMPLEXITY_COLORS[ideation.complexity]}`}>
                        {ideation.complexity.charAt(0).toUpperCase() + ideation.complexity.slice(1)}
                      </span>
                    )}
                    <span className="text-xs text-gray-400">v{ideation.version}</span>
                  </div>
                  <h3 className="font-medium text-gray-900 dark:text-white text-sm truncate">
                    {ideation.title}
                  </h3>
                  {ideation.problem_statement && (
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                      {sanitizePreview(ideation.problem_statement)}
                    </p>
                  )}
                  {/* Labels */}
                  {ideation.labels && ideation.labels.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {ideation.labels.map((label, i) => (
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
                  {ideation.archived && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">archived</span>
                  )}
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        if (ideation.archived) {
                          await api.restoreTree(boardId, 'ideation', ideation.id);
                          toast.success('Tree restored');
                        } else {
                          await api.archiveTree(boardId, 'ideation', ideation.id);
                          toast.success('Tree archived');
                        }
                        loadIdeations();
                      } catch { toast.error('Failed'); }
                    }}
                    className="p-1 text-gray-400 hover:text-blue-500 opacity-0 group-hover:opacity-100 transition-opacity"
                    title={ideation.archived ? 'Restore tree' : 'Archive tree'}
                  >
                    {ideation.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                  </button>
                  <ChevronRight
                    size={16}
                    className="text-gray-300 dark:text-gray-600 group-hover:text-blue-500 mt-1 transition-colors"
                  />
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Modals */}
      {createOpen && (
        <CreateIdeationModal
          boardId={boardId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => loadIdeations()}
        />
      )}

      {selectedIdeationId && (
        <IdeationModal
          ideationId={selectedIdeationId}
          boardId={boardId}
          onClose={() => setSelectedIdeationId(null)}
          onChanged={loadIdeations}
        />
      )}
    </div>
  );
}
