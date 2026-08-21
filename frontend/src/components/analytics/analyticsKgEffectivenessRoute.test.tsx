import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { KgEffectivenessFilterState } from './KgEffectivenessFullView';

const apiMock = vi.hoisted(() => ({
  getBoard: vi.fn(),
  exportOverviewCsv: vi.fn(),
  exportBoardCsv: vi.fn(),
  exportEntityCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('./OverviewDashboard', () => ({ OverviewDashboard: () => <div data-testid="overview" /> }));
vi.mock('./EntityDetail', () => ({ EntityDetail: () => <div data-testid="entity-detail" /> }));
vi.mock('./CanonicalCoverageRoute', () => ({ CanonicalCoverageRoute: () => <div data-testid="coverage-route" /> }));
vi.mock('./DeliveryIntelligenceFullView', () => ({ DeliveryIntelligenceFullView: () => <div data-testid="delivery-route" /> }));
vi.mock('./FlowHealthFullView', () => ({ FlowHealthFullView: () => <div data-testid="flow-route" /> }));
vi.mock('./FlowHealthSettingsPage', () => ({ FlowHealthSettingsPage: () => <div data-testid="flow-settings-route" /> }));
vi.mock('./BoardDashboard', () => ({
  BoardDashboard: ({ onOpenKgEffectiveness }: { onOpenKgEffectiveness?: () => void }) => (
    <button type="button" onClick={onOpenKgEffectiveness}>Open governed KG effectiveness</button>
  ),
}));
vi.mock('./KgEffectivenessFullView', () => ({
  KgEffectivenessFullView: ({
    from,
    to,
    initialCognitiveStatus,
    initialArtifactTypes,
    pageLimit,
    onFiltersChange,
  }: {
    from: string;
    to: string;
    initialCognitiveStatus: string[];
    initialArtifactTypes: string[];
    pageLimit: number;
    onFiltersChange: (filters: KgEffectivenessFilterState) => void;
  }) => (
    <div data-testid="kg-full-route">
      <span>{from} through {to}</span>
      <span>{initialCognitiveStatus.join(',')} / {initialArtifactTypes.join(',')} / {pageLimit}</span>
      <button type="button" onClick={() => onFiltersChange({
        from: '2026-08-05',
        to: '2026-08-20',
        cognitiveStatus: ['failed', 'pending'],
        artifactTypes: ['spec'],
        limit: 25,
      })}>Set KG filters</button>
    </div>
  ),
}));

import { AnalyticsPage } from './AnalyticsPage';

describe('KG effectiveness Analytics route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoard.mockResolvedValue({ name: 'E2E' });
    window.history.replaceState({}, '', '/analytics/boards/board-1?from=2026-08-01&to=2026-08-21');
  });

  it('opens the dedicated full view and preserves its server filters in the URL', async () => {
    render(<AnalyticsPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Open governed KG effectiveness' }));
    expect(await screen.findByTestId('kg-full-route')).toHaveTextContent('2026-08-01 through 2026-08-21');
    expect(window.location.pathname).toBe('/analytics/boards/board-1/kg-effectiveness');

    fireEvent.click(screen.getByRole('button', { name: 'Set KG filters' }));

    await waitFor(() => expect(window.location.search).toContain('cognitive_status=failed'));
    const params = new URLSearchParams(window.location.search);
    expect(params.getAll('cognitive_status')).toEqual(['failed', 'pending']);
    expect(params.getAll('artifact_type')).toEqual(['spec']);
    expect(params.get('from')).toBe('2026-08-05');
    expect(params.get('to')).toBe('2026-08-20');
    expect(params.get('limit')).toBe('25');
  });

  it('restores a copied filtered KG full-view URL through browser history', async () => {
    render(<AnalyticsPage />);
    act(() => {
      window.history.pushState({}, '', '/analytics/boards/board-1/kg-effectiveness?from=2026-07-01&to=2026-07-31&cognitive_status=consolidated&artifact_type=card&limit=50');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    await waitFor(() => expect(screen.getByTestId('kg-full-route')).toHaveTextContent('2026-07-01 through 2026-07-31'));
    expect(screen.getByTestId('kg-full-route')).toHaveTextContent('consolidated / card / 50');
  });
});
