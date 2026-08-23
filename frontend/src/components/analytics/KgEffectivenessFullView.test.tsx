import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { KgEffectivenessFullView } from './KgEffectivenessFullView';
import { KgEffectivenessPanel } from './KgEffectivenessPanel';
import { mergeBoardKgAnalyticsPages } from './kgEffectivenessPagination';
import type {
  BoardKgAnalyticsQueryOptions,
  BoardKgAnalyticsResponse,
  BoardKgAnalyticsState,
} from './analyticsCanonicalTypes';

const dashboardApi = vi.hoisted(() => ({
  getBoardKgAnalytics: vi.fn(),
  exportBoardKgAnalyticsCsv: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => dashboardApi,
}));

function kgPage({
  resultState = 'available',
  nextCursor = 'offset:2',
  numerator = 6,
  denominator = 8,
  inventoryTotal = 11,
  cognitiveBacklog = 5,
}: {
  resultState?: BoardKgAnalyticsState;
  nextCursor?: string | null;
  numerator?: number;
  denominator?: number;
  inventoryTotal?: number;
  cognitiveBacklog?: number;
} = {}): BoardKgAnalyticsResponse {
  return {
    contract_version: '2',
    foundation_version: '1',
    query_fingerprint: 'c'.repeat(64),
    query: {
      window: { from: '2026-08-01T00:00:00Z', to: '2026-08-22T00:00:00Z' },
      cognitive_status: [],
      artifact_types: [],
      cursor: null,
      limit: 2,
    },
    filters: [],
    as_of: '2026-08-21T12:00:00Z',
    board_id: 'board-1',
    result_state: resultState,
    provenance: {
      observed_at: '2026-08-21T12:00:00Z',
      currentness: resultState === 'available' ? 'current' : 'partial',
      reason: null,
      sources: [{ authority: 'kg_health', reference: 'board:board-1', timestamp_field: 'observed_at' }],
    },
    health: {
      state: 'healthy',
      classification_reason: 'within_operational_policy',
      reason_codes: [],
      availability: {
        active_queue: 'available',
        technical_dlq: 'available',
        canonical_debt: 'available',
        policy_projection_debt: 'available',
        cognitive_backlog: resultState,
      },
      components: [{ component: 'canonical_partition', health_state: 'healthy', result_state: 'available', classification_reason: 'canonical_partition_healthy' }],
    },
    domains: [
      { domain: 'active_queue', result_state: 'available', count: 4, severity: 'at_risk', age: { result_state: 'available', sample_count: 4, p50_hours: 2, p95_hours: 7, oldest_hours: 9, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
      { domain: 'technical_dlq', result_state: 'available', count: 1, severity: 'blocking', age: { result_state: 'available', sample_count: 1, p50_hours: 10, p95_hours: 10, oldest_hours: 10, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
      { domain: 'canonical_debt', result_state: 'available', count: 2, severity: 'at_risk', age: { result_state: 'available', sample_count: 2, p50_hours: 8, p95_hours: 12, oldest_hours: 14, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
      { domain: 'policy_projection_debt', result_state: 'available', count: 3, severity: 'at_risk', age: { result_state: 'available', sample_count: 3, p50_hours: 4, p95_hours: 9, oldest_hours: 11, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
      { domain: 'cognitive_backlog', result_state: resultState, count: cognitiveBacklog, severity: 'informational', age: { result_state: resultState, sample_count: cognitiveBacklog, p50_hours: 5, p95_hours: 15, oldest_hours: 18, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
    ],
    cognitive_inventory: {
      result_state: resultState,
      by_status: { pending: inventoryTotal },
      total: inventoryTotal,
      overdue_revisits: 1,
      age: { result_state: resultState, sample_count: inventoryTotal, p50_hours: 4, p95_hours: 14, oldest_hours: 20, reason: null },
      reason: null,
    },
    effectiveness: {
      state: resultState === 'available' || resultState === 'empty' || resultState === 'restricted' || resultState === 'unavailable' ? resultState : 'available',
      numerator,
      denominator,
      rate: denominator === 0 ? null : numerator / denominator,
      candidate_count: denominator,
      persisted_count: numerator,
      conversion_rate: denominator === 0 ? null : numerator / denominator,
      method_version: 'candidate-persistence-v1',
      sample_period: { from: '2026-08-01T00:00:00Z', to: '2026-08-22T00:00:00Z' },
      timing: { state: 'available', sample_count: numerator, p50_hours: 2.5, p95_hours: 8, reason: null },
      reason: null,
    },
    provenance_mix: {
      result_state: resultState,
      total: denominator,
      by_kind: { cognitive: { count: numerator, rate: denominator === 0 ? null : numerator / denominator } },
      reason: null,
    },
    diagnostics: [],
    redactions: [],
    population_scope: { scope_ref: 'board:board-1', accessible_count: 1, excluded_count: 0 },
    exclusions: { restricted_count: 0, excluded_count: 0, reasons: [] },
    next_cursor: nextCursor,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => { resolve = resolver; });
  return { promise, resolve };
}

describe('KG effectiveness A6 UI', () => {
  beforeEach(() => {
    dashboardApi.getBoardKgAnalytics.mockReset();
    dashboardApi.exportBoardKgAnalyticsCsv.mockReset();
    dashboardApi.exportBoardKgAnalyticsCsv.mockResolvedValue(undefined);
  });

  it('keeps the board mode compact and opens the dedicated full view through its callback', () => {
    const onOpenFullView = vi.fn();
    render(
      <KgEffectivenessPanel
        data={kgPage({ nextCursor: null })}
        loading={false}
        error={null}
        exporting={false}
        from="2026-08-01"
        to="2026-08-21"
        onRetry={vi.fn()}
        onExport={vi.fn()}
        mode="compact"
        onOpenFullView={onOpenFullView}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));

    expect(onOpenFullView).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('table', { name: 'KG operational debt domains' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('KG effectiveness summary KPIs')).toBeInTheDocument();
  });

  it.each<[BoardKgAnalyticsState, string]>([
    ['available', 'Canonical KG data is available for this selection.'],
    ['partial', 'Some canonical KG facts are unavailable. Available facts remain visible and are not inferred.'],
    ['empty', 'No authorized KG records match this selection.'],
    ['restricted', 'You do not have access to this canonical KG projection.'],
    ['unavailable', 'The canonical KG authority is unavailable. No result was inferred.'],
    ['error', 'The canonical KG request failed. Retry when ready.'],
  ])('renders the %s result as an explicit state', (state, message) => {
    render(
      <KgEffectivenessPanel
        data={kgPage({ resultState: state, nextCursor: null })}
        loading={false}
        error={null}
        exporting={false}
        from="2026-08-01"
        to="2026-08-21"
        onRetry={vi.fn()}
        onExport={vi.fn()}
        mode="compact"
        onOpenFullView={vi.fn()}
      />,
    );

    expect(screen.getByTestId(`kg-result-state-${state}`)).toHaveTextContent(message);
  });

  it('applies cognitive and artifact filters on the server and accumulates cursor pages', async () => {
    const onFiltersChange = vi.fn();
    dashboardApi.getBoardKgAnalytics.mockImplementation((...args: unknown[]) => {
      const options = args[3] as BoardKgAnalyticsQueryOptions;
      return Promise.resolve(options.cursor
        ? kgPage({ nextCursor: null, numerator: 1, denominator: 2, inventoryTotal: 2, cognitiveBacklog: 1 })
        : kgPage());
    });

    render(<KgEffectivenessFullView boardId="board-1" boardLabel="E2E" from="2026-08-01" to="2026-08-21" pageLimit={2} onFiltersChange={onFiltersChange} />);
    await waitFor(() => expect(dashboardApi.getBoardKgAnalytics).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText('Failed'));
    fireEvent.click(screen.getByLabelText('Spec'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await waitFor(() => expect(dashboardApi.getBoardKgAnalytics).toHaveBeenCalledTimes(2));
    expect(dashboardApi.getBoardKgAnalytics).toHaveBeenLastCalledWith(
      'board-1',
      '2026-08-01',
      '2026-08-21',
      { cognitiveStatus: ['failed'], artifactTypes: ['spec'], cursor: null, limit: 2 },
    );
    expect(onFiltersChange).toHaveBeenCalledWith({
      from: '2026-08-01',
      to: '2026-08-21',
      cognitiveStatus: ['failed'],
      artifactTypes: ['spec'],
      limit: 2,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(dashboardApi.getBoardKgAnalytics).toHaveBeenCalledTimes(3));
    expect(dashboardApi.getBoardKgAnalytics).toHaveBeenLastCalledWith(
      'board-1',
      '2026-08-01',
      '2026-08-21',
      { cognitiveStatus: ['failed'], artifactTypes: ['spec'], cursor: 'offset:2', limit: 2 },
    );

    expect(await screen.findByText('2 pages loaded')).toBeInTheDocument();
    const kpis = screen.getByLabelText('KG effectiveness KPIs');
    expect(within(kpis).getByText('7 / 10')).toBeInTheDocument();
    expect(within(kpis).getByText('13')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'All records loaded' })).toBeDisabled();
  });

  it('does not double-count repeated operational facts or fabricate cross-page quantiles', () => {
    const merged = mergeBoardKgAnalyticsPages([
      kgPage(),
      kgPage({ nextCursor: null, numerator: 1, denominator: 2, inventoryTotal: 2, cognitiveBacklog: 1 }),
    ]);

    expect(merged?.domains.find((domain) => domain.domain === 'active_queue')?.count).toBe(4);
    expect(merged?.domains.find((domain) => domain.domain === 'cognitive_backlog')?.count).toBe(6);
    expect(merged?.cognitive_inventory.total).toBe(13);
    expect(merged?.cognitive_inventory.age).toMatchObject({
      result_state: 'partial',
      p50_hours: null,
      p95_hours: null,
    });
    expect(merged?.effectiveness.timing).toMatchObject({
      state: 'unavailable',
      p50_hours: null,
      p95_hours: null,
    });
  });

  it('clears stale pagination loading when a first-page replacement starts', async () => {
    const pagination = deferred<BoardKgAnalyticsResponse>();
    dashboardApi.getBoardKgAnalytics
      .mockResolvedValueOnce(kgPage())
      .mockReturnValueOnce(pagination.promise)
      .mockResolvedValueOnce(kgPage());

    const view = render(<KgEffectivenessFullView boardId="board-1" from="2026-08-01" to="2026-08-21" pageLimit={2} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(screen.getByRole('button', { name: 'Load more' })).toBeDisabled();

    view.rerender(<KgEffectivenessFullView boardId="board-1" from="2026-08-02" to="2026-08-21" pageLimit={2} />);
    await waitFor(() => expect(dashboardApi.getBoardKgAnalytics).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load more' })).toBeEnabled());

    pagination.resolve(kgPage({ nextCursor: null }));
    await waitFor(() => expect(screen.getByText('1 page loaded')).toBeInTheDocument());
  });

  it('maps authorization failures to Restricted instead of a generic empty result', async () => {
    dashboardApi.getBoardKgAnalytics.mockRejectedValue(new AuthenticatedFetchError({
      message: 'Board analytics access denied.',
      status: 403,
      code: 'analytics_restricted',
    }));

    render(<KgEffectivenessFullView boardId="board-1" from="2026-08-01" to="2026-08-21" />);

    expect(await screen.findByTestId('kg-result-state-restricted')).toHaveTextContent('You do not have access');
    expect(screen.queryByText('No authorized KG records match this selection.')).not.toBeInTheDocument();
  });
});
