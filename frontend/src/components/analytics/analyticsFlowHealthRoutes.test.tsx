import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  getBoard: vi.fn(),
  exportOverviewCsv: vi.fn(),
  exportBoardCsv: vi.fn(),
  exportEntityCsv: vi.fn(),
  exportBoardFlowHealthCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('./OverviewDashboard', () => ({ OverviewDashboard: () => <div data-testid="overview" /> }));
vi.mock('./EntityDetail', () => ({ EntityDetail: () => <div data-testid="entity-detail" /> }));
vi.mock('./BoardDashboard', () => ({
  BoardDashboard: ({ onOpenFlowHealth }: { onOpenFlowHealth: () => void }) => (
    <button type="button" onClick={onOpenFlowHealth}>Open governed Flow Health</button>
  ),
}));
vi.mock('./FlowHealthFullView', () => ({
  FlowHealthFullView: ({
    from,
    to,
    filters,
    onFiltersChange,
    onOpenSettings,
  }: {
    from: string;
    to: string;
    filters: { search: string; workType: string; owner: string; health: string; blockersOnly: boolean };
    onFiltersChange: (filters: { search: string; workType: string; owner: string; health: string; blockersOnly: boolean }) => void;
    onOpenSettings: () => void;
  }) => (
    <div data-testid="flow-full-route">
      <span>{from} through {to}</span>
      <span>{filters.workType} / {filters.owner}</span>
      <button type="button" onClick={() => onFiltersChange({ search: 'blocked', workType: 'card', owner: 'Maya', health: 'blocked', blockersOnly: true })}>Set Flow filters</button>
      <button type="button" onClick={onOpenSettings}>Open settings route</button>
    </div>
  ),
}));
vi.mock('./FlowHealthSettingsPage', () => ({
  FlowHealthSettingsPage: ({ onBack }: { onBack: () => void }) => (
    <div data-testid="flow-settings-route"><button type="button" onClick={onBack}>Back to full route</button></div>
  ),
}));

import { AnalyticsPage } from './AnalyticsPage';

describe('Flow Health Analytics routes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoard.mockResolvedValue({ name: 'E2E' });
    window.history.replaceState({}, '', '/analytics/boards/board-1?from=2026-07-01&to=2026-07-31');
  });

  it('opens the dedicated full view and settings route while preserving temporal and Flow filters', async () => {
    render(<AnalyticsPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Open governed Flow Health' }));
    expect(await screen.findByTestId('flow-full-route')).toHaveTextContent('2026-07-01 through 2026-07-31');
    expect(window.location.pathname).toBe('/analytics/boards/board-1/flow-health');
    expect(window.location.search).toContain('from=2026-07-01');
    expect(window.location.search).toContain('to=2026-07-31');

    fireEvent.click(screen.getByRole('button', { name: 'Set Flow filters' }));
    expect(window.location.search).toContain('work_type=card');
    expect(window.location.search).toContain('owner=Maya');
    expect(window.location.search).toContain('blockers_only=true');

    fireEvent.click(screen.getByRole('button', { name: 'Open settings route' }));
    expect(await screen.findByTestId('flow-settings-route')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/analytics/boards/board-1/flow-health/settings');
    expect(window.location.search).toContain('work_type=card');

    fireEvent.click(screen.getByRole('button', { name: 'Back to full route' }));
    expect(await screen.findByTestId('flow-full-route')).toHaveTextContent('card / Maya');
    expect(window.location.pathname).toBe('/analytics/boards/board-1/flow-health');
  });

  it('restores a copied full-view URL through popstate', async () => {
    render(<AnalyticsPage />);
    act(() => {
      window.history.pushState({}, '', '/analytics/boards/board-1/flow-health?from=2026-06-01&to=2026-06-30&work_type=spec&owner=Noah');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    await waitFor(() => expect(screen.getByTestId('flow-full-route')).toHaveTextContent('spec / Noah'));
    expect(screen.getByTestId('flow-full-route')).toHaveTextContent('2026-06-01 through 2026-06-30');
  });
});
