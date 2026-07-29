/**
 * SpecsPanel - List of specs for the current board
 */

import { useEffect, useRef, useState } from 'react';
import {
  Plus,
  FileText,
  Clock,
  CheckCircle2,
  Settings,
  Ban,
  ChevronRight,
  Archive,
  ArchiveRestore,
  GitBranch,
  Layers,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { SpecSummary, SpecStatus } from '@/types';
import { SPEC_STATUS_LABELS } from '@/types';
import { sanitizePreview } from '@/lib/sanitizePreview';
import { SearchInput } from '@/components/shared/SearchInput';
import { useViewMode } from '@/hooks/useViewMode';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { HierarchicalList } from '@/components/shared/HierarchicalList';
import { openLineageGraph } from '@/components/traceability';
import { CreateSpecModal } from './CreateSpecModal';
import { SpecModal } from './SpecModal';
import { CognitivePendingBadge } from '@/components/knowledge/CognitivePendingBadge';
import { useCognitivePendingBadges } from '@/hooks/useCognitivePendingBadges';
import { QABadge } from '@/components/shared/QABadge';
import { QualitySummaryBadges } from '@/components/quality';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { AccessiblePaginator } from '@/components/shared/AccessiblePaginator';
import { usePersistedPagination } from '@/hooks/usePersistedPagination';

interface SpecsPanelProps {
  boardId: string;
}

type SpecGroupMode = 'none' | 'ideation' | 'refinement' | 'parents';

const SPEC_GROUP_OPTIONS: Array<{ value: SpecGroupMode; label: string }> = [
  { value: 'none', label: 'None' },
  { value: 'ideation', label: 'Ideation' },
  { value: 'refinement', label: 'Refinement' },
  { value: 'parents', label: 'Parents' },
];

const DEFAULT_SPEC_GROUP_MODE: SpecGroupMode = 'parents';
const SPEC_GROUP_MODE_STORAGE_PREFIX = 'okto-pulse:specs:group-mode:';
const SPEC_GROUP_MODE_VALUES = new Set<SpecGroupMode>(
  SPEC_GROUP_OPTIONS.map((option) => option.value)
);

function specGroupModeStorageKey(boardId: string): string {
  return `${SPEC_GROUP_MODE_STORAGE_PREFIX}${boardId}`;
}

function loadSpecGroupMode(boardId: string): SpecGroupMode {
  if (typeof localStorage === 'undefined') return DEFAULT_SPEC_GROUP_MODE;

  const key = specGroupModeStorageKey(boardId);
  try {
    const raw = localStorage.getItem(key);
    if (raw && SPEC_GROUP_MODE_VALUES.has(raw as SpecGroupMode)) {
      return raw as SpecGroupMode;
    }
    if (raw) localStorage.removeItem(key);
  } catch {
    // Keep the default if storage is unavailable.
  }
  return DEFAULT_SPEC_GROUP_MODE;
}

function saveSpecGroupMode(boardId: string, value: SpecGroupMode): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(specGroupModeStorageKey(boardId), value);
  } catch {
    // Non-critical UI preference.
  }
}

const STATUS_ICON: Record<SpecStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  validated: <CheckCircle2 size={14} />,
  in_progress: <Settings size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<SpecStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  validated: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  in_progress: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

