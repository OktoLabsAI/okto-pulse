import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DeliveryIntelligenceFilters } from './analyticsDeliveryTypes';

const apiMock = vi.hoisted(() => ({
  getBoard: vi.fn(),
  exportOverviewCsv: vi.fn(),
  exportBoardCsv: vi.fn(),
  exportEntityCsv: vi.fn(),
  exportBoardFlowHealthCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('./OverviewDashboard', () => ({ OverviewDashboard: () => <div data-testid="overview" /> }));
vi.mock('./DateFilter', () => ({ DateFilter: () => <div data-testid="global-date-filter" /> }));
vi.mock('./EntityDetail', () => ({ EntityDetail: () => <div data-testid="entity-detail" /> }));
vi.mock('./CanonicalCoverageRoute', () => ({ CanonicalCoverageRoute: () => <div data-testid="coverage-route" /> }));
vi.mock('./FlowHealthFullView', () => ({ FlowHealthFullView: () => <div data-testid="flow-route" /> }));
vi.mock('./FlowHealthSettingsPage', () => ({ FlowHealthSettingsPage: () => <div data-testid="flow-settings-route" /> }));
vi.mock('./KgEffectivenessFullView', () => ({
  KgEffectivenessFullView: () => <div data-testid="kg-route" />,
}));
vi.mock('./BoardDashboard', () => ({
  BoardDashboard: ({ onOpenDeliveryIntelligence }: { onOpenDeliveryIntelligence: () => void }) => (
    <button type="button" onClick={onOpenDeliveryIntelligence}>Open Delivery Intelligence</button>
  ),
}));
vi.mock('./DeliveryIntelligenceFullView', () => ({
  DeliveryIntelligenceFullView: ({
    from,
    to,
    initialFilters,
    onFiltersChange,
    onPeriodChange,
    onSelectEntity,
  }: {
    from: string;
    to: string;
    initialFilters: DeliveryIntelligenceFilters;
    onFiltersChange: (filters: DeliveryIntelligenceFilters) => void;
    onPeriodChange: (days: 30 | 90) => void;
    onSelectEntity: (type: 'sprint', id: string, name: string) => void;
  }) => (
    <div data-testid="delivery-route">
      <span>{from} through {to}</span>
      <span data-testid="delivery-route-filters">{JSON.stringify(initialFilters)}</span>
      <button type="button" onClick={() => onFiltersChange({
        sprintId: 'sprint-1',
        lane: 'hotfix',
        role: 'developer',
        contributionView: 'self',
        limit: 25,
      })}>Set Delivery filters</button>
      <button type="button" onClick={() => onPeriodChange(30)}>Use 30 days</button>
      <button type="button" onClick={() => onSelectEntity('sprint', 'sprint-1', 'Sprint Alpha')}>Open Sprint Alpha</button>
    </div>
  ),
}));

import { AnalyticsPage } from './AnalyticsPage';

describe('Delivery Intelligence Analytics route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoard.mockResolvedValue({ name: 'E2E' });
    window.history.replaceState({}, '', '/analytics/boards/board-1?from=2026-07-01&to=2026-07-31');
  });

  it('opens the dedicated route, persists filters and period, then opens the Sprint entity', async () => {
    render(<AnalyticsPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Open Delivery Intelligence' }));
    const route = await screen.findByTestId('delivery-route');
    expect(route).toHaveTextContent('2026-07-01 through 2026-07-31');
    expect(window.location.pathname).toBe('/analytics/boards/board-1/delivery-intelligence');
    expect(window.location.search).toContain('from=2026-07-01');
    expect(window.location.search).toContain('to=2026-07-31');
    expect(screen.queryByTestId('global-date-filter')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Set Delivery filters' }));
    expect(window.location.search).toContain('sprint_id=sprint-1');
    expect(window.location.search).toContain('lane=hotfix');
    expect(window.location.search).toContain('role=developer');
    expect(window.location.search).toContain('contribution_view=self');
    expect(window.location.search).not.toContain('limit=');

    fireEvent.click(screen.getByRole('button', { name: 'Use 30 days' }));
    expect(await screen.findByTestId('delivery-route')).toHaveTextContent('2026-07-02 through 2026-07-31');
    expect(window.location.search).toContain('from=2026-07-02');
    expect(window.location.search).toContain('sprint_id=sprint-1');

    fireEvent.click(screen.getByRole('button', { name: 'Open Sprint Alpha' }));
    expect(await screen.findByTestId('entity-detail')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/analytics/boards/board-1/entities/sprint/sprint-1');
  });

  it('restores a copied full-view URL and its filters through popstate', async () => {
    render(<AnalyticsPage />);
    act(() => {
      window.history.pushState(
        {},
        '',
        '/analytics/boards/board-1/delivery-intelligence?from=2026-06-01&to=2026-06-30&sprint_id=sprint%2Ftwo&lane=normal&role=qa&contribution_view=aggregates',
      );
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    const route = await screen.findByTestId('delivery-route');
    await waitFor(() => expect(route).toHaveTextContent('2026-06-01 through 2026-06-30'));
    expect(screen.getByTestId('delivery-route-filters')).toHaveTextContent('"sprintId":"sprint/two"');
    expect(screen.getByTestId('delivery-route-filters')).toHaveTextContent('"lane":"normal"');
    expect(screen.getByTestId('delivery-route-filters')).toHaveTextContent('"role":"qa"');
    expect(screen.getByTestId('delivery-route-filters')).toHaveTextContent('"contributionView":"aggregates"');
    expect(screen.getByTestId('delivery-route-filters')).toHaveTextContent('"limit":25');
  });
});
