import type { ReactNode } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OverviewDashboard } from './OverviewDashboard';

const api = vi.hoisted(() => ({ getAnalyticsOverview: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('recharts', () => ({
  BarChart: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ResponsiveContainer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  Bar: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null, Legend: () => null,
}));

function overview() {
  return {
    total_ideations: 2, ideations_done: 1, total_specs: 3, specs_done: 1, specs_with_tests: 2,
    total_cards_impl: 5, total_cards_test: 2, total_cards_bug: 1,
    total_business_rules: 4, specs_with_rules: 2, total_api_contracts: 1, specs_with_contracts: 1,
    avg_completeness: 88, avg_drift: 8, avg_cycle_hours: 12,
    cycle_time: { ideation: 1, spec: 3, card: 12 },
    spec_validation_gate: { total_submitted: 3, total_success: 2, total_failed: 1, success_rate: 66.7 },
    task_validation_gate: { total_submitted: 4, total_success: 3, total_failed: 1, first_pass_rate: 50 },
    spec_evaluation: { total_submitted: 2, total_approve: 1, total_reject: 0, total_request_changes: 1,
      approve_rate: 50, avg_overall_score: 80, specs_with_evaluation: 1 },
    funnel: { ideations: 2, refinements: 2, specs: 3, cards: 8, tests: 2, bugs: 1, done: 4 },
    velocity: [{ week: '2026-09-21', impl: 3, test: 1, bug: 0, validation_bounce: 2, spec_done: 1 }],
    boards: [{ board_id: 'b1', board_name: 'My Board', ideations: 2, refinements: 2,
      specs: 3, cards: 8, cards_done: 4, bugs: 1 }],
    total_bugs: 1, bugs_open: 1, bugs_done: 0, bugs_by_severity: { critical: 1, major: 0, minor: 0 },
    bug_rate_per_spec: [], avg_triage_hours: null,
  };
}

describe('overview after Sprint aggregate retirement', () => {
  beforeEach(() => vi.resetAllMocks());

  it.each([false, true])('renders surviving metrics and navigation with stale fields=%s', async (stale) => {
    const value = overview();
    api.getAnalyticsOverview.mockResolvedValue(stale ? {
      ...value, total_sprints: 987, sprint_status_breakdown: { active: 987 },
      sprint_evaluation: { total_submitted: 987 }, cycle_time: { ...value.cycle_time, sprint: 987 },
    } : value);
    const onSelectBoard = vi.fn();
    render(<OverviewDashboard from="2026-09-01" to="2026-09-21" onSelectBoard={onSelectBoard} />);
    await screen.findByText('My Board');
    expect(screen.getByText('Spec Validation Gate')).toBeInTheDocument();
    expect(screen.getByText('Task Validation Gate')).toBeInTheDocument();
    expect(screen.getByText('Spec Evaluation')).toBeInTheDocument();
    expect(screen.getByText('88%')).toBeInTheDocument();
    expect(screen.getByText('Spec: 3.0h')).toBeInTheDocument();
    expect(screen.queryByText(/sprint/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/987/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('boards-list-item'));
    expect(onSelectBoard).toHaveBeenCalledWith('b1', 'My Board');
    expect(api.getAnalyticsOverview).toHaveBeenCalledWith('2026-09-01', '2026-09-21');
  });

  it('reloads the remaining metrics for the selected dates', async () => {
    api.getAnalyticsOverview.mockResolvedValue(overview());
    const view = render(<OverviewDashboard from="" to="" onSelectBoard={vi.fn()} />);
    await screen.findByText('My Board');
    view.rerender(<OverviewDashboard from="2026-08-01" to="2026-08-31" onSelectBoard={vi.fn()} />);
    await waitFor(() => expect(api.getAnalyticsOverview).toHaveBeenLastCalledWith('2026-08-01', '2026-08-31'));
    await screen.findByText('My Board');
  });

  it('keeps a read failure visible instead of showing zero metrics', async () => {
    api.getAnalyticsOverview.mockRejectedValue(new Error('Analytics unavailable'));
    render(<OverviewDashboard from="" to="" onSelectBoard={vi.fn()} />);
    expect(await screen.findByText('Analytics unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Spec Validation Gate')).not.toBeInTheDocument();
  });
});
