import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeliveryIntelligenceFullView } from './DeliveryIntelligenceFullView';
import type {
  DeliveryForecastResponse,
  DeliveryIntelligenceResponse,
  DeliveryIntelligenceSprint,
  DeliveryMetric,
} from './analyticsDeliveryTypes';

const dashboardApi = vi.hoisted(() => ({
  getBoardDeliveryIntelligence: vi.fn(),
  getBoardDeliveryForecast: vi.fn(),
  exportBoardDeliveryIntelligenceCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => dashboardApi,
}));

const period = { from: '2026-07-01', to: '2026-07-31' };

function metric(
  value: number | null,
  state: string = 'available',
  overrides: Partial<DeliveryMetric> = {},
): DeliveryMetric {
  return {
    state,
    value,
    numerator: value === null ? null : value,
    denominator: value === null ? null : 100,
    sample_size: value === null ? 0 : 8,
    reason: value === null ? `${state}_by_authority` : null,
    unit: null,
    ...overrides,
  };
}

function sprint(
  sprintId = 'sprint-1',
  title = 'Sprint Alpha',
): DeliveryIntelligenceSprint {
  return {
    sprint_id: sprintId,
    title,
    status: 'completed',
    spec_id: 'spec-1',
    lane_type: 'normal',
    origin_sprint_id: null,
    origin_bug_id: null,
    total_cards: 10,
    done_cards: 8,
    completion_rate: 80,
    card_status_breakdown: { done: 8, active: 2 },
    evaluations_count: 1,
    last_evaluation: {
      overall_score: 92,
      recommendation: 'approve',
      evaluator_name: 'Delivery evaluator',
      created_at: '2026-07-31T12:00:00Z',
    },
    task_validation_gate: {
      total_submitted: 8,
      total_success: 7,
      total_failed: 1,
      rejection_reasons: { test_failure: 1 },
      first_pass_rate: 87.5,
    },
    commitment: {
      state: 'available',
      baseline_ref: `sprint:${sprintId}:activation`,
      activated_at: '2026-07-01T12:00:00Z',
      original_member_count: 8,
      current_member_count: 10,
      added_count: 3,
      removed_count: 1,
      unavailable_reason: null,
    },
    completed_committed_count: 7,
    committed_effort: {
      state: 'available',
      value: 21,
      unit: 'points',
      reason: null,
    },
    carryover: { state: 'available', count: 1, reason: null },
    velocity: {
      state: 'available',
      period: 'sprint',
      sample_size: 4,
      series: [{ sprint: sprintId, done: 8 }],
      reason: null,
    },
    forecast: null,
  };
}

function deliveryPage({
  resultState = 'available',
  sprints = [sprint()],
  nextCursor = null,
}: {
  resultState?: DeliveryIntelligenceResponse['result_state'];
  sprints?: DeliveryIntelligenceSprint[];
  nextCursor?: string | null;
} = {}): DeliveryIntelligenceResponse {
  const factState = resultState === 'available' || resultState === 'partial'
    ? resultState
    : resultState;
  const factValue = factState === 'available' || factState === 'partial' ? 87.5 : null;
  return {
    contract_version: '1',
    foundation_version: '1',
    query_fingerprint: 'd'.repeat(64),
    filters: [],
    as_of: '2026-08-01T12:00:00Z',
    board_id: 'board-1',
    result_state: resultState,
    provenance: {
      observed_at: '2026-08-01T12:00:00Z',
      currentness: resultState === 'available' ? 'current' : 'partial',
      reason: resultState === 'available' ? null : `${resultState}_projection`,
      sources: [{ authority: 'sprint_delivery', reference: 'board:board-1', timestamp_field: 'completed_at' }],
    },
    population_scope: { scope_ref: 'board:board-1', accessible_count: 8, excluded_count: 0 },
    exclusions: { restricted_count: resultState === 'restricted' ? 8 : 0, excluded_count: 0, reasons: [] },
    minimum_sample_size: 5,
    summary: {
      commitment_reliability: metric(factValue, factState),
      throughput: {
        state: factState,
        total: factValue === null ? 0 : 10,
        normal: factValue === null ? 0 : 8,
        hotfix: factValue === null ? 0 : 2,
        sample_size: factValue === null ? 0 : 8,
        reason: factValue === null ? `${factState}_by_authority` : null,
      },
      carryover: metric(factValue === null ? null : 2, factState, { numerator: null, denominator: null }),
      hotfix_share: metric(factValue === null ? null : 20, factState, { numerator: 2, denominator: 10 }),
      scope: {
        state: factState,
        committed_at_activation: factValue === null ? null : 8,
        completed_from_commitment: factValue === null ? null : 7,
        added_after_activation: factValue === null ? null : 3,
        removed_after_activation: factValue === null ? null : 1,
        sample_size: factValue === null ? 0 : 8,
        reason: factValue === null ? `${factState}_by_authority` : null,
      },
    },
    sprints: resultState === 'available' || resultState === 'partial' ? sprints : [],
    contributions: resultState === 'available' || resultState === 'partial' ? [{
      subject_id: 'user-1',
      subject_label: 'You',
      visibility: 'self',
      role: 'Developer',
      done_count: 8,
      first_pass: metric(75, 'available', { numerator: 6, denominator: 8 }),
      validation_success: metric(87.5, 'available', { numerator: 7, denominator: 8 }),
      rework_introduced: 1,
      rework_resolved: 1,
      median_cycle_hours: metric(18, 'available', { numerator: null, denominator: null, unit: 'hours' }),
      sample_size: 8,
      period,
    }] : [],
    next_cursor: nextCursor,
  };
}

