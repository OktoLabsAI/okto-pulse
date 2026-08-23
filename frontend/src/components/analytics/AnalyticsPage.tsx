import { useState, useEffect, useCallback } from 'react';
import { Download } from 'lucide-react';
import { Breadcrumb } from './Breadcrumb';
import { DateFilter } from './DateFilter';
import { OverviewDashboard } from './OverviewDashboard';
import { BoardDashboard } from './BoardDashboard';
import { EntityDetail } from './EntityDetail';
import { CanonicalCoverageRoute } from './CanonicalCoverageRoute';
import { DeliveryIntelligenceFullView } from './DeliveryIntelligenceFullView';
import { FlowHealthFullView } from './FlowHealthFullView';
import { FlowHealthSettingsPage } from './FlowHealthSettingsPage';
import { KgEffectivenessFullView } from './KgEffectivenessFullView';
import type { KgEffectivenessFilterState } from './KgEffectivenessFullView';
import {
  canonicalCoverageFullViewPath,
  canonicalCoverageSearchParams,
  parseCanonicalCoverageQuery,
} from './canonicalCoverageQueryState';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';
import {
  deliveryFiltersFromSearch,
  deliveryFiltersToSearch,
} from './deliveryIntelligenceQueryState';
import type { DeliveryIntelligenceFilters } from './analyticsDeliveryTypes';
import type { BoardKgCognitiveStatus } from './analyticsCanonicalTypes';
import {
  analyticsQueryString,
  readFlowHealthRouteFilters,
} from './flowHealthQueryState';
import type { FlowHealthRouteFilters } from './flowHealthQueryState';
import { useDashboardApi } from '@/services/api';

type AnalyticsLevel =
  | 'overview'
  | 'board'
  | 'entity'
  | 'canonical-coverage'
  | 'delivery-intelligence'
  | 'flow-health'
  | 'flow-health-settings'
  | 'kg-effectiveness';

interface AnalyticsState {
  level: AnalyticsLevel;
  boardId?: string;
  boardName?: string;
  entityType?: 'ideation' | 'spec' | 'refinement' | 'sprint' | 'card';
  entityId?: string;
  entityName?: string;
  focusedCoverageSpecId?: string;
  focusedCoverageSpecName?: string;
}

function daysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().split('T')[0];
}

function today(): string {
  return new Date().toISOString().split('T')[0];
}

function decodePathSegment(segment: string): string | undefined {
  try {
    return decodeURIComponent(segment);
  } catch {
    return undefined;
  }
}

// Deriva state inicial do pathname atual. O overlay já só monta AnalyticsPage
// quando showAnalytics=true, então aqui só precisamos decidir entre overview e
// drill. Dedicated Board analytics surfaces are real URLs so refresh,
// browser history and copied links preserve the selected evidence scope.
function stateFromPath(pathname: string): AnalyticsState {
  const entityMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/entities\/(ideation|spec|refinement|sprint|card)\/([^/]+)\/?$/);
  if (entityMatch) {
    const boardId = decodePathSegment(entityMatch[1]);
    const entityId = decodePathSegment(entityMatch[3]);
    if (boardId !== undefined && entityId !== undefined) {
      return {
        level: 'entity',
        boardId,
        boardName: '',
        entityType: entityMatch[2] as AnalyticsState['entityType'],
        entityId,
        entityName: '',
      };
    }
  }
  const flowSettingsMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/flow-health\/settings\/?$/);
  if (flowSettingsMatch) {
    const boardId = decodePathSegment(flowSettingsMatch[1]);
    if (boardId !== undefined) return { level: 'flow-health-settings', boardId, boardName: '' };
  }
  const flowMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/flow-health\/?$/);
  if (flowMatch) {
    const boardId = decodePathSegment(flowMatch[1]);
    if (boardId !== undefined) return { level: 'flow-health', boardId, boardName: '' };
  }
  const coverageSpecMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/canonical-coverage\/specs\/([^/]+)\/?$/);
  if (coverageSpecMatch) {
    const boardId = decodePathSegment(coverageSpecMatch[1]);
    const focusedCoverageSpecId = decodePathSegment(coverageSpecMatch[2]);
    if (boardId !== undefined && focusedCoverageSpecId !== undefined) {
      return {
        level: 'canonical-coverage',
        boardId,
        boardName: '',
        focusedCoverageSpecId,
        focusedCoverageSpecName: '',
      };
    }
  }
  const coverageMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/canonical-coverage\/?$/);
  if (coverageMatch) {
    const boardId = decodePathSegment(coverageMatch[1]);
    if (boardId !== undefined) return { level: 'canonical-coverage', boardId, boardName: '' };
  }
  const deliveryMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/delivery-intelligence\/?$/);
  if (deliveryMatch) {
    const boardId = decodePathSegment(deliveryMatch[1]);
    if (boardId !== undefined) return { level: 'delivery-intelligence', boardId, boardName: '' };
  }
  const kgMatch = pathname.match(/^\/analytics\/boards\/([^/]+)\/kg-effectiveness\/?$/);
  if (kgMatch) {
    const boardId = decodePathSegment(kgMatch[1]);
    if (boardId !== undefined) return { level: 'kg-effectiveness', boardId, boardName: '' };
  }
  const boardMatch = pathname.match(/^\/analytics\/boards\/([^/]+)/);
  if (boardMatch) {
    const boardId = decodePathSegment(boardMatch[1]);
    if (boardId !== undefined) return { level: 'board', boardId, boardName: '' };
  }
  return { level: 'overview' };
}

