import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const dashboardApi = vi.hoisted(() => ({
  getCanonicalBoardCoverage: vi.fn(),
  getBoardAnalyticsEntities: vi.fn(),
  exportCanonicalBoardCoverageCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => dashboardApi,
}));

vi.mock('./CanonicalCoverageFullView', () => ({
  CanonicalCoverageFullView: ({
    data,
    error,
    exportError,
    specTitles,
    onExport,
  }: {
    data: unknown;
    error: string | null;
    exportError: string | null;
    specTitles: Record<string, string>;
    onExport: () => Promise<void>;
  }) => (
    <div data-testid="canonical-route-result" data-has-data={String(data !== null)}>
      <span>{error ?? 'no-error'}</span>
      <span>{exportError ?? 'no-export-error'}</span>
      <button type="button" onClick={() => void onExport()}>Export</button>
      {Object.values(specTitles).map((title) => <span key={title}>{title}</span>)}
    </div>
  ),
}));

import { CanonicalCoverageRoute } from './CanonicalCoverageRoute';

const queryState = {
  from: '2026-08-01',
  to: '2026-08-21',
  lifecycle: 'all' as const,
  outcome: 'all' as const,
  search: '',
};

function renderRoute() {
  return render(
    <CanonicalCoverageRoute
      boardId="board-1"
      queryState={queryState}
      onQueryStateChange={vi.fn()}
      onBack={vi.fn()}
      onOpenSpec={vi.fn()}
    />,
  );
}

describe('CanonicalCoverageRoute catalog boundary', () => {
  beforeEach(() => {
    dashboardApi.getCanonicalBoardCoverage.mockReset();
    dashboardApi.getBoardAnalyticsEntities.mockReset();
    dashboardApi.exportCanonicalBoardCoverageCsv.mockReset();
    dashboardApi.getCanonicalBoardCoverage.mockResolvedValue({
      contract_version: '1',
      query_fingerprint: 'a'.repeat(64),
      coverage: [],
    });
  });

  it('loads the Spec title catalog in API-bounded pages', async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => ({
      id: `spec-${index + 1}`,
      title: `Spec ${index + 1}`,
    }));
    dashboardApi.getBoardAnalyticsEntities.mockImplementation(
      async (
        _boardId: string,
        _type: string,
        _from: string,
        _to: string,
        offset: number,
        limit: number,
      ) => ({
        total: 201,
        offset,
        limit,
        items: offset === 0 ? firstPage : [{ id: 'spec-201', title: 'Spec 201' }],
      }),
    );

    renderRoute();

    expect(await screen.findByText('Spec 201')).toBeInTheDocument();
    expect(dashboardApi.getBoardAnalyticsEntities).toHaveBeenNthCalledWith(
      1,
      'board-1',
      'spec',
      '2026-08-01',
      '2026-08-21',
      0,
      200,
    );
    expect(dashboardApi.getBoardAnalyticsEntities).toHaveBeenNthCalledWith(
      2,
      'board-1',
      'spec',
      '2026-08-01',
      '2026-08-21',
      200,
      200,
    );
  });

  it('keeps canonical coverage available when the optional title catalog fails', async () => {
    dashboardApi.getBoardAnalyticsEntities.mockRejectedValue(
      new Error('catalog unavailable'),
    );

    renderRoute();

    const result = await screen.findByTestId('canonical-route-result');
    await waitFor(() => expect(result).toHaveAttribute('data-has-data', 'true'));
    expect(result).toHaveTextContent('no-error');
  });

  it('keeps loaded coverage visible when CSV export fails', async () => {
    dashboardApi.getBoardAnalyticsEntities.mockResolvedValue({ total: 0, items: [] });
    dashboardApi.exportCanonicalBoardCoverageCsv.mockRejectedValue(new Error('download unavailable'));

    renderRoute();

    const result = await screen.findByTestId('canonical-route-result');
    await waitFor(() => expect(result).toHaveAttribute('data-has-data', 'true'));
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));

    await waitFor(() => expect(result).toHaveTextContent('download unavailable'));
    expect(result).toHaveAttribute('data-has-data', 'true');
    expect(result).toHaveTextContent('no-error');
  });
});
