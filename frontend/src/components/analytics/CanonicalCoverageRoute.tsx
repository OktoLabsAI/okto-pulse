import { useEffect, useMemo, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { CanonicalCoverageFullView } from './CanonicalCoverageFullView';
import type { CanonicalCoverageResponse } from './analyticsCanonicalTypes';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';

interface CanonicalCoverageRouteProps {
  boardId: string;
  queryState: CanonicalCoverageQueryState;
  onQueryStateChange: (query: CanonicalCoverageQueryState) => void;
  onBack: () => void;
  onOpenSpec: (specId: string, title: string, query: CanonicalCoverageQueryState) => void;
}

export function CanonicalCoverageRoute({
  boardId,
  queryState,
  onQueryStateChange,
  onBack,
  onOpenSpec,
}: CanonicalCoverageRouteProps) {
  const api = useDashboardApi();
  const [data, setData] = useState<CanonicalCoverageResponse | null>(null);
  const [specTitles, setSpecTitles] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const CATALOG_PAGE_SIZE = 200;
    setLoading(true);
    setError(null);

    const loadSpecCatalog = async () => {
      const items: Array<{ id: string; title: string }> = [];
      let offset = 0;
      while (!cancelled) {
        const response = await api.getBoardAnalyticsEntities(
          boardId,
          'spec',
          queryState.from,
          queryState.to,
          offset,
          CATALOG_PAGE_SIZE,
        );
        if (cancelled) return [];
        const page = Array.isArray(response?.items) ? response.items : [];
        items.push(...page);
        const nextOffset = offset + page.length;
        const total = Number.isInteger(response?.total) && response.total >= 0
          ? response.total
          : nextOffset;
        if (page.length === 0 || nextOffset <= offset || nextOffset >= total) break;
        offset = nextOffset;
      }
      return items;
    };

    Promise.allSettled([
      api.getCanonicalBoardCoverage(boardId, queryState.from, queryState.to),
      loadSpecCatalog(),
    ])
      .then(([coverageResult, catalogResult]) => {
        if (cancelled) return;
        if (coverageResult.status === 'rejected') {
          setData(null);
          setSpecTitles({});
          setError(
            coverageResult.reason instanceof Error
              ? coverageResult.reason.message
              : 'Canonical coverage is unavailable.',
          );
          return;
        }
        setData(coverageResult.value);
        const items = catalogResult.status === 'fulfilled' ? catalogResult.value : [];
        setSpecTitles(Object.fromEntries(items.map((item: { id: string; title: string }) => [item.id, item.title])));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, queryState.from, queryState.to, retry]);

  const titleCatalog = useMemo(() => specTitles, [specTitles]);

  return (
    <CanonicalCoverageFullView
      boardId={boardId}
      queryState={queryState}
      onQueryStateChange={onQueryStateChange}
      onBack={onBack}
      data={data}
      loading={loading}
      error={error}
      exporting={exporting}
      specTitles={titleCatalog}
      onRetry={() => setRetry((value) => value + 1)}
      onExport={async () => {
        if (exporting) return;
        setExporting(true);
        setError(null);
        try {
          await api.exportCanonicalBoardCoverageCsv(
            boardId,
            queryState.from,
            queryState.to,
          );
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : 'Canonical coverage export failed.');
        } finally {
          setExporting(false);
        }
      }}
      onOpenSpec={(specId, title) => onOpenSpec(specId, title, queryState)}
    />
  );
}
