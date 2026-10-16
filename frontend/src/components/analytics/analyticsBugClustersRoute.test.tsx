import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
const api = vi.hoisted(() => ({ getBoard: vi.fn().mockResolvedValue({ name: 'Board' }), getBugClusters: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('./OverviewDashboard', () => ({ OverviewDashboard: () => <div /> }));
vi.mock('./BoardDashboard', () => ({ BoardDashboard: () => <div>Board dashboard</div> }));
vi.mock('./EntityDetail', () => ({ EntityDetail: () => <div /> }));
vi.mock('./CanonicalCoverageRoute', () => ({ CanonicalCoverageRoute: () => <div /> }));
vi.mock('./DeliveryIntelligenceFullView', () => ({ DeliveryIntelligenceFullView: () => <div /> }));
vi.mock('./FlowHealthFullView', () => ({ FlowHealthFullView: () => <div /> }));
vi.mock('./FlowHealthSettingsPage', () => ({ FlowHealthSettingsPage: () => <div /> }));
vi.mock('./KgEffectivenessFullView', () => ({ KgEffectivenessFullView: () => <div /> }));
import { AnalyticsPage } from './AnalyticsPage';

beforeEach(() => {
  vi.clearAllMocks();
  api.getBugClusters.mockImplementation(() => new Promise(() => {}));
  window.history.replaceState({}, '', '/analytics/boards/one');
});

it('does not read clusters on Board mount and cancels when leaving the dedicated route', async () => {
  render(<AnalyticsPage />);
  await screen.findByText('Board dashboard');
  expect(api.getBugClusters).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Bug clusters' }));
  await waitFor(() => expect(api.getBugClusters).toHaveBeenCalledTimes(1));
  expect(window.location.pathname).toBe('/analytics/boards/one/bug-clusters');
  const signal = api.getBugClusters.mock.calls[0][2] as AbortSignal;
  fireEvent.click(screen.getByRole('button', { name: 'Back to Board analytics' }));
  expect(await screen.findByText('Board dashboard')).toBeInTheDocument();
  expect(signal.aborted).toBe(true);
});

it('restores direct and browser-history routes and cancels the previous Board', async () => {
  window.history.replaceState({}, '', '/analytics/boards/one/bug-clusters');
  render(<AnalyticsPage />);
  await waitFor(() => expect(api.getBugClusters).toHaveBeenCalledTimes(1));
  const oldSignal = api.getBugClusters.mock.calls[0][2] as AbortSignal;
  act(() => {
    window.history.pushState({}, '', '/analytics/boards/two/bug-clusters');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await waitFor(() => expect(api.getBugClusters).toHaveBeenCalledTimes(2));
  expect(api.getBugClusters.mock.calls[1][0]).toBe('two');
  expect(oldSignal.aborted).toBe(true);
});