function pathFromState(state: AnalyticsState): string {
  if (state.level === 'overview') return '/analytics';
  if (state.level === 'board' && state.boardId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}`;
  }
  if (state.level === 'flow-health' && state.boardId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}/flow-health`;
  }
  if (state.level === 'flow-health-settings' && state.boardId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}/flow-health/settings`;
  }
  if (state.level === 'canonical-coverage' && state.boardId) {
    const base = `/analytics/boards/${encodeURIComponent(state.boardId)}/canonical-coverage`;
    return state.focusedCoverageSpecId
      ? `${base}/specs/${encodeURIComponent(state.focusedCoverageSpecId)}`
      : base;
  }
  if (state.level === 'delivery-intelligence' && state.boardId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}/delivery-intelligence`;
  }
  if (state.level === 'kg-effectiveness' && state.boardId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}/kg-effectiveness`;
  }
  if (state.level === 'entity' && state.boardId && state.entityType && state.entityId) {
    return `/analytics/boards/${encodeURIComponent(state.boardId)}/entities/${state.entityType}/${encodeURIComponent(state.entityId)}`;
  }
  if (state.boardId) return `/analytics/boards/${encodeURIComponent(state.boardId)}`;
  return '/analytics';
}

function dateFromSearch(name: 'from' | 'to', fallback: string): string {
  const value = new URLSearchParams(window.location.search).get(name);
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : fallback;
}

function kgFiltersFromSearch(search: string, fallbackFrom: string, fallbackTo: string): KgEffectivenessFilterState {
  const params = new URLSearchParams(search);
  const limitValue = Number(params.get('limit') ?? 100);
  return {
    from: params.get('from') || fallbackFrom,
    to: params.get('to') || fallbackTo,
    cognitiveStatus: params.getAll('cognitive_status') as BoardKgCognitiveStatus[],
    artifactTypes: params.getAll('artifact_type'),
    limit: Number.isFinite(limitValue) ? Math.min(500, Math.max(1, Math.trunc(limitValue))) : 100,
  };
}

function kgFiltersSearchParams(filters: KgEffectivenessFilterState): URLSearchParams {
  const params = new URLSearchParams({ from: filters.from, to: filters.to, limit: String(filters.limit) });
  [...new Set(filters.cognitiveStatus)].sort().forEach((value) => params.append('cognitive_status', value));
  [...new Set(filters.artifactTypes)].sort().forEach((value) => params.append('artifact_type', value));
  return params;
}

function dateSearchParams(from: string, to: string): URLSearchParams {
  return new URLSearchParams({ from, to });
}

export function AnalyticsPage() {
  const api = useDashboardApi();
  const [state, setState] = useState<AnalyticsState>(() =>
    stateFromPath(window.location.pathname),
  );
  const [from, setFrom] = useState(() => dateFromSearch('from', daysAgo(30)));
  const [to, setTo] = useState(() => dateFromSearch('to', today()));
  const [coverageQuery, setCoverageQuery] = useState<CanonicalCoverageQueryState>(() => (
    parseCanonicalCoverageQuery(window.location.search, { from, to })
  ));
  const [deliveryFilters, setDeliveryFilters] = useState<DeliveryIntelligenceFilters>(() => (
    deliveryFiltersFromSearch(window.location.search)
  ));
  const [kgFilters, setKgFilters] = useState<KgEffectivenessFilterState>(() => (
    kgFiltersFromSearch(window.location.search, from, to)
  ));
  const [flowFilters, setFlowFilters] = useState<FlowHealthRouteFilters>(() => (
    readFlowHealthRouteFilters(window.location.search)
  ));
  const [exporting, setExporting] = useState(false);

  // Sincroniza state com popstate (back/forward do browser).
  useEffect(() => {
    const handlePopstate = () => {
      const nextFrom = dateFromSearch('from', daysAgo(30));
      const nextTo = dateFromSearch('to', today());
      setState(stateFromPath(window.location.pathname));
      setFrom(nextFrom);
      setTo(nextTo);
      setFlowFilters(readFlowHealthRouteFilters(window.location.search));
      setCoverageQuery(parseCanonicalCoverageQuery(window.location.search, { from: nextFrom, to: nextTo }));
      setDeliveryFilters(deliveryFiltersFromSearch(window.location.search));
      setKgFilters(kgFiltersFromSearch(window.location.search, nextFrom, nextTo));
    };
    window.addEventListener('popstate', handlePopstate);
    return () => window.removeEventListener('popstate', handlePopstate);
  }, []);

  // Resolve o board name quando entramos via URL (deep-link) sem nome ainda.
  useEffect(() => {
    if (state.level !== 'overview' && state.boardId && !state.boardName) {
      api.getBoard(state.boardId).then(
        (b: { name: string }) => {
          setState((prev) =>
            prev.boardId === state.boardId && !prev.boardName
              ? { ...prev, boardName: b.name }
              : prev,
          );
        },
        () => {
          /* ignore — breadcrumb cai no fallback 'Board' */
        },
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.boardId, state.level]);

  // Empurra nova URL quando o state muda por navegação interna (clique em board/entity).
  const pushPath = useCallback((next: AnalyticsState, nextFrom = from, nextTo = to, nextFilters = flowFilters) => {
    const nextPath = pathFromState(next);
    const query = next.level === 'flow-health' || next.level === 'flow-health-settings'
      ? analyticsQueryString(nextFrom, nextTo, nextFilters)
      : dateSearchParams(nextFrom, nextTo).toString();
    const nextUrl = `${nextPath}?${query}`;
    if (`${window.location.pathname}${window.location.search}` !== nextUrl) {
      window.history.pushState({}, '', nextUrl);
    }
  }, [flowFilters, from, to]);

  const handleDateChange = (newFrom: string, newTo: string) => {
    setFrom(newFrom);
    setTo(newTo);
    setCoverageQuery((current) => ({ ...current, from: newFrom, to: newTo }));
    setKgFilters((current) => ({ ...current, from: newFrom, to: newTo }));
    pushPath(state, newFrom, newTo);
  };

  const navigateToOverview = () => {
    const next: AnalyticsState = { level: 'overview' };
    setState(next);
    pushPath(next);
  };

  const navigateToBoard = (boardId: string, boardName: string) => {
    const next: AnalyticsState = { level: 'board', boardId, boardName };
    setState(next);
    pushPath(next);
  };

  const navigateToFlowHealth = () => {
    if (!state.boardId) return;
    const next: AnalyticsState = {
      level: 'flow-health',
      boardId: state.boardId,
      boardName: state.boardName,
    };
    setState(next);
    pushPath(next);
  };

  const navigateToFlowHealthSettings = () => {
    if (!state.boardId) return;
    const next: AnalyticsState = {
      level: 'flow-health-settings',
      boardId: state.boardId,
      boardName: state.boardName,
    };
    setState(next);
    pushPath(next);
  };

  const updateFlowFilters = (filters: FlowHealthRouteFilters) => {
    setFlowFilters(filters);
    const path = pathFromState(state);
    const query = analyticsQueryString(from, to, filters);
    window.history.replaceState({}, '', `${path}?${query}`);
  };

  const navigateToCanonicalCoverage = (query: CanonicalCoverageQueryState = coverageQuery) => {
    if (!state.boardId) return;
    const next: AnalyticsState = {
      level: 'canonical-coverage',
      boardId: state.boardId,
      boardName: state.boardName,
    };
    setState(next);
    setFrom(query.from);
    setTo(query.to);
    setCoverageQuery(query);
    window.history.pushState({}, '', canonicalCoverageFullViewPath(state.boardId, query));
  };

  const updateCoverageQuery = (query: CanonicalCoverageQueryState) => {
    setCoverageQuery(query);
    setFrom(query.from);
    setTo(query.to);
    const path = pathFromState(state);
    window.history.replaceState({}, '', `${path}?${canonicalCoverageSearchParams(query).toString()}`);
  };

  const navigateToCanonicalSpec = (
    specId: string,
    title: string,
    query: CanonicalCoverageQueryState,
  ) => {
    if (!state.boardId) return;
    const focusedQuery = { ...query, search: specId };
    const next: AnalyticsState = {
      level: 'canonical-coverage',
      boardId: state.boardId,
      boardName: state.boardName,
      focusedCoverageSpecId: specId,
      focusedCoverageSpecName: title,
    };
    setState(next);
    setCoverageQuery(focusedQuery);
    const path = pathFromState(next);
    window.history.pushState({}, '', `${path}?${canonicalCoverageSearchParams(focusedQuery).toString()}`);
  };

  const navigateToDeliveryIntelligence = () => {
    if (!state.boardId) return;
    const next: AnalyticsState = {
      level: 'delivery-intelligence',
      boardId: state.boardId,
      boardName: state.boardName,
    };
    setState(next);
    const params = dateSearchParams(from, to);
    deliveryFiltersToSearch(deliveryFilters).forEach((value, key) => params.set(key, value));
    window.history.pushState({}, '', `${pathFromState(next)}?${params.toString()}`);
  };

  const updateDeliveryFilters = (filters: DeliveryIntelligenceFilters) => {
    setDeliveryFilters(filters);
    const params = dateSearchParams(from, to);
    deliveryFiltersToSearch(filters).forEach((value, key) => params.set(key, value));
    window.history.replaceState({}, '', `${pathFromState(state)}?${params.toString()}`);
  };

  const updateDeliveryPeriod = (days: 30 | 90) => {
    const end = new Date(`${to}T00:00:00Z`);
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - (days - 1));
    const nextFrom = start.toISOString().slice(0, 10);
    setFrom(nextFrom);
    const params = dateSearchParams(nextFrom, to);
    deliveryFiltersToSearch(deliveryFilters).forEach((value, key) => params.set(key, value));
    window.history.replaceState({}, '', `${pathFromState(state)}?${params.toString()}`);
  };

  const navigateToKgEffectiveness = () => {
    if (!state.boardId) return;
    const next: AnalyticsState = {
      level: 'kg-effectiveness',
      boardId: state.boardId,
      boardName: state.boardName,
    };
    setState(next);
    setKgFilters((current) => ({ ...current, from, to }));
    window.history.pushState(
      {},
      '',
      `${pathFromState(next)}?${kgFiltersSearchParams({ ...kgFilters, from, to }).toString()}`,
    );
  };

  const updateKgFilters = (filters: KgEffectivenessFilterState) => {
    setKgFilters(filters);
    setFrom(filters.from);
    setTo(filters.to);
    window.history.replaceState(
      {},
      '',
      `${pathFromState(state)}?${kgFiltersSearchParams(filters).toString()}`,
    );
  };

  const navigateToEntity = (
    entityType: 'ideation' | 'spec' | 'refinement' | 'sprint' | 'card',
    entityId: string,
    entityName: string,
  ) => {
    const next: AnalyticsState = {
      boardId: state.boardId,
      boardName: state.boardName,
      level: 'entity',
      entityType,
      entityId,
      entityName,
    };
    setState(next);
    pushPath(next);
  };


  const buildBreadcrumbSegments = () => {
    const rootLabel = state.level === 'overview' ? 'Analytics (Global)' : 'Analytics';
    const segments = [{ label: rootLabel, onClick: navigateToOverview }];

    if (state.level !== 'overview') {
      segments.push({
        label: state.boardName || 'Board',
        onClick: () =>
          navigateToBoard(state.boardId!, state.boardName!),
      });
    }

    if (state.level === 'flow-health' || state.level === 'flow-health-settings') {
      segments.push({
        label: 'Flow Health',
        onClick: navigateToFlowHealth,
      });
    }

    if (state.level === 'flow-health-settings') {
      segments.push({
        label: 'Settings',
        onClick: undefined as unknown as () => void,
      });
    }

    if (state.level === 'canonical-coverage') {
      segments.push({
        label: 'Coverage & Traceability',
        onClick: () => navigateToCanonicalCoverage({ ...coverageQuery, search: '' }),
      });
      if (state.focusedCoverageSpecId) {
        segments.push({
          label: state.focusedCoverageSpecName || 'Canonical Spec',
          onClick: undefined as unknown as () => void,
        });
      }
    }

    if (state.level === 'delivery-intelligence') {
      segments.push({
        label: 'Delivery Intelligence',
        onClick: undefined as unknown as () => void,
      });
    }

    if (state.level === 'kg-effectiveness') {
      segments.push({
        label: 'KG Health & Cognitive Effectiveness',
        onClick: undefined as unknown as () => void,
      });
    }

    if (state.level === 'entity') {
      segments.push({
        label: state.entityName || 'Entity',
        onClick: undefined as unknown as () => void,
      });
    }

    return segments;
  };

  const handleExportCsv = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      if (state.level === 'overview') {
        await api.exportOverviewCsv(from, to);
      } else if (state.level === 'board' && state.boardId) {
        await api.exportBoardCsv(state.boardId, from, to);
      } else if (state.level === 'flow-health' && state.boardId) {
        await api.exportBoardFlowHealthCsv(state.boardId, from, to);
      } else if (state.level === 'entity' && state.boardId && state.entityType && state.entityId) {
        await api.exportEntityCsv(state.boardId, state.entityType, state.entityId);
      }
    } catch (err) {
      console.error('Export CSV failed:', err);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-4 px-8 py-6 max-w-[1920px] mx-auto">
      {/* Header row: Breadcrumb + DateFilter + Export */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <Breadcrumb segments={buildBreadcrumbSegments()} />

        <div className="flex items-center gap-3">
          {!['flow-health-settings', 'canonical-coverage', 'delivery-intelligence', 'kg-effectiveness'].includes(state.level) && <DateFilter from={from} to={to} onChange={handleDateChange} />}
          {(state.level === 'overview' || state.level === 'board' || state.level === 'entity') && <button
            onClick={handleExportCsv}
            disabled={exporting}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300
              hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors
              disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Download className={`w-3.5 h-3.5 ${exporting ? 'animate-pulse' : ''}`} />
            {exporting ? 'Exporting...' : 'Export CSV'}
          </button>}
        </div>
      </div>

      {/* Content based on level */}
      {state.level === 'overview' && (
        <OverviewDashboard from={from} to={to} onSelectBoard={navigateToBoard} />
      )}
      {state.level === 'board' && state.boardId && (
        <BoardDashboard
          boardId={state.boardId}
          from={from}
          to={to}
          onSelectEntity={navigateToEntity}
          onOpenCanonicalCoverage={navigateToCanonicalCoverage}
          onOpenDeliveryIntelligence={navigateToDeliveryIntelligence}
          onOpenFlowHealth={navigateToFlowHealth}
          onOpenKgEffectiveness={navigateToKgEffectiveness}
        />
      )}
      {state.level === 'canonical-coverage' && state.boardId && (
        <CanonicalCoverageRoute
          boardId={state.boardId}
          queryState={coverageQuery}
          onQueryStateChange={updateCoverageQuery}
          onBack={() => navigateToBoard(state.boardId!, state.boardName ?? '')}
          onOpenSpec={navigateToCanonicalSpec}
        />
      )}
      {state.level === 'delivery-intelligence' && state.boardId && (
        <DeliveryIntelligenceFullView
          boardId={state.boardId}
          from={from}
          to={to}
          initialFilters={deliveryFilters}
          onFiltersChange={updateDeliveryFilters}
          onPeriodChange={updateDeliveryPeriod}
          onSelectEntity={navigateToEntity}
        />
      )}
      {state.level === 'kg-effectiveness' && state.boardId && (
        <KgEffectivenessFullView
          key={`${state.boardId}:${kgFiltersSearchParams(kgFilters).toString()}`}
          boardId={state.boardId}
          boardLabel={state.boardName}
          from={kgFilters.from}
          to={kgFilters.to}
          initialCognitiveStatus={kgFilters.cognitiveStatus}
          initialArtifactTypes={kgFilters.artifactTypes}
          pageLimit={kgFilters.limit}
          onBack={() => navigateToBoard(state.boardId!, state.boardName ?? '')}
          onFiltersChange={updateKgFilters}
        />
      )}
      {state.level === 'flow-health' && state.boardId && (
        <FlowHealthFullView
          boardId={state.boardId}
          from={from}
          to={to}
          filters={flowFilters}
          onFiltersChange={updateFlowFilters}
          onBack={() => navigateToBoard(state.boardId!, state.boardName ?? '')}
          onOpenSettings={navigateToFlowHealthSettings}
          onSelectEntity={navigateToEntity}
        />
      )}
      {state.level === 'flow-health-settings' && state.boardId && (
        <FlowHealthSettingsPage boardId={state.boardId} onBack={navigateToFlowHealth} />
      )}
      {state.level === 'entity' && state.boardId && state.entityId && state.entityType && (
        <EntityDetail
          boardId={state.boardId}
          entityType={state.entityType}
          entityId={state.entityId}
          from={from}
          to={to}
        />
      )}
    </div>
  );
}
