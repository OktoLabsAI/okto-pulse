/**
 * RefinementsPanel - List of refinements across all ideations for a board
 */

import { useEffect, useRef, useState } from 'react';
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
  GitBranch,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi, type BoardRefinementPageItem } from '@/services/api';
import type { RefinementStatus } from '@/types';
import { REFINEMENT_STATUS_LABELS } from '@/types';
import { SearchInput } from '@/components/shared/SearchInput';
import {
  DerivationPendingBadge,
  REFINEMENT_PENDING_SPEC_LABEL,
} from '@/components/shared/DerivationPendingBadge';
import { useViewMode } from '@/hooks/useViewMode';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { openLineageGraph } from '@/components/traceability';
import { CreateRefinementModal } from './CreateRefinementModal';
import { RefinementModal } from './RefinementModal';
import { CognitivePendingBadge } from '@/components/knowledge/CognitivePendingBadge';
import { useCognitivePendingBadges } from '@/hooks/useCognitivePendingBadges';
import { QABadge } from '@/components/shared/QABadge';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { AccessiblePaginator } from '@/components/shared/AccessiblePaginator';
import { usePersistedPagination } from '@/hooks/usePersistedPagination';

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
  refinement: BoardRefinementPageItem;
  ideationTitle: string;
}

export function RefinementsPanel({ boardId }: RefinementsPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [refinements, setRefinements] = useState<GroupedRefinement[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [totalFiltered, setTotalFiltered] = useState(0);
  const [totalOverall, setTotalOverall] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedRefinementId, setSelectedRefinementId] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [showWithoutDerivation, setShowWithoutDerivation] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const { page, pageSize, setPagination, requestIntent } = usePersistedPagination('refinements');

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    void apiRef.current.listBoardRefinementsPage(boardId, {
      status: filterStatus || undefined,
      search: debouncedSearch || undefined,
      derivationPending: showWithoutDerivation ? true : undefined,
      includeArchived: showArchived,
      offset: requestIntent.offset,
      limit: requestIntent.limit,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setRefinements(page.items.map((refinement) => ({
        refinement,
        ideationTitle: refinement.ideation_title,
      })));
      setTotalFiltered(page.total_filtered);
      setTotalOverall(page.total_overall);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setLoadError(error instanceof Error ? error.message : 'Failed to load refinements');
      toast.error('Failed to load refinements');
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [
    boardId,
    filterStatus,
    showWithoutDerivation,
    showArchived,
    debouncedSearch,
    reloadKey,
    requestIntent.offset,
    requestIntent.limit,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = searchQuery.trim();
      if (next === debouncedSearch) return;
      setDebouncedSearch(next);
      setPagination({ page: 1, pageSize });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchQuery, debouncedSearch, pageSize, setPagination]);

  const loadRefinements = async () => {
    setReloadKey((value) => value + 1);
  };

  const resetPage = () => {
    setPagination({ page: 1, pageSize });
  };

  const statusFilters = [
    { value: '', label: 'All' },
    { value: 'draft', label: 'Draft' },
    { value: 'review', label: 'Review' },
    { value: 'approved', label: 'Approved' },
    { value: 'done', label: 'Done' },
  ];

  const { viewMode, setViewMode } = useViewMode('refinements', 'list');

  // Group by ideation for display
  const displayGroups = new Map<string, { ideationTitle: string; refinements: GroupedRefinement[] }>();
  for (const item of refinements) {
    const key = item.refinement.ideation_id;
    if (!displayGroups.has(key)) {
      displayGroups.set(key, { ideationTitle: item.ideationTitle, refinements: [] });
    }
    displayGroups.get(key)!.refinements.push(item);
  }

  // KG-03.6 — refinement is a first-line badge target (br_b7535ce1 +
  // ir_21ec0034). Batch by visible refinement source_refs; one HTTP
  // request per panel mount/refresh (api_28a22fec batch semantics).
  const visibleRefinementSourceRefs = refinements.map(
    (item) => `refinement:${item.refinement.id}`,
  );
  const { badges: cognitiveBadges } = useCognitivePendingBadges(
    boardId,
    visibleRefinementSourceRefs,
  );

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="mb-4 flex shrink-0 items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Refinements</h2>
          <span className="text-sm text-gray-400">
            ({totalFiltered} of {totalOverall})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={searchQuery}
            onChange={setSearchQuery}
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
      <div className="mb-4 flex shrink-0 flex-wrap gap-1.5">
        {statusFilters.map((f) => (
          <button
            key={f.value}
            onClick={() => {
              setFilterStatus(f.value);
              resetPage();
            }}
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
          onClick={() => {
            setShowWithoutDerivation(!showWithoutDerivation);
            resetPage();
          }}
          className={`inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-full transition-colors ml-2 ${
            showWithoutDerivation
              ? 'bg-cyan-600 text-white shadow-sm'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
          data-testid="refinements-no-derivation-filter"
        >
          <GitBranch size={12} />
          No derivation
        </button>
        <button
          onClick={() => {
            setShowArchived(!showArchived);
            resetPage();
          }}
          className={`text-xs px-2.5 py-1 rounded-full transition-colors ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      {/* Refinement list grouped by ideation */}
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto">
        {loading ? (
          <PulseLoader size="sm" label="Loading refinements..." />
        ) : loadError ? (
          <div className="py-12 text-center text-sm text-red-600" role="alert">
            Could not load refinements.
          </div>
        ) : refinements.length === 0 ? (
          <div className="text-center py-12">
            <Layers size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {totalFiltered > 0
                ? `Page ${page} is out of range`
                : debouncedSearch
                  ? `No results for “${searchQuery}”`
                  : filterStatus || showWithoutDerivation
                    ? 'No refinements match the active filters'
                    : 'No refinements yet'}
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
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[refinement.status]}`}>
                            {STATUS_ICON[refinement.status]}
                            {REFINEMENT_STATUS_LABELS[refinement.status]}
                          </span>
                          <DerivationPendingBadge
                            label={showWithoutDerivation ? REFINEMENT_PENDING_SPEC_LABEL : null}
                          />
                          <span className="text-xs text-gray-400">v{refinement.version}</span>
                          <CognitivePendingBadge
                            badge={cognitiveBadges[`refinement:${refinement.id}`]}
                          />
                        </div>
                        <h3 className="font-medium text-gray-900 dark:text-white text-sm truncate">
                          {refinement.title}
                        </h3>
                        {refinement.description && (
                          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                            {refinement.description}
                          </p>
                        )}
                        {/* Open Q&A badge on its own row, above the labels */}
                        {refinement.open_qa_count ? (
                          <div className="mt-2">
                            <QABadge count={refinement.open_qa_count} />
                          </div>
                        ) : null}
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
                          onClick={(e) => {
                            e.stopPropagation();
                            openLineageGraph('refinement', refinement.id);
                          }}
                          className="p-1 text-gray-400 hover:text-cyan-500 opacity-0 group-hover:opacity-100 transition-opacity"
                          title="Open lineage graph"
                        >
                          <GitBranch size={14} />
                        </button>
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
      <AccessiblePaginator
        page={page}
        pageSize={pageSize}
        totalFiltered={totalFiltered}
        totalOverall={totalOverall}
        itemCount={refinements.length}
        loading={loading}
        error={loadError}
        onRetry={loadRefinements}
        onPaginationChange={setPagination}
        ariaLabel="Refinements pagination"
        emptyMessage="No refinements match the active filters."
        className="mt-3 shrink-0"
        testId="refinements-pagination"
      />

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
