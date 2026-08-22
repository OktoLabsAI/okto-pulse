import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { ArrowLeft, Filter, Loader2 } from 'lucide-react';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardApi } from '@/services/api';
import { KgEffectivenessPanel } from './KgEffectivenessPanel';
import { mergeBoardKgAnalyticsPages } from './kgEffectivenessPagination';
import {
  BOARD_KG_COGNITIVE_STATUSES,
  type BoardKgAnalyticsResponse,
  type BoardKgAnalyticsState,
  type BoardKgCognitiveStatus,
} from './analyticsCanonicalTypes';

const DEFAULT_ARTIFACT_TYPES = ['card', 'ideation', 'refinement', 'spec', 'sprint', 'story'] as const;

export interface KgEffectivenessFilterState {
  from: string;
  to: string;
  cognitiveStatus: BoardKgCognitiveStatus[];
  artifactTypes: string[];
  limit: number;
}

interface RequestIssue {
  state: Extract<BoardKgAnalyticsState, 'restricted' | 'unavailable' | 'error'>;
  message: string;
}

export interface KgEffectivenessFullViewProps {
  boardId: string;
  from: string;
  to: string;
  boardLabel?: string;
  initialCognitiveStatus?: readonly BoardKgCognitiveStatus[];
  initialArtifactTypes?: readonly string[];
  artifactTypeOptions?: readonly string[];
  pageLimit?: number;
  onBack?: () => void;
  onFiltersChange?: (filters: KgEffectivenessFilterState) => void;
}

function normalizedStrings(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort();
}

function normalizedLimit(value: number): number {
  if (!Number.isFinite(value)) return 100;
  return Math.min(500, Math.max(1, Math.trunc(value)));
}

function initialFilterState({
  from,
  to,
  initialCognitiveStatus = [],
  initialArtifactTypes = [],
  pageLimit = 100,
}: Pick<KgEffectivenessFullViewProps, 'from' | 'to' | 'initialCognitiveStatus' | 'initialArtifactTypes' | 'pageLimit'>): KgEffectivenessFilterState {
  return {
    from,
    to,
    cognitiveStatus: [...new Set(initialCognitiveStatus)].sort(),
    artifactTypes: normalizedStrings(initialArtifactTypes),
    limit: normalizedLimit(pageLimit),
  };
}

function filterStateKey(filters: KgEffectivenessFilterState): string {
  return JSON.stringify([
    filters.from,
    filters.to,
    filters.cognitiveStatus,
    filters.artifactTypes,
    filters.limit,
  ]);
}

function requestIssue(error: unknown): RequestIssue {
  const message = error instanceof Error ? error.message : 'The canonical KG request failed.';
  if (error instanceof AuthenticatedFetchError) {
    if (error.status === 401 || error.status === 403 || error.code?.includes('restricted')) {
      return { state: 'restricted', message };
    }
    if (error.status === 503 || error.code?.includes('unavailable')) {
      return { state: 'unavailable', message };
    }
  }
  return { state: 'error', message };
}

function toggleValue<T extends string>(values: readonly T[], value: T, checked: boolean): T[] {
  return checked ? [...new Set([...values, value])].sort() : values.filter((item) => item !== value);
}