const forecastBase = {
  contract_version: '1',
  dependency_versions: { analytics_foundation: '1', delivery_phase_1: '1' },
  query_fingerprint: 'f'.repeat(64),
  filters: [],
  as_of: '2026-08-01T12:00:00Z',
  board_id: 'board-1',
  provenance: {
    observed_at: '2026-08-01T12:00:00Z',
    currentness: 'current' as const,
    reason: null,
    sources: [{ authority: 'sprint_delivery', reference: 'board:board-1', timestamp_field: 'completed_at' }],
  },
  population_scope: { scope_ref: 'board:board-1', accessible_count: 8, excluded_count: 0 },
  exclusions: { restricted_count: 0, excluded_count: 0, reasons: [] },
};

function readyForecast(): DeliveryForecastResponse {
  return {
    ...forecastBase,
    result_state: 'available',
    readiness: {
      ready: true,
      state: 'ready',
      reason: null,
      remediation: null,
      actual_observations: 8,
      required_observations: 5,
      rule_version: 'history-v1',
    },
    forecast: {
      point: 12,
      lower_bound: 9,
      upper_bound: 15,
      confidence_level: 0.8,
      horizon: 'next_sprint',
      assumptions: ['stable_scope', 'observed_history_only'],
      sample_size: 8,
      source_period: { from: '2026-07-01T00:00:00Z', to: '2026-07-31T23:59:59Z' },
      method_version: 'empirical-v1',
    },
    backtest: {
      state: 'available',
      error: 1.5,
      calibration: 0.82,
      method_version: 'empirical-v1',
      sample_size: 5,
      evaluation_window: { from: '2026-06-01T00:00:00Z', to: '2026-06-30T23:59:59Z' },
      reason: null,
    },
  };
}

function nonReadyForecast(): DeliveryForecastResponse {
  return {
    ...forecastBase,
    result_state: 'unavailable',
    readiness: {
      ready: false,
      state: 'insufficient_history',
      reason: 'insufficient_observations',
      remediation: 'Complete more governed Sprints.',
      actual_observations: 2,
      required_observations: 5,
      rule_version: 'history-v1',
    },
    backtest: {
      state: 'unavailable',
      error: null,
      calibration: null,
      method_version: 'empirical-v1',
      sample_size: 0,
      evaluation_window: null,
      reason: 'insufficient_observations',
    },
  };
}

function renderFullView(overrides: Partial<React.ComponentProps<typeof DeliveryIntelligenceFullView>> = {}) {
  const props: React.ComponentProps<typeof DeliveryIntelligenceFullView> = {
    boardId: 'board-1',
    ...period,
    onSelectEntity: vi.fn(),
    ...overrides,
  };
  return { ...render(<DeliveryIntelligenceFullView {...props} />), props };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => { resolve = resolver; });
  return { promise, resolve };
}

