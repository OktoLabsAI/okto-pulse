/**
 * SprintsPanel — Board-level list of all sprints across specs
 */

import { useEffect, useRef, useState } from 'react';
import { Layers, ChevronRight, Filter } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi, type SprintPageItem } from '@/services/api';
import type { LookupOption, SprintStatus } from '@/types';
import { SPRINT_STATUS_LABELS, SPRINT_STATUS_COLORS, SPRINT_STATUSES } from '@/types';
import { SearchInput } from '@/components/shared/SearchInput';
import { useViewMode } from '@/hooks/useViewMode';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { HierarchicalList } from '@/components/shared/HierarchicalList';
import { SprintModal } from './SprintModal';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { AccessiblePaginator } from '@/components/shared/AccessiblePaginator';
import { usePersistedPagination } from '@/hooks/usePersistedPagination';
import { QABadge } from '@/components/shared/QABadge';

interface SprintsPanelProps {
  boardId: string;
}

export function SprintsPanel({ boardId }: SprintsPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [sprints, setSprints] = useState<SprintPageItem[]>([]);
  const [specs, setSpecs] = useState<LookupOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [totalFiltered, setTotalFiltered] = useState(0);
  const [totalOverall, setTotalOverall] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [filterStatus, setFilterStatus] = useState<string>('');
  const [filterSpecId, setFilterSpecId] = useState<string>('');
  const [selectedSprintId, setSelectedSprintId] = useState<string | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createTitle, setCreateTitle] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createSpecId, setCreateSpecId] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const { page, pageSize, setPagination, requestIntent } = usePersistedPagination('sprints', boardId);

  useEffect(() => {
    let active = true;
    void apiRef.current.lookupSpecs(boardId, { limit: 50 }).then((data) => {
      if (active) setSpecs(data.items);
    }).catch(() => {
      // Specs list is best-effort for filtering.
    });
    return () => { active = false; };
  }, [boardId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    void apiRef.current.listBoardSprintsPage(boardId, {
      status: filterStatus || undefined,
      specId: filterSpecId || undefined,
      search: debouncedSearch || undefined,
      offset: requestIntent.offset,
      limit: requestIntent.limit,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setSprints(page.items);
      setTotalFiltered(page.total_filtered);
      setTotalOverall(page.total_overall);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setLoadError(error instanceof Error ? error.message : 'Failed to load sprints');
      toast.error('Failed to load sprints');
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [
    boardId,
    filterStatus,
    filterSpecId,
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

  const loadSprints = async () => {
    setReloadKey((value) => value + 1);
  };

  const resetPage = () => {
    setPagination({ page: 1, pageSize });
  };

  const statusFilters = [
    { value: '', label: 'All' },
    ...SPRINT_STATUSES.map(s => ({ value: s, label: SPRINT_STATUS_LABELS[s] })),
  ];

  const { viewMode, setViewMode } = useViewMode('sprints', 'list');
  const [groupBySpec, setGroupBySpec] = useState(true);
  const specTitleById = (sid: string) => {
    const s = specs.find((x) => x.id === sid);
    if (s?.title) return s.title;
    return sid;
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="mb-3 flex shrink-0 items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-bold text-gray-900 dark:text-white">Sprints</h2>
          <span className="text-sm text-gray-400">
            ({totalFiltered} of {totalOverall})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={searchQuery}
            onChange={setSearchQuery}
            placeholder="Search sprints…"
            testId="sprints-search"
          />
          <ViewModeToggle value={viewMode} onChange={setViewMode} testId="sprints-view-mode" />
          <button
            type="button"
            onClick={() => setGroupBySpec((v) => !v)}
            data-testid="sprints-group-toggle"
            className={`text-xs px-2.5 py-1 rounded-full transition-colors ${
              groupBySpec
                ? 'bg-indigo-500 text-white'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300'
            }`}
            title="Group sprints by parent spec"
          >
            {groupBySpec ? 'Grouped by spec' : 'Flat list'}
          </button>
          <button
            onClick={() => setShowCreateForm(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-indigo-500 text-white hover:bg-indigo-600 transition-colors"
          >
            <Layers size={14} /> New Sprint
          </button>
        </div>
      </div>

      {/* Create sprint form */}
      {showCreateForm && (
        <div className="mb-4 shrink-0 space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
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
                } catch (error: unknown) {
                  toast.error(error instanceof Error ? error.message : 'Failed to create sprint');
                }
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
      <div className="mb-4 flex shrink-0 flex-wrap items-center gap-3">
        {/* Status pills */}
        <div className="flex items-center gap-1 flex-wrap">
          {statusFilters.map(f => (
            <button
              key={f.value}
              onClick={() => {
                setFilterStatus(f.value);
                resetPage();
              }}
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
              onChange={(e) => {
                setFilterSpecId(e.target.value);
                resetPage();
              }}
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
      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <PulseLoader size="sm" label="Loading sprints..." />
        ) : loadError ? (
          <div className="py-12 text-center text-sm text-red-600" role="alert">
            Could not load sprints.
          </div>
        ) : sprints.length === 0 ? (
          <div className="text-center py-12">
            <Layers size={40} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
            <p className="text-gray-500 dark:text-gray-400">
              {totalFiltered > 0
                ? `Page ${page} is out of range`
                : debouncedSearch ? `No results for “${searchQuery}”` : 'No sprints found'}
            </p>
            <p className="text-sm text-gray-400 mt-1">Sprints are created from within a Spec</p>
          </div>
        ) : (
          <HierarchicalList<SprintPageItem>
            items={sprints}
            viewMode={viewMode}
            groupingEnabled={groupBySpec}
            ungroupedLabel="No spec"
            getItemKey={(s) => s.id}
            getGroupKey={(s) => s.spec_id}
            getGroupTitle={(k) => specTitleById(k)}
            testId="sprints-list"
            groupIcon={Layers}
            renderItem={(sprint) => (
              <div
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
                  {(sprint.open_qa_count ?? 0) > 0 && (
                    <div className="mt-2">
                      <QABadge count={sprint.open_qa_count} />
                    </div>
                  )}
                  <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                    {!groupBySpec && <span>Spec: {specTitleById(sprint.spec_id)}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-400">
                  <ChevronRight size={14} className="opacity-0 group-hover:opacity-100 transition-opacity" />
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
        itemCount={sprints.length}
        loading={loading}
        error={loadError}
        onRetry={loadSprints}
        onPaginationChange={setPagination}
        ariaLabel="Sprints pagination"
        emptyMessage="No sprints match the active filters."
        className="mt-3 shrink-0"
        testId="sprints-pagination"
      />

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
