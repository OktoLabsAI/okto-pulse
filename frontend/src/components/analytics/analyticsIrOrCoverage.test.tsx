import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BoardDashboard } from './BoardDashboard';
import { EntityDetail } from './EntityDetail';

const mockApi = vi.hoisted(() => ({
  getBoardAnalyticsFunnel: vi.fn(),
  getBoardAnalyticsQuality: vi.fn(),
  getBoardAnalyticsCoverage: vi.fn(),
  getCanonicalBoardCoverage: vi.fn(),
  exportCanonicalBoardCoverageCsv: vi.fn(),
  getBoardFlowHealth: vi.fn(),
  exportBoardFlowHealthCsv: vi.fn(),
  getSpecReadiness: vi.fn(),
  getPolicyResourceReadiness: vi.fn(),
  exportReadinessCsv: vi.fn(),
  getBoardAnalyticsAgents: vi.fn(),
  getBoardAnalyticsValidations: vi.fn(),
  getBoardAnalyticsSprints: vi.fn(),
  getBoardKgAnalytics: vi.fn(),
  exportBoardKgAnalyticsCsv: vi.fn(),
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
    mockApi.getBoardKgAnalytics.mockResolvedValue({
      query_fingerprint: 'a'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      result_state: 'unavailable',
      health: {
        state: 'healthy',
        classification_reason: 'cognitive_metric_unavailable',
        reason_codes: ['cognitive_metric_unavailable'],
      },
      debt_domains: {
        result_state: 'available',
        active_queue_count: 2,
        technical_dlq_count: 1,
        canonical_debt_count: 3,
      },
      cognitive_effectiveness: {
        result_state: 'unavailable',
        cognitively_effective: null,
        denominator: null,
        attempted_count: null,
        persisted_count: null,
        technical_dlq_count: null,
        persistence_gap_count: null,
      },
    });
    mockApi.getCanonicalBoardCoverage.mockResolvedValue({
      query_fingerprint: 'b'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      totals: {
        state: 'available',
        applicable: 2,
        covered: 0,
        uncovered: 2,
        skipped: 2,
        value: 0,
        n: 2,
        reason: null,
      },
      coverage: [{
        obligation_type: 'ac',
        state: 'available',
        applicable: 2,
        covered: 0,
        uncovered: 2,
        skipped: 2,
        value: 0,
        n: 2,
        reason: null,
        rows: [],
      }],
    });
    mockApi.getBoardFlowHealth.mockResolvedValue({
      query_fingerprint: 'c'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      effective_policy: {
        version: 2,
        general_stale_hours: 72,
        rejected_stale_hours: 96,
      },
      summary: {
        healthy: 1,
        at_risk: 0,
        blocked: 1,
        stale: 0,
        restricted: 0,
        unavailable: 0,
        inconsistent: 0,
      },
      items: [{
        subject: { type: 'card', id: 'card-1' },
        state: 'blocked',
        reason_codes: ['spec_pending_validation'],
        current_episode: { state: 'in_progress', age_seconds: 3600, entered_at: '2026-05-28T11:00:00Z' },
        rework: [],
      }],
    });
    mockApi.getSpecReadiness.mockResolvedValue({
      query_fingerprint: 'd'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      specs: [
        {
          spec_id: 'spec-1',
          edition: 3,
          validation: {
            state: 'current',
            measures: {
              confidence: 80,
              clarity: 81,
              assertiveness: 82,
              decidability: 83,
              ambiguity: 12
            },
            attempts: 2,
            lifecycle_ready: true
          },
          lifecycle: { spec_pending_validation: false }
        }
      ]
    });
    mockApi.getPolicyResourceReadiness.mockResolvedValue({
      query_fingerprint: 'e'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      specs: [
        {
          spec_id: 'spec-1',
          edition: 3,
          policy: {
            totals: {
              native_pass: 1,
              blocking_pending: 0,
              blocking_failed: 0,
              stale: 0,
              inconsistent: 0
            }
          },
          resources: {
            l1: [
              { resource_type: 'architecture', state: 'provided' },
              { resource_type: 'mockup', state: 'missing' },
              { resource_type: 'knowledge_base', state: 'provided' }
            ],
            l2: [],
            covered_only_by_cancelled_task: 1
          }
        }
      ]
    });
  });

  it('renders canonical Spec, policy and resource readiness independently', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    const heading = await screen.findByRole('heading', {
      name: 'Spec & Policy Readiness'
    });
    const panel = heading.closest('section');
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).getByText('1/1')).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText('1 pass / 0 pending / 0 failed')).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText('2/3')).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText('spec-1')).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText(/cancelled-only resources 1/)).toBeInTheDocument();
  });

  it('resolves Flow Health and readiness titles from paginated entity catalogs', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);
    mockApi.getBoardAnalyticsEntities.mockImplementation(async (
      _boardId: string,
      type: string,
      _from?: string,
      _to?: string,
      offset = 0,
      limit = 50,
    ) => {
      if (limit !== 200) {
        return { total: 0, offset, limit, items: [] };
      }
      if (type === 'spec' && offset === 0) {
        return {
          total: 2,
          offset,
          limit,
          items: [{ id: 'spec-other', title: 'Another spec', status: 'current' }],
        };
      }
      if (type === 'spec' && offset === 1) {
        return {
          total: 2,
          offset,
          limit,
          items: [{ id: 'spec-1', title: 'Checkout readiness spec', status: 'current' }],
        };
      }
      if (type === 'card') {
        return {
          total: 1,
          offset,
          limit,
          items: [{ id: 'card-1', title: 'Checkout implementation card', status: 'in_progress' }],
        };
      }
      return { total: 0, offset, limit, items: [] };
    });

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    const flowHeading = await screen.findByRole('heading', { name: 'Flow Health' });
    const flowPanel = flowHeading.closest('section');
    const readinessHeading = screen.getByRole('heading', { name: 'Spec & Policy Readiness' });
    const readinessPanel = readinessHeading.closest('section');
    expect(flowPanel).not.toBeNull();
    expect(readinessPanel).not.toBeNull();

    await waitFor(() => {
      expect(within(flowPanel!).getByText('Checkout implementation card')).toBeInTheDocument();
      expect(within(readinessPanel!).getByText('Checkout readiness spec')).toBeInTheDocument();
    });
    expect(within(flowPanel!).getByText('Checkout implementation card').closest('td'))
      .toHaveAttribute('title', 'card:card-1');
    expect(within(readinessPanel!).getByText('Checkout readiness spec').closest('td'))
      .toHaveAttribute('title', 'spec-1');
    expect(mockApi.getBoardAnalyticsEntities).toHaveBeenCalledWith(
      'board-1',
      'spec',
      undefined,
      undefined,
      1,
      200,
    );
  });

  it('places governed analytics panels after Validation Gates in DOM order', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    let previousHeading = await screen.findByRole('heading', { name: 'Validation Gates' });
    for (const headingName of [
      'Board KG Analytics',
      'Canonical Coverage & Traceability',
      'Flow Health',
      'Spec & Policy Readiness',
    ]) {
      const currentHeading = screen.getByRole('heading', { name: headingName });
      expect(previousHeading.compareDocumentPosition(currentHeading) & Node.DOCUMENT_POSITION_FOLLOWING)
        .not.toBe(0);
      previousHeading = currentHeading;
    }
  });

  it('maps every canonical obligation type to a descriptive label', async () => {
    const obligationLabels = [
      ['ac', 'Acceptance Criteria'],
      ['fr', 'Functional Requirement'],
      ['test_scenario', 'Test Scenario'],
      ['business_rule', 'Business Rule'],
      ['api_contract', 'API Contract'],
      ['technical_requirement', 'Technical Requirement'],
      ['decision', 'Decision'],
      ['integration_requirement', 'Integration Requirement'],
      ['observability_requirement', 'Observability Requirement'],
    ] as const;
    mockApi.getCanonicalBoardCoverage.mockResolvedValue({
      query_fingerprint: 'g'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      totals: {
        state: 'not_applicable',
        applicable: 0,
        covered: 0,
        uncovered: 0,
        skipped: 0,
        value: null,
        n: 0,
        reason: 'zero_applicable_obligations',
      },
      coverage: obligationLabels.map(([obligationType]) => ({
        obligation_type: obligationType,
        state: 'not_applicable',
        applicable: 0,
        covered: 0,
        uncovered: 0,
        skipped: 0,
        value: null,
        n: 0,
        reason: 'zero_applicable_obligations',
        rows: [],
      })),
    });
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    const heading = await screen.findByRole('heading', { name: 'Canonical Coverage & Traceability' });
    const panel = heading.closest('section');
    expect(panel).not.toBeNull();
    for (const [, label] of obligationLabels) {
      expect(within(panel!).getByText(label)).toBeInTheDocument();
    }
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

  it('renders KG health and metric availability as independent states', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Board KG Analytics')).toBeInTheDocument());
    const panel = screen.getByRole('heading', { name: 'Board KG Analytics' }).closest('section');
    expect(panel).not.toBeNull();
    expect(within(panel!).getByText('healthy')).toBeInTheDocument();
    expect(within(panel!).getByText('unavailable')).toBeInTheDocument();
    expect(within(panel!).getAllByText('Unavailable')).toHaveLength(2);
    expect(within(panel!).getByText('cognitive_metric_unavailable')).toBeInTheDocument();
  });

  it('keeps skipped obligations in factual coverage and out of covered', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Canonical Coverage & Traceability')).toBeInTheDocument());
    const panel = screen.getByRole('heading', { name: 'Canonical Coverage & Traceability' }).closest('section');
    expect(panel).not.toBeNull();
    expect(panel).toHaveTextContent('Covered0');
    expect(panel).toHaveTextContent('Uncovered2');
    expect(panel).toHaveTextContent('Skipped2');
    expect(panel).toHaveTextContent('Acceptance Criteria2');
  });

  it('renders unavailable obligation groups without dereferencing absent counts', async () => {
    mockApi.getCanonicalBoardCoverage.mockResolvedValue({
      query_fingerprint: 'f'.repeat(64),
      as_of: '2026-05-28T12:00:00.000000Z',
      totals: {
        state: 'unavailable',
        applicable: null,
        covered: null,
        uncovered: null,
        skipped: null,
        value: null,
        n: null,
        reason: 'coverage_unavailable',
      },
      coverage: [{
        obligation_type: 'decision',
        state: 'unavailable',
        applicable: null,
        covered: null,
        uncovered: null,
        skipped: null,
        value: null,
        n: null,
        reason: 'coverage_unavailable',
        rows: [],
      }],
    });
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    const obligation = await screen.findByText('Decision');
    const row = obligation.closest('tr');
    expect(row).not.toBeNull();
    expect(within(row!).getByText('unavailable')).toBeInTheDocument();
    expect(within(row!).getAllByText('—')).toHaveLength(3);
  });

  it('labels a historical Sprint commitment unavailable without inferred counts', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);
    mockApi.getBoardAnalyticsSprints.mockResolvedValue({
      summary: {
        total_sprints: 1,
        status_breakdown: { active: 1 },
        avg_completion_rate: 50,
        sprint_evaluation: { total_submitted: 0, approve_rate: null, avg_overall_score: null },
      },
      sprints: [{
        sprint_id: 'sprint-legacy',
        title: 'Legacy Sprint',
        status: 'active',
        spec_id: 'spec-1',
        total_cards: 2,
        done_cards: 1,
        completion_rate: 50,
        card_status_breakdown: { done: 1, in_progress: 1 },
        evaluations_count: 0,
        last_evaluation: null,
        task_validation_gate: {
          total_submitted: 0,
          total_success: 0,
          total_failed: 0,
          rejection_reasons: {},
          first_pass_rate: null,
        },
        commitment: {
          state: 'unavailable_legacy',
          baseline_ref: null,
          unavailable_reason: 'activation_baseline_not_persisted',
        },
      }],
    });

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Legacy Sprint')).toBeInTheDocument());
    expect(screen.getByText('unavailable legacy')).toBeInTheDocument();
    expect(screen.queryByText(/original ·/)).not.toBeInTheDocument();
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

  it('renders governed Flow Health independently and falls back to the subject identity when title lookup fails', async () => {
    mockApi.getBoardAnalyticsCoverage.mockResolvedValue([]);
    mockApi.getBoardAnalyticsEntities.mockRejectedValue(new Error('catalog unavailable'));

    render(<BoardDashboard boardId="board-1" from="2026-05-01" to="2026-05-28" onSelectEntity={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Flow Health' })).toBeInTheDocument());
    expect(screen.getByText('card:card-1')).toBeInTheDocument();
    expect(screen.getByText('in_progress')).toBeInTheDocument();
    expect(screen.getByText('policy v2', { exact: false })).toBeInTheDocument();
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
      ac_details: [
        {
          index: 0,
          text: { id: 'ac-structured', text: 'Structured AC text', status: 'active' },
          covered: true,
        },
      ],
      total_fr: 1,
      fr_details: [
        {
          index: 0,
          text: { id: 'fr-structured', text: 'Structured FR text', status: 'active' },
          has_rule: true,
          has_contract: false,
        },
      ],
      scenario_statuses: [{ id: 'ts-1', title: 'Scenario', status: 'ready' }],
      cards: [],
      avg_cycle_hours: null,
      derivation: { ideation_id: null, refinement_id: null },
      business_rules: [],
      api_contracts: [],
      rules_coverage: 0,
      contracts_coverage: 0,
      technical_requirements: [
        {
          id: 'tr-structured',
          text: { id: 'tr-inner', text: 'Structured TR text', status: 'active' },
          linked_task_ids: ['task-1'],
        },
      ],
      trs_coverage: 100,
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
    expect(screen.getByText('Structured AC text')).toBeInTheDocument();
    expect(screen.getByText('Structured FR text')).toBeInTheDocument();
    expect(screen.getByText('Structured TR text')).toBeInTheDocument();
    expect(screen.getByText('Covered integration')).toBeInTheDocument();
    expect(screen.getByText('Open integration')).toBeInTheDocument();
    expect(screen.getByText('Covered telemetry')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'IRs help' }));
    expect(scrollIntoView).toHaveBeenCalled();
  });
});
