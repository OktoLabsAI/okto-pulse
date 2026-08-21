import { useEffect, useState } from 'react';
import { ArrowLeft, Settings2 } from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import type { FlowHealthResponse } from './analyticsCanonicalTypes';
import { FlowHealthPanel } from './FlowHealthPanel';
import type { FlowHealthRouteFilters } from './flowHealthQueryState';

interface FlowHealthFullViewProps {
  boardId: string;
  from: string;
  to: string;
  filters: FlowHealthRouteFilters;
  onFiltersChange: (filters: FlowHealthRouteFilters) => void;
  onBack: () => void;
  onOpenSettings: () => void;
  onSelectEntity: (
    type: 'ideation' | 'spec' | 'refinement' | 'card',
    id: string,
    name: string,
  ) => void;
}

interface EntityItem {
  id: string;
  title?: string | null;
}

interface EntityListResponse {
  total: number;
  items: EntityItem[];
}

function titleKey(type: string, id: string): string {
  return `${type === 'task' ? 'card' : type}:${id}`;
}

export function FlowHealthFullView({
  boardId,
  from,
  to,
  filters,
  onFiltersChange,
  onBack,
  onOpenSettings,
  onSelectEntity,
}: FlowHealthFullViewProps) {
  const api = useDashboardApi();
  const [data, setData] = useState<FlowHealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [retry, setRetry] = useState(0);
  const [subjectTitles, setSubjectTitles] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.getBoardFlowHealth(boardId, from, to)
      .then((response) => {
        if (!cancelled) setData(response);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setData(null);
          setError(reason instanceof Error ? reason.message : 'Flow Health is unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, retry]);

  useEffect(() => {
    let cancelled = false;
    const PAGE_SIZE = 200;

    const loadKind = async (kind: 'spec' | 'card'): Promise<EntityItem[]> => {
      const items: EntityItem[] = [];
      let offset = 0;
      while (!cancelled) {
        const response = await api.getBoardAnalyticsEntities(
          boardId,
          kind,
          undefined,
          undefined,
          offset,
          PAGE_SIZE,
        ) as EntityListResponse;
        if (cancelled) return [];
        const page = Array.isArray(response.items) ? response.items : [];
        items.push(...page);
        const nextOffset = offset + page.length;
        if (page.length === 0 || nextOffset <= offset || nextOffset >= response.total) break;
        offset = nextOffset;
      }
      return items;
    };

    Promise.allSettled([loadKind('spec'), loadKind('card')]).then((results) => {
      if (cancelled) return;
      const catalog: Record<string, string> = {};
      results.forEach((result, index) => {
        if (result.status !== 'fulfilled') return;
        const type = index === 0 ? 'spec' : 'card';
        result.value.forEach((item) => {
          const title = item.title?.trim();
          if (item.id && title) catalog[titleKey(type, item.id)] = title;
        });
      });
      setSubjectTitles(catalog);
    });

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  const exportCsv = async () => {
    if (exporting) return;
    setExporting(true);
    setError(null);
    try {
      await api.exportBoardFlowHealthCsv(boardId, from, to);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Flow Health export failed.');
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-4" data-testid="flow-health-full-view">
      <div className="flex flex-wrap items-start justify-between gap-4 rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wide text-violet-500">Analytics · Delivery</p>
          <h1 className="mt-1 text-xl font-semibold text-gray-900 dark:text-white">Flow Health &amp; Governed Rework</h1>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Board-scoped operational evidence · Read only</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={onBack} className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 px-3 py-2 text-xs font-semibold dark:border-gray-600"><ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Board dashboard</button>
          <button type="button" onClick={onOpenSettings} className="inline-flex items-center gap-1.5 rounded-md border border-violet-300 px-3 py-2 text-xs font-semibold text-violet-700 dark:border-violet-700 dark:text-violet-200"><Settings2 className="h-3.5 w-3.5" aria-hidden="true" /> Board settings</button>
        </div>
      </div>

      <FlowHealthPanel
        boardId={boardId}
        data={data}
        loading={loading}
        error={error}
        exporting={exporting}
        from={from}
        to={to}
        subjectTitles={subjectTitles}
        initialFilters={filters}
        settingsMode="separate"
        onFiltersChange={onFiltersChange}
        onOpenSettings={onOpenSettings}
        onRetry={() => setRetry((value) => value + 1)}
        onReload={() => setRetry((value) => value + 1)}
        onExport={exportCsv}
        onOpenSubject={(type, id, title) => {
          const normalized = type === 'task' ? 'card' : type;
          if (normalized === 'spec' || normalized === 'card' || normalized === 'ideation' || normalized === 'refinement') {
            onSelectEntity(normalized, id, title);
          }
        }}
      />
    </div>
  );
}