export function SpecsPanel({ boardId }: SpecsPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [specs, setSpecs] = useState<SpecSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [totalFiltered, setTotalFiltered] = useState(0);
  const [totalOverall, setTotalOverall] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedSpecId, setSelectedSpecId] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [showArchived, setShowArchived] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const { page, pageSize, setPagination, requestIntent } = usePersistedPagination('specs', boardId);
  const { viewMode, setViewMode } = useViewMode('specs', 'list');
  const [groupMode, setGroupModeState] = useState<SpecGroupMode>(() => loadSpecGroupMode(boardId));
  const [parentTitleById, setParentTitleById] = useState<Record<string, string>>({});

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    void apiRef.current.listSpecsPage(boardId, {
      status: filterStatus || undefined,
      search: debouncedSearch || undefined,
      includeArchived: showArchived,
      offset: requestIntent.offset,
      limit: requestIntent.limit,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setSpecs(page.items);
      setTotalFiltered(page.total_filtered);
      setTotalOverall(page.total_overall);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setLoadError(error instanceof Error ? error.message : 'Failed to load specs');
      toast.error('Failed to load specs');
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [
    boardId,
    filterStatus,
    debouncedSearch,
    showArchived,
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

  useEffect(() => {
    setGroupModeState(loadSpecGroupMode(boardId));
  }, [boardId]);

  const setGroupMode = (value: SpecGroupMode) => {
    setGroupModeState(value);
    saveSpecGroupMode(boardId, value);
  };

  useEffect(() => {
    const controller = new AbortController();
    const loadParents = async () => {
      try {
        const [ideations, refinements] = await Promise.all([
          apiRef.current.lookupIdeations(boardId, { limit: 50, signal: controller.signal }),
          apiRef.current.listBoardRefinementsPage(boardId, {
            offset: 0,
            limit: 100,
            includeArchived: true,
            signal: controller.signal,
          }),
        ]);
        if (controller.signal.aborted) return;
        const map: Record<string, string> = {};
        for (const ideation of ideations.items) map[ideation.id] = ideation.title;
        for (const refinement of refinements.items) map[refinement.id] = refinement.title;
        setParentTitleById(map);
      } catch {
        // best-effort lookup; group titles fall back to ID when missing
      }
    };
    void loadParents();
    return () => controller.abort();
  }, [boardId]);

  const loadSpecs = async () => {
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
    { value: 'validated', label: 'Validated' },
    { value: 'in_progress', label: 'In Progress' },
    { value: 'done', label: 'Done' },
  ];

  // KG-03.6 — batch cognitive pending badges for the visible specs.
  // ONE HTTP request per (boardId, visible spec ids) change; never one
  // request per card (api_28a22fec batch semantics).
  const visibleSpecSourceRefs = specs.map((spec) => `spec:${spec.id}`);
  const { badges: cognitiveBadges } = useCognitivePendingBadges(
    boardId,
    visibleSpecSourceRefs,
  );

  const getSpecGroupKey = (spec: SpecSummary): string | null => {
    if (groupMode === 'none') return null;
    if (groupMode === 'ideation') {
      return spec.ideation_id ? `ideation:${spec.ideation_id}` : null;
    }
    if (groupMode === 'refinement') {
      return spec.refinement_id ? `refinement:${spec.refinement_id}` : null;
    }
    if (spec.refinement_id) return `refinement:${spec.refinement_id}`;
    if (spec.ideation_id) return `ideation:${spec.ideation_id}`;
    return null;
  };

  const getSpecGroupTitle = (key: string): string => {
    const [kind, id] = key.split(':', 2);
    const title = parentTitleById[id] || id || key;
    if (kind === 'ideation') return `Ideation: ${title}`;
    if (kind === 'refinement') return `Refinement: ${title}`;
    return title;
  };

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="mb-4 flex shrink-0 items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Specifications</h2>
          <span className="text-sm text-gray-400">
            ({totalFiltered} of {totalOverall})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={searchQuery}
            onChange={setSearchQuery}
            placeholder="Search specs…"
            testId="specs-search"
          />
          <ViewModeToggle value={viewMode} onChange={setViewMode} testId="specs-view-mode" />
          <label className="sr-only" htmlFor="specs-group-mode">Group specs</label>
          <div className="inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 dark:border-gray-700 dark:bg-surface-800 dark:text-gray-300">
            <Layers size={14} className="text-indigo-500" />
            <select
              id="specs-group-mode"
              value={groupMode}
              onChange={(event) => setGroupMode(event.target.value as SpecGroupMode)}
              data-testid="specs-group-mode"
              className="w-28 bg-transparent text-xs font-medium outline-none dark:bg-surface-800"
              title="Group specs"
            >
              {SPEC_GROUP_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <button
            onClick={() => setCreateOpen(true)}
            className="btn btn-primary flex items-center gap-1 text-sm"
          >
            <Plus size={16} />
            New Spec
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
            setShowArchived(!showArchived);
            resetPage();
          }}
          className={`text-xs px-2.5 py-1 rounded-full transition-colors ml-2 ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      {/* Spec list */}
      <div className="min-h-0 flex-1 overflow-y-auto" data-testid={`specs-${viewMode}`}>
        {loading ? (
          <PulseLoader size="sm" label="Loading specs..." />
        ) : loadError ? (
          <div className="py-12 text-center text-sm text-red-600" role="alert">
            Could not load specs.
          </div>
        ) : specs.length === 0 ? (
          <div className="text-center py-12">
            <FileText size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {totalFiltered > 0
                ? `Page ${page} is out of range`
                : debouncedSearch
                  ? `No results for “${searchQuery}”`
                  : filterStatus ? 'No specs with this status' : 'No specs yet'}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mb-4">
              Specs define requirements before creating tasks
            </p>
            <button
              onClick={() => setCreateOpen(true)}
              className="btn btn-primary text-sm"
            >
              Create your first spec
            </button>
          </div>
        ) : (
          <HierarchicalList<SpecSummary>
            items={specs}
            viewMode={viewMode}
            groupingEnabled={groupMode !== 'none'}
            ungroupedMode="flat"
            getItemKey={(s) => s.id}
            getGroupKey={getSpecGroupKey}
            getGroupTitle={getSpecGroupTitle}
            testId="specs-list"
            groupIcon={Layers}
            renderItem={(spec) => (
              <div
                onClick={() => setSelectedSpecId(spec.id)}
                className={`group bg-white dark:bg-surface-800/80 border border-surface-200/80 dark:border-surface-700/40 rounded-xl p-4 cursor-pointer
                  hover:border-accent-300 dark:hover:border-accent-600/40 hover:shadow-card-hover dark:hover:shadow-card-dark-hover transition-all duration-200 ${spec.archived ? 'opacity-50' : ''}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[spec.status]}`}>
                        {STATUS_ICON[spec.status]}
                        {SPEC_STATUS_LABELS[spec.status]}
                      </span>
                      <span className="text-xs text-gray-400">v{spec.version}</span>
                      <CognitivePendingBadge
                        badge={cognitiveBadges[`spec:${spec.id}`]}
                      />
                    </div>
                    <h3 className="font-medium text-gray-900 dark:text-white text-sm truncate">
                      {spec.title}
                    </h3>
                    {spec.description && (
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                        {sanitizePreview(spec.description)}
                      </p>
                    )}
                    <QualitySummaryBadges
                      summaries={spec.quality_summaries}
                      className="mt-2"
                    />
                    {/* Open Q&A badge on its own row, above the labels */}
                    {spec.open_qa_count ? (
                      <div className="mt-2">
                        <QABadge count={spec.open_qa_count} />
                      </div>
                    ) : null}
                    {spec.labels && spec.labels.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {spec.labels.map((label, i) => (
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
                    {spec.archived && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">archived</span>
                    )}
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        openLineageGraph('spec', spec.id);
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
                          if (spec.archived) {
                            await api.restoreTree(boardId, 'spec', spec.id);
                            toast.success('Tree restored');
                          } else {
                            await api.archiveTree(boardId, 'spec', spec.id);
                            toast.success('Tree archived');
                          }
                          loadSpecs();
                        } catch { toast.error('Failed'); }
                      }}
                      className="p-1 text-gray-400 hover:text-blue-500 opacity-0 group-hover:opacity-100 transition-opacity"
                      title={spec.archived ? 'Restore tree' : 'Archive tree'}
                    >
                      {spec.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                    </button>
                    <ChevronRight
                      size={16}
                      className="text-gray-300 dark:text-gray-600 group-hover:text-blue-500 mt-1 transition-colors"
                    />
                  </div>
                </div>
              </div>
            )}
          />
        )}
      </div>
      <AccessiblePaginator
        page={page}
        pageSize={pageSize}
        totalFiltered={totalFiltered}
        totalOverall={totalOverall}
        itemCount={specs.length}
        loading={loading}
        error={loadError}
        onRetry={loadSpecs}
        onPaginationChange={setPagination}
        ariaLabel="Specifications pagination"
        emptyMessage="No specifications match the active filters."
        className="mt-3 shrink-0"
        testId="specs-pagination"
      />

      {/* Modals */}
      {createOpen && (
        <CreateSpecModal
          boardId={boardId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => loadSpecs()}
        />
      )}

      {selectedSpecId && (
        <SpecModal
          specId={selectedSpecId}
          boardId={boardId}
          onClose={() => setSelectedSpecId(null)}
          onChanged={loadSpecs}
        />
      )}
    </div>
  );
}
