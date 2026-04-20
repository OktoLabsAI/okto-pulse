import { useState, useEffect, useCallback } from 'react';
import { Download } from 'lucide-react';
import { Breadcrumb } from './Breadcrumb';
import { DateFilter } from './DateFilter';
import { OverviewDashboard } from './OverviewDashboard';
import { BoardDashboard } from './BoardDashboard';
import { EntityDetail } from './EntityDetail';
import { useDashboardApi } from '@/services/api';

type AnalyticsLevel = 'overview' | 'board' | 'entity';

interface AnalyticsState {
  level: AnalyticsLevel;
  boardId?: string;
  boardName?: string;
  entityType?: 'ideation' | 'spec' | 'refinement' | 'card';
  entityId?: string;
  entityName?: string;
}

function daysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().split('T')[0];
}

function today(): string {
  return new Date().toISOString().split('T')[0];
}

// Deriva state inicial do pathname atual. O overlay já só monta AnalyticsPage
// quando showAnalytics=true, então aqui só precisamos decidir entre overview e
// drill. /analytics → overview; /analytics/boards/:id → board.
function stateFromPath(pathname: string): AnalyticsState {
  const boardMatch = pathname.match(/^\/analytics\/boards\/([^/]+)/);
  if (boardMatch) {
    return { level: 'board', boardId: boardMatch[1], boardName: '' };
  }
  return { level: 'overview' };
}

function pathFromState(state: AnalyticsState): string {
  if (state.level === 'overview') return '/analytics';
  if (state.level === 'board' && state.boardId) {
    return `/analytics/boards/${state.boardId}`;
  }
  // entity ainda não tem URL própria — mantém a do board pai
  if (state.boardId) return `/analytics/boards/${state.boardId}`;
  return '/analytics';
}

export function AnalyticsPage() {
  const api = useDashboardApi();
  const [state, setState] = useState<AnalyticsState>(() =>
    stateFromPath(window.location.pathname),
  );
  const [from, setFrom] = useState(daysAgo(30));
  const [to, setTo] = useState(today());
  const [exporting, setExporting] = useState(false);

  // Sincroniza state com popstate (back/forward do browser).
  useEffect(() => {
    const handlePopstate = () => {
      setState(stateFromPath(window.location.pathname));
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
  const pushPath = useCallback((next: AnalyticsState) => {
    const nextPath = pathFromState(next);
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, '', nextPath);
    }
  }, []);

  const handleDateChange = (newFrom: string, newTo: string) => {
    setFrom(newFrom);
    setTo(newTo);
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

  const navigateToEntity = (
    entityType: 'ideation' | 'spec' | 'refinement' | 'card',
    entityId: string,
    entityName: string,
  ) => {
    setState((prev) => ({
      ...prev,
      level: 'entity',
      entityType,
      entityId,
      entityName,
    }));
  };


  const buildBreadcrumbSegments = () => {
    const rootLabel = state.level === 'overview' ? 'Analytics (Global)' : 'Analytics';
    const segments = [{ label: rootLabel, onClick: navigateToOverview }];

    if (state.level === 'board' || state.level === 'entity') {
      segments.push({
        label: state.boardName || 'Board',
        onClick: () =>
          navigateToBoard(state.boardId!, state.boardName!),
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
          <DateFilter from={from} to={to} onChange={handleDateChange} />
          <button
            onClick={handleExportCsv}
            disabled={exporting}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300
              hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors
              disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Download className={`w-3.5 h-3.5 ${exporting ? 'animate-pulse' : ''}`} />
            {exporting ? 'Exporting...' : 'Export CSV'}
          </button>
        </div>
      </div>

      {/* Content based on level */}
      {state.level === 'overview' && (
        <OverviewDashboard from={from} to={to} onSelectBoard={navigateToBoard} />
      )}
      {state.level === 'board' && state.boardId && (
        <BoardDashboard boardId={state.boardId} from={from} to={to} onSelectEntity={navigateToEntity} />
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
