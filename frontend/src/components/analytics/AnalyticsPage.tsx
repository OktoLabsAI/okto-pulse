import { useState } from 'react';
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

export function AnalyticsPage() {
  const api = useDashboardApi();
  const [state, setState] = useState<AnalyticsState>({ level: 'overview' });
  const [from, setFrom] = useState(daysAgo(30));
  const [to, setTo] = useState(today());
  const [exporting, setExporting] = useState(false);

  const handleDateChange = (newFrom: string, newTo: string) => {
    setFrom(newFrom);
    setTo(newTo);
  };

  const navigateToOverview = () => {
    setState({ level: 'overview' });
  };

  const navigateToBoard = (boardId: string, boardName: string) => {
    setState({ level: 'board', boardId, boardName });
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
    const segments = [{ label: 'Analytics', onClick: navigateToOverview }];

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
    <div className="space-y-4">
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
