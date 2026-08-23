import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';

const apiMock = vi.hoisted(() => ({
  getBoardAnalyticsFunnel: vi.fn(),
  getBoardAnalyticsQuality: vi.fn(),
  getBoardAnalyticsAgents: vi.fn(),
  getBoardAnalyticsValidations: vi.fn(),
  getBoardAnalyticsSprints: vi.fn(),
  getBoardDeliveryForecast: vi.fn(),
  getBoardKgAnalytics: vi.fn(),
  getCanonicalBoardCoverage: vi.fn(),
  getBoardFlowHealth: vi.fn(),
  getSpecReadiness: vi.fn(),
  getPolicyResourceReadiness: vi.fn(),
  getBoardAnalyticsEntities: vi.fn(),
  exportBoardDeliveryForecastCsv: vi.fn(),
  exportBoardKgAnalyticsCsv: vi.fn(),
  exportCanonicalBoardCoverageCsv: vi.fn(),
  exportReadinessCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('recharts', () => ({
  ScatterChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Scatter: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Cell: () => null,
}));
vi.mock('./KgEffectivenessPanel', () => ({
  KgEffectivenessPanel: ({ onOpenFullView }: { onOpenFullView?: () => void }) => (
    <section data-testid="kg-summary"><h3>Board KG Analytics</h3><button type="button" onClick={onOpenFullView}>Open full view</button></section>
  ),
}));
vi.mock('./CanonicalCoveragePanel', () => ({
  CanonicalCoveragePanel: ({
    onOpenFullView,
  }: {
    onOpenFullView?: (query: CanonicalCoverageQueryState) => void;
  }) => (
    <section data-testid="canonical-summary">
      <h3>Canonical Coverage &amp; Traceability</h3>
      <button
        type="button"
        onClick={() => onOpenFullView?.({
          from: '2026-07-01',
          to: '2026-07-31',
          lifecycle: 'all',
          outcome: 'all',
          search: '',
        })}
      >
        Open full view
      </button>
    </section>
  ),
}));
vi.mock('./FlowHealthSummary', () => ({
  FlowHealthSummary: ({ onOpenFullView }: { onOpenFullView?: () => void }) => (
    <section data-testid="flow-summary"><h3>Flow Health</h3><button type="button" onClick={onOpenFullView}>Open full view</button></section>
  ),
}));
vi.mock('./DeliveryForecastPanel', () => ({
  DeliveryForecastPanel: ({ onOpenFullView }: { onOpenFullView?: () => void }) => (
    <section data-testid="delivery-summary"><h3>Delivery Intelligence</h3><button type="button" onClick={onOpenFullView}>Open full view</button></section>
  ),
}));

import { BoardDashboard } from './BoardDashboard';

const funnel = {
  stories: 0,
  story_conversion_pct: 0,
  ideations: 0,
  ideations_done: 0,
  refinements: 0,
  specs: 0,
  specs_done: 0,
  sprints: 0,
  cards: 0,
  cards_impl: 0,
  cards_test: 0,
  cards_bug: 0,
  done: 0,
  rules_count: 0,
  contracts_count: 0,
  specs_with_rules: 0,
  specs_with_contracts: 0,
  spec_status_breakdown: {},
  sprint_status_breakdown: {},
  card_status_breakdown: {},
  bugs_total: 0,
  bugs_open: 0,
  bugs_by_severity: { critical: 0, major: 0, minor: 0 },
  avg_cycle_hours: null,
};

const validations = {
  spec_validation_gate: {
    total_submitted: 0,
    total_success: 0,
    total_failed: 0,
    success_rate: null,
    avg_attempts_per_spec: null,
    avg_scores: { completeness: null, assertiveness: null, ambiguity: null },
    rejection_reasons: { completeness_below: 0, assertiveness_below: 0, ambiguity_above: 0, reject_recommendation: 0 },
    specs_with_validation: 0,
    per_spec: [],
  },
  task_validation_gate: {
    total_submitted: 0,
    total_success: 0,
    total_failed: 0,
    success_rate: null,
    avg_attempts_per_card: null,
    first_pass_rate: null,
    avg_scores: { confidence: null, completeness: null, drift: null },
    rejection_reasons: { confidence_below: 0, completeness_below: 0, drift_above: 0, reject_recommendation: 0 },
    cards_with_validation: 0,
    per_card: [],
  },
  spec_evaluation: { total_submitted: 0, approve_rate: null, avg_overall_score: null, specs_with_evaluation: 0 },
  sprint_evaluation: { total_submitted: 0, approve_rate: null, avg_overall_score: null, sprints_with_evaluation: 0 },
};

describe('Board dashboard A3-A6 integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoardAnalyticsFunnel.mockResolvedValue(funnel);
    apiMock.getBoardAnalyticsQuality.mockResolvedValue({ conclusion_reported: [], validation_reported: [] });
    apiMock.getBoardAnalyticsAgents.mockResolvedValue([]);
    apiMock.getBoardAnalyticsValidations.mockResolvedValue(validations);
    apiMock.getBoardAnalyticsSprints.mockResolvedValue({ summary: {}, sprints: [] });
    apiMock.getBoardDeliveryForecast.mockResolvedValue(null);
    apiMock.getBoardKgAnalytics.mockResolvedValue(null);
    apiMock.getCanonicalBoardCoverage.mockResolvedValue(null);
    apiMock.getBoardFlowHealth.mockResolvedValue(null);
    apiMock.getSpecReadiness.mockResolvedValue({ query_fingerprint: 'a'.repeat(64), as_of: '2026-07-31T00:00:00Z', specs: [] });
    apiMock.getPolicyResourceReadiness.mockResolvedValue({ query_fingerprint: 'b'.repeat(64), as_of: '2026-07-31T00:00:00Z', specs: [] });
    apiMock.getBoardAnalyticsEntities.mockResolvedValue({ total: 0, offset: 0, limit: 50, items: [] });
  });

  it('places every governed analytics surface in the exact product order after Validation Gates', async () => {
    render(
      <BoardDashboard
        boardId="board-1"
        from="2026-07-01"
        to="2026-07-31"
        onSelectEntity={vi.fn()}
      />,
    );

    await screen.findByRole('heading', { name: 'Validation Gates' });
    const expectedOrder = [
      'Validation Gates',
      'Board KG Analytics',
      'Canonical Coverage & Traceability',
      'Flow Health',
      'Spec & Policy Readiness',
      'Delivery Intelligence',
    ];
    const relevantHeadings = screen.getAllByRole('heading')
      .map((heading) => heading.textContent?.trim())
      .filter((heading): heading is string => expectedOrder.includes(heading ?? ''));

    expect(relevantHeadings).toEqual(expectedOrder);
  });

  it('passes each Open full view action to the corresponding A3-A6 callback', async () => {
    const onOpenCanonicalCoverage = vi.fn();
    const onOpenFlowHealth = vi.fn();
    const onOpenDeliveryIntelligence = vi.fn();
    const onOpenKgEffectiveness = vi.fn();
    render(
      <BoardDashboard
        boardId="board-1"
        from="2026-07-01"
        to="2026-07-31"
        onSelectEntity={vi.fn()}
        onOpenCanonicalCoverage={onOpenCanonicalCoverage}
        onOpenFlowHealth={onOpenFlowHealth}
        onOpenDeliveryIntelligence={onOpenDeliveryIntelligence}
        onOpenKgEffectiveness={onOpenKgEffectiveness}
      />,
    );

    await waitFor(() => expect(screen.getByTestId('canonical-summary')).toBeInTheDocument());
    fireEvent.click(within(screen.getByTestId('canonical-summary')).getByRole('button', { name: 'Open full view' }));
    fireEvent.click(within(screen.getByTestId('flow-summary')).getByRole('button', { name: 'Open full view' }));
    fireEvent.click(within(screen.getByTestId('delivery-summary')).getByRole('button', { name: 'Open full view' }));
    fireEvent.click(within(screen.getByTestId('kg-summary')).getByRole('button', { name: 'Open full view' }));

    expect(onOpenCanonicalCoverage).toHaveBeenCalledWith({
      from: '2026-07-01',
      to: '2026-07-31',
      lifecycle: 'all',
      outcome: 'all',
      search: '',
    });
    expect(onOpenFlowHealth).toHaveBeenCalledTimes(1);
    expect(onOpenDeliveryIntelligence).toHaveBeenCalledTimes(1);
    expect(onOpenKgEffectiveness).toHaveBeenCalledTimes(1);
  });
});