function label(value: string): string {
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function KgEffectivenessFullView(props: KgEffectivenessFullViewProps) {
  const {
    boardId,
    boardLabel,
    artifactTypeOptions = DEFAULT_ARTIFACT_TYPES,
    onBack,
    onFiltersChange,
  } = props;
  const api = useDashboardApi();
  const [draft, setDraft] = useState<KgEffectivenessFilterState>(() => initialFilterState(props));
  const [applied, setApplied] = useState<KgEffectivenessFilterState>(() => initialFilterState(props));
  const [pages, setPages] = useState<BoardKgAnalyticsResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [issue, setIssue] = useState<RequestIssue | null>(null);
  const [paginationIssue, setPaginationIssue] = useState<string | null>(null);
  const [filterError, setFilterError] = useState<string | null>(null);
  const requestSequence = useRef(0);
  const externalFilterKey = filterStateKey(initialFilterState(props));
  const externalFilterKeyRef = useRef(externalFilterKey);
  const data = useMemo(() => mergeBoardKgAnalyticsPages(pages), [pages]);
  const availableArtifactTypes = useMemo(
    () => normalizedStrings([...artifactTypeOptions, ...draft.artifactTypes]),
    [artifactTypeOptions, draft.artifactTypes],
  );

  const loadFirstPage = useCallback(async (filters: KgEffectivenessFilterState) => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setIssue(null);
    setPaginationIssue(null);
    try {
      const response = await api.getBoardKgAnalytics(boardId, filters.from, filters.to, {
        cognitiveStatus: filters.cognitiveStatus,
        artifactTypes: filters.artifactTypes,
        cursor: null,
        limit: filters.limit,
      });
      if (requestSequence.current === sequence) setPages([response]);
    } catch (error) {
      if (requestSequence.current === sequence) {
        setPages([]);
        setIssue(requestIssue(error));
      }
    } finally {
      if (requestSequence.current === sequence) setLoading(false);
    }
  }, [api, boardId]);

  useEffect(() => {
    if (externalFilterKeyRef.current === externalFilterKey) return;
    externalFilterKeyRef.current = externalFilterKey;
    const next = initialFilterState(props);
    setDraft((current) => filterStateKey(current) === externalFilterKey ? current : next);
    setApplied((current) => filterStateKey(current) === externalFilterKey ? current : next);
  }, [externalFilterKey, props]);

  useEffect(() => {
    void loadFirstPage(applied);
    return () => {
      requestSequence.current += 1;
    };
  }, [applied, loadFirstPage]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draft.from > draft.to) {
      setFilterError('Start date must not be after end date.');
      return;
    }
    setFilterError(null);
    const next = {
      ...draft,
      cognitiveStatus: [...new Set(draft.cognitiveStatus)].sort(),
      artifactTypes: normalizedStrings(draft.artifactTypes),
      limit: normalizedLimit(draft.limit),
    };
    setApplied(next);
    onFiltersChange?.(next);
  };

  const loadMore = async () => {
    const cursor = pages[pages.length - 1]?.next_cursor;
    if (!cursor || loadingMore) return;
    const sequence = ++requestSequence.current;
    setLoadingMore(true);
    setPaginationIssue(null);
    try {
      const response = await api.getBoardKgAnalytics(boardId, applied.from, applied.to, {
        cognitiveStatus: applied.cognitiveStatus,
        artifactTypes: applied.artifactTypes,
        cursor,
        limit: applied.limit,
      });
      if (requestSequence.current === sequence) setPages((current) => [...current, response]);
    } catch (error) {
      if (requestSequence.current === sequence) setPaginationIssue(requestIssue(error).message);
    } finally {
      if (requestSequence.current === sequence) setLoadingMore(false);
    }
  };

  const exportCsv = async () => {
    setExporting(true);
    setPaginationIssue(null);
    try {
      await api.exportBoardKgAnalyticsCsv(boardId, applied.from, applied.to, {
        cognitiveStatus: applied.cognitiveStatus,
        artifactTypes: applied.artifactTypes,
        cursor: null,
        limit: 500,
      });
    } catch (error) {
      setPaginationIssue(`CSV export failed: ${requestIssue(error).message}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <main className="space-y-5" aria-labelledby="kg-effectiveness-full-view-heading" data-testid="kg-effectiveness-full-view">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          {onBack && <button type="button" onClick={onBack} aria-label="Back to board analytics" className="mt-0.5 rounded-md border border-gray-200 p-2 text-gray-600 dark:border-gray-700 dark:text-gray-300"><ArrowLeft className="h-4 w-4" aria-hidden="true" /></button>}
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 id="kg-effectiveness-full-view-heading" className="text-xl font-bold text-gray-900 dark:text-white">KG Health &amp; Cognitive Effectiveness</h1>
              <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700 dark:border-indigo-800 dark:bg-indigo-950/30 dark:text-indigo-300">Full view</span>
              <span className="rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-[10px] font-semibold text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">Read-only</span>
            </div>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{boardLabel ?? boardId} · canonical live projection</p>
          </div>
        </div>
      </header>

      <form onSubmit={applyFilters} aria-label="KG effectiveness server filters" className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold text-gray-800 dark:text-gray-100"><Filter className="h-4 w-4 text-indigo-500" aria-hidden="true" /> Server-side filters</div>
        <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-4">
          <fieldset className="grid grid-cols-2 gap-2">
            <legend className="col-span-2 mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-500">Time range</legend>
            <label className="text-xs text-gray-600 dark:text-gray-300">From<input required type="date" value={draft.from} max={draft.to} onChange={(event) => setDraft((current) => ({ ...current, from: event.target.value }))} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs dark:border-gray-600 dark:bg-gray-900" /></label>
            <label className="text-xs text-gray-600 dark:text-gray-300">To<input required type="date" value={draft.to} min={draft.from} onChange={(event) => setDraft((current) => ({ ...current, to: event.target.value }))} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs dark:border-gray-600 dark:bg-gray-900" /></label>
          </fieldset>

          <fieldset>
            <legend className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-500">Cognitive status</legend>
            <div className="grid grid-cols-2 gap-1.5">{BOARD_KG_COGNITIVE_STATUSES.map((status) => <label key={status} className="flex min-h-8 items-center gap-2 rounded-md border border-gray-200 px-2 text-xs dark:border-gray-700"><input type="checkbox" checked={draft.cognitiveStatus.includes(status)} onChange={(event) => setDraft((current) => ({ ...current, cognitiveStatus: toggleValue(current.cognitiveStatus, status, event.target.checked) }))} /> {label(status)}</label>)}</div>
          </fieldset>

          <fieldset>
            <legend className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-500">Artifact type</legend>
            <div className="grid max-h-28 grid-cols-2 gap-1.5 overflow-y-auto pr-1">{availableArtifactTypes.map((artifactType) => <label key={artifactType} className="flex min-h-8 items-center gap-2 rounded-md border border-gray-200 px-2 text-xs dark:border-gray-700"><input type="checkbox" checked={draft.artifactTypes.includes(artifactType)} onChange={(event) => setDraft((current) => ({ ...current, artifactTypes: toggleValue(current.artifactTypes, artifactType, event.target.checked) }))} /> {label(artifactType)}</label>)}</div>
          </fieldset>

          <div className="flex flex-col justify-between gap-3">
            <label className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Records per page<input type="number" min={1} max={500} value={draft.limit} onChange={(event) => setDraft((current) => ({ ...current, limit: Number(event.target.value) }))} className="mt-1 min-h-9 w-full rounded-md border border-gray-300 bg-white px-2 text-xs normal-case dark:border-gray-600 dark:bg-gray-900" /></label>
            <button type="submit" disabled={loading} className="inline-flex min-h-9 items-center justify-center gap-2 rounded-md bg-indigo-600 px-4 text-xs font-semibold text-white disabled:opacity-50">{loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />} Apply filters</button>
          </div>
        </div>
        {filterError && <p className="mt-3 text-xs text-red-700 dark:text-red-300" role="alert">{filterError}</p>}
      </form>

      <KgEffectivenessPanel
        data={data}
        loading={loading}
        error={issue?.message ?? null}
        errorState={issue?.state}
        exporting={exporting}
        from={applied.from}
        to={applied.to}
        onRetry={() => { void loadFirstPage(applied); }}
        onExport={exportCsv}
        mode="full"
        loadedPages={pages.length}
      />

      {!loading && data && <section aria-label="KG cognitive pagination" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
        <div><p className="text-xs font-semibold text-gray-800 dark:text-gray-100" aria-live="polite">{pages.length} {pages.length === 1 ? 'page' : 'pages'} loaded</p><p className="mt-0.5 text-[10px] text-gray-500">Cursor pages add cognitive records; repeated operational domains are not double-counted. Each request uses the live authority; a historical as_of is not synthesized.</p>{paginationIssue && <p className="mt-1 text-xs text-red-700 dark:text-red-300" role="alert">{paginationIssue}</p>}</div>
        <button type="button" disabled={!data.next_cursor || loadingMore} onClick={() => { void loadMore(); }} className="inline-flex min-h-9 items-center gap-2 rounded-md border border-indigo-300 px-4 text-xs font-semibold text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50 dark:border-indigo-700 dark:text-indigo-300">{loadingMore && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />} {data.next_cursor ? 'Load more' : 'All records loaded'}</button>
      </section>}
    </main>
  );
}
