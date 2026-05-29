import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BoardDashboard } from './BoardDashboard';
import { EntityDetail } from './EntityDetail';

const mockApi = vi.hoisted(() => ({
  getBoardAnalyticsFunnel: vi.fn(),
  getBoardAnalyticsQuality: vi.fn(),
  getBoardAnalyticsCoverage: vi.fn(),
  getBoardAnalyticsAgents: vi.fn(),
  getBoardAnalyticsValidations: vi.fn(),
  getBoardAnalyticsSprints: vi.fn(),
  getBoardAnalyticsEntities: vi.fn(),
  getEntityAnalytics: vi.fn(),
  getRefinement: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => mockApi,
}));

vi.mock('recharts', () => ({
  ScatterChart: ({ children }: { children?: React.ReactNode }) => <div data-testid="scatter-chart">{children}</div>,
  Scatter: ({ children }: { children?: React.ReactNode }) => <div data-testid="scatter">{children}</div>,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Cell: () => null,
}));

const funnel = {
  stories: 0,
  story_conversion_pct: 0,
  ideations: 0,
  ideations_done: 0,
  refinements: 0,
  specs: 1,
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

describe('analytics IR/OR coverage UI', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getBoardAnalyticsFunnel.mockResolvedValue(funnel);
    mockApi.getBoardAnalyticsQuality.mockResolvedValue({ conclusion_reported: [], validation_reported: [] });
    mockApi.getBoardAnalyticsAgents.mockResolvedValue([]);
    mockApi.getBoardAnalyticsValidations.mockResolvedValue(validations);
    mockApi.getBoardAnalyticsSprints.mockResolvedValue({
      summary: {
        total_sprints: 0,
        status_breakdown: {},
        avg_completion_rate: null,
        sprint_evaluation: { total_submitted: 0, approve_rate: null, avg_overall_score: null },
      },
      sprints: [],
    });
    mockApi.getBoardAnalyticsEntities.mockResolvedValue({ total: 0, offset: 0, limit: 50, items: [] });
  });

  it('renders IR and OR coverage bars when the board payload exposes them', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([
      {
        spec_id: 'spec-1',
        title: 'Coverage Spec',
        total_ac: 2,
        covered_ac: 2,
        total_scenarios: 1,
        scenario_status_counts: { ready: 1 },
        business_rules_count: 1,
        api_contracts_count: 1,
        fr_with_rules_pct: 100,
        fr_with_contracts_pct: 100,
        tr_task_linkage_pct: 100,
        trs_total: 1,
        ir_task_linkage_pct: 50,
        irs_total: 2,
        irs_linked: 1,
        or_task_linkage_pct: 100,
        ors_total: 1,
        ors_linked: 1,
        decisions_coverage_pct: 0,
        decisions_total: 0,
      },
    ]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Coverage Spec')).toBeInTheDocument());
    expect(screen.getByText('IRs')).toBeInTheDocument();
    expect(screen.getByText('ORs')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
  });

  it('keeps legacy board coverage payloads free of IR/OR rows', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([
      {
        spec_id: 'spec-legacy',
        title: 'Legacy Spec',
        total_ac: 1,
        covered_ac: 1,
        total_scenarios: 1,
        scenario_status_counts: { ready: 1 },
        business_rules_count: 1,
        api_contracts_count: 0,
        fr_with_rules_pct: 100,
        fr_with_contracts_pct: 0,
      },
    ]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Legacy Spec')).toBeInTheDocument());
    expect(screen.queryByText('IRs')).not.toBeInTheDocument();
    expect(screen.queryByText('ORs')).not.toBeInTheDocument();
  });

  it('renders help controls for every first-level analytics header metric', async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Stories help' })).toBeInTheDocument());

    for (const label of ['Stories', 'Ideations', 'Specs', 'Tasks', 'Completeness', 'Drift', 'Coverage', 'Bugs', 'Cycle Time']) {
      expect(screen.getByRole('button', { name: `${label} help` })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole('button', { name: 'Completeness help' }));
    expect(scrollIntoView).toHaveBeenCalled();
  });

  it('renders spec-detail IR/OR drilldowns and header help targets', async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    mockApi.getEntityAnalytics.mockResolvedValue({
      spec_id: 'spec-1',
      title: 'Spec detail',
      status: 'review',
      total_ac: 1,
      covered_ac: 1,
      ac_details: [{ index: 0, text: 'AC text', covered: true }],
      scenario_statuses: [{ id: 'ts-1', title: 'Scenario', status: 'ready' }],
      cards: [],
      avg_cycle_hours: null,
      derivation: { ideation_id: null, refinement_id: null },
      business_rules: [],
      api_contracts: [],
      rules_coverage: 0,
      contracts_coverage: 0,
      technical_requirements: [],
      decisions: [],
      integration_requirements: [
        { id: 'ir-covered', title: 'Covered integration', linked_task_ids: ['task-1'] },
        { id: 'ir-open', title: 'Open integration', linked_task_ids: [] },
      ],
      observability_requirements: [
        { id: 'or-covered', title: 'Covered telemetry', linked_task_ids: ['task-2'] },
      ],
      coverage_summary: {
        ir_task_linkage_pct: 50,
        irs_total: 2,
        irs_linked: 1,
        irs_uncovered_ids: ['ir-open'],
        or_task_linkage_pct: 100,
        ors_total: 1,
        ors_linked: 1,
        ors_uncovered_ids: [],
      },
    });

    render(<EntityDetail boardId="board-1" entityType="spec" entityId="spec-1" from="2026-05-01" to="2026-05-28" />);

    await waitFor(() => expect(screen.getByText('IR Coverage (1/2)')).toBeInTheDocument());
    expect(screen.getByText('OR Coverage (1/1)')).toBeInTheDocument();
    expect(screen.getByText('Covered integration')).toBeInTheDocument();
    expect(screen.getByText('Open integration')).toBeInTheDocument();
    expect(screen.getByText('Covered telemetry')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'IRs help' }));
    expect(scrollIntoView).toHaveBeenCalled();
  });
});