describe('Delivery Intelligence A5 full view', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dashboardApi.getBoardDeliveryIntelligence.mockResolvedValue(deliveryPage());
    dashboardApi.getBoardDeliveryForecast.mockResolvedValue(nonReadyForecast());
    dashboardApi.exportBoardDeliveryIntelligenceCsv.mockResolvedValue(undefined);
  });

  it('renders available facts, emits governed filters, exports, and opens a keyboard-focusable Sprint', async () => {
    const onFiltersChange = vi.fn();
    const onPeriodChange = vi.fn();
    const onSelectEntity = vi.fn();
    dashboardApi.getBoardDeliveryForecast.mockResolvedValue(readyForecast());
    const { props } = renderFullView({ onFiltersChange, onPeriodChange, onSelectEntity });

    const page = await screen.findByTestId('delivery-intelligence-full-view');
    expect(within(page).getByRole('heading', { name: 'Delivery Intelligence' })).toBeInTheDocument();
    expect((await within(page).findAllByText('87.5%')).length).toBeGreaterThan(0);
    expect(within(page).getByText('Forecast ready')).toBeInTheDocument();
    expect(within(page).getByText('9–15 · confidence 0.8')).toBeInTheDocument();

    const sprintButton = within(page).getByRole('button', { name: 'Sprint Alpha' });
    sprintButton.focus();
    expect(sprintButton).toHaveFocus();
    fireEvent.click(sprintButton);
    expect(onSelectEntity).toHaveBeenCalledWith('sprint', 'sprint-1', 'Sprint Alpha');

    fireEvent.change(within(page).getByLabelText('Delivery Sprint'), { target: { value: 'sprint-1' } });
    fireEvent.change(within(page).getByLabelText('Delivery lane'), { target: { value: 'hotfix' } });
    fireEvent.change(within(page).getByLabelText('Contribution role'), { target: { value: 'developer' } });
    fireEvent.change(within(page).getByLabelText('Contribution visibility'), { target: { value: 'self' } });
    expect(onFiltersChange).toHaveBeenLastCalledWith({
      sprintId: 'sprint-1',
      lane: 'hotfix',
      role: 'developer',
      contributionView: 'self',
      limit: 25,
      cursor: undefined,
    });

    fireEvent.change(within(page).getByLabelText('Delivery period'), { target: { value: '90' } });
    expect(onPeriodChange).toHaveBeenCalledWith(90);

    const exportButton = within(page).getByRole('button', { name: 'Export CSV' });
    await waitFor(() => expect(exportButton).toBeEnabled());
    fireEvent.click(exportButton);
    await waitFor(() => expect(dashboardApi.exportBoardDeliveryIntelligenceCsv).toHaveBeenCalledWith(
      'board-1',
      period.from,
      period.to,
      expect.objectContaining({ sprintId: 'sprint-1', lane: 'hotfix', role: 'developer', contributionView: 'self' }),
    ));
    expect(props.onSelectEntity).toBe(onSelectEntity);
  });

  it('keeps partial, restricted, and error result states explicit', async () => {
    for (const state of ['partial', 'restricted', 'error'] as const) {
      dashboardApi.getBoardDeliveryIntelligence.mockResolvedValueOnce(deliveryPage({ resultState: state }));
      const view = renderFullView();
      const page = await screen.findByTestId('delivery-intelligence-full-view');
      expect(await within(page).findAllByText(state[0].toUpperCase() + state.slice(1))).not.toHaveLength(0);
      expect(within(page).queryByText('No delivery evidence in this period')).not.toBeInTheDocument();
      view.unmount();
    }
  });

  it('renders an honest empty state without inferring a zero commitment', async () => {
    dashboardApi.getBoardDeliveryIntelligence.mockResolvedValue(deliveryPage({ resultState: 'empty', sprints: [] }));
    renderFullView();

    expect(await screen.findByText('No delivery evidence in this period')).toBeInTheDocument();
    expect(screen.getByText(/No zero-valued commitment is inferred/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Delivery summary')).not.toBeInTheDocument();
  });

  it('keeps an empty projection visible and retries only a failed export', async () => {
    dashboardApi.getBoardDeliveryIntelligence.mockResolvedValue(deliveryPage({ resultState: 'empty', sprints: [] }));
    dashboardApi.exportBoardDeliveryIntelligenceCsv
      .mockRejectedValueOnce(new Error('download unavailable'))
      .mockResolvedValueOnce(undefined);
    renderFullView();

    expect(await screen.findByText('No delivery evidence in this period')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));

    const exportAlert = await screen.findByRole('alert');
    expect(exportAlert).toHaveTextContent('CSV export failed: download unavailable');
    expect(screen.getByText('No delivery evidence in this period')).toBeInTheDocument();
    fireEvent.click(within(exportAlert).getByRole('button', { name: 'Retry export' }));
    await waitFor(() => expect(dashboardApi.exportBoardDeliveryIntelligenceCsv).toHaveBeenCalledTimes(2));
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenCalledTimes(1);
  });

  it('exposes a transport error and retries without replacing it with an empty result', async () => {
    dashboardApi.getBoardDeliveryIntelligence
      .mockRejectedValueOnce(new Error('Delivery authority timed out.'))
      .mockResolvedValueOnce(deliveryPage());
    renderFullView();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Delivery authority timed out.');
    expect(screen.queryByText('No delivery evidence in this period')).not.toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('button', { name: 'Sprint Alpha' })).toBeInTheDocument();
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenCalledTimes(2);
  });

  it('loads the next cursor page and appends Sprints without losing the first page', async () => {
    dashboardApi.getBoardDeliveryIntelligence.mockImplementation((
      _boardId: string,
      _from: string,
      _to: string,
      filters: { cursor?: string },
    ) => Promise.resolve(filters.cursor === 'cursor-2'
      ? deliveryPage({ sprints: [sprint('sprint-2', 'Sprint Beta')], nextCursor: null })
      : deliveryPage({ nextCursor: 'cursor-2' })));
    renderFullView();

    fireEvent.click(await screen.findByRole('button', { name: 'Load more Sprints' }));
    expect(await screen.findByRole('button', { name: 'Sprint Beta' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sprint Alpha' })).toBeInTheDocument();
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenLastCalledWith(
      'board-1',
      period.from,
      period.to,
      expect.objectContaining({ cursor: 'cursor-2', limit: 25 }),
    );
    expect(screen.queryByRole('button', { name: 'Load more Sprints' })).not.toBeInTheDocument();
  });

  it('keeps the loaded projection visible and retries only a failed cursor page', async () => {
    dashboardApi.getBoardDeliveryIntelligence
      .mockResolvedValueOnce(deliveryPage({ nextCursor: 'cursor-2' }))
      .mockRejectedValueOnce(new Error('Next page timed out.'))
      .mockResolvedValueOnce(deliveryPage({ sprints: [sprint('sprint-2', 'Sprint Beta')], nextCursor: null }));
    renderFullView();

    fireEvent.click(await screen.findByRole('button', { name: 'Load more Sprints' }));
    const paginationAlert = await screen.findByRole('alert');
    expect(paginationAlert).toHaveTextContent('Next page timed out.');
    expect(screen.getByRole('button', { name: 'Sprint Alpha' })).toBeInTheDocument();

    fireEvent.click(within(paginationAlert).getByRole('button', { name: 'Retry page' }));
    expect(await screen.findByRole('button', { name: 'Sprint Beta' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sprint Alpha' })).toBeInTheDocument();
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenCalledTimes(3);
  });

  it('ignores a stale response after the board changes', async () => {
    const stale = deferred<DeliveryIntelligenceResponse>();
    const current = deferred<DeliveryIntelligenceResponse>();
    dashboardApi.getBoardDeliveryIntelligence
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(current.promise);

    const view = renderFullView();
    view.rerender(<DeliveryIntelligenceFullView {...view.props} boardId="board-2" />);
    current.resolve(deliveryPage({ sprints: [sprint('sprint-2', 'Sprint Current')] }));

    expect(await screen.findByRole('button', { name: 'Sprint Current' })).toBeInTheDocument();
    stale.resolve(deliveryPage({ sprints: [sprint('sprint-1', 'Sprint Stale')] }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Sprint Stale' })).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Sprint Current' })).toBeInTheDocument();
  });

  it('keeps a non-ready forecast absent and gives the governed remediation', async () => {
    renderFullView();

    const forecastRegion = await screen.findByLabelText('Delivery forecast state');
    expect(within(forecastRegion).getAllByText('Insufficient History')).toHaveLength(2);
    expect(within(forecastRegion).getByText('Insufficient Observations')).toBeInTheDocument();
    expect(within(forecastRegion).getByText(/2\/5 observations · Complete more governed Sprints/)).toBeInTheDocument();
    expect(within(forecastRegion).queryByText('Forecast ready')).not.toBeInTheDocument();
  });
});
