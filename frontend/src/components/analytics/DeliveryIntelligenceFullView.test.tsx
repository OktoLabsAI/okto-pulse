import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeliveryIntelligenceFullView } from './DeliveryIntelligenceFullView';
import type {
  DeliveryIntelligenceResponse,
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

function deliveryPage({
  resultState = 'available',
  subject = "You",
  nextCursor = null,
}: {
  resultState?: DeliveryIntelligenceResponse['result_state'];
  subject?: string;
  nextCursor?: string | null;
} = {}): DeliveryIntelligenceResponse {
  return {
    contract_version: '2',
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
      sources: [{ authority: 'board_cards_created_in_window', reference: 'board:board-1', timestamp_field: 'completed_at' }],
    },
    population_scope: { scope_ref: 'board:board-1', accessible_count: 8, excluded_count: 0 },
    exclusions: { restricted_count: resultState === 'restricted' ? 8 : 0, excluded_count: 0, reasons: [] },
    minimum_sample_size: 5,
    contributions: resultState === 'available' || resultState === 'partial' ? [{
      subject_id: subject,
      subject_label: subject,
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

function renderFullView(overrides: Partial<React.ComponentProps<typeof DeliveryIntelligenceFullView>> = {}) {
  const props: React.ComponentProps<typeof DeliveryIntelligenceFullView> = {
    boardId: 'board-1',
    ...period,
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
    dashboardApi.exportBoardDeliveryIntelligenceCsv.mockResolvedValue(undefined);
  });

  it('renders available facts, emits governed filters, exports, without Sprint controls', async () => {
    const onFiltersChange = vi.fn();
    const onPeriodChange = vi.fn();
    renderFullView({ onFiltersChange, onPeriodChange });

    const page = await screen.findByTestId('delivery-intelligence-full-view');
    expect(within(page).getByRole('heading', { name: 'Delivery Intelligence' })).toBeInTheDocument();
    expect((await within(page).findAllByText('87.5%')).length).toBeGreaterThan(0);

    expect(within(page).queryByLabelText('Delivery Sprint')).not.toBeInTheDocument();
    expect(within(page).queryByLabelText('Delivery lane')).not.toBeInTheDocument();
    expect(within(page).getByText(/Cards created in the selected period/)).toBeInTheDocument();
    // Filtering stays possible when a role has no rows on the current page.
    expect(within(page).getByRole('option', { name: 'Validation agent' })).toBeInTheDocument();
    expect(within(page).getByRole('option', { name: 'Implementation agent' })).toBeInTheDocument();
    fireEvent.change(within(page).getByLabelText('Contribution role'), { target: { value: 'developer' } });
    fireEvent.change(within(page).getByLabelText('Contribution visibility'), { target: { value: 'self' } });
    expect(onFiltersChange).toHaveBeenLastCalledWith({
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
      expect.objectContaining({ role: 'developer', contributionView: 'self' }),
    ));
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
    dashboardApi.getBoardDeliveryIntelligence.mockResolvedValue(deliveryPage({ resultState: 'empty' }));
    renderFullView();

    expect(await screen.findByText('No delivery evidence in this period')).toBeInTheDocument();
    expect(screen.getByText(/Change the period, role/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Delivery summary')).not.toBeInTheDocument();
  });

  it('keeps an empty projection visible and retries only a failed export', async () => {
    dashboardApi.getBoardDeliveryIntelligence.mockResolvedValue(deliveryPage({ resultState: 'empty' }));
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

  it('locks all projection filters while a CSV export is pending', async () => {
    const pendingExport = deferred<void>();
    dashboardApi.exportBoardDeliveryIntelligenceCsv.mockReturnValue(
      pendingExport.promise,
    );
    renderFullView();

    const exportButton = await screen.findByRole('button', { name: 'Export CSV' });
    await waitFor(() => expect(exportButton).toBeEnabled());
    fireEvent.click(exportButton);

    await waitFor(() => expect(exportButton).toHaveTextContent('Exporting…'));
    for (const label of [
      'Delivery period',
      'Contribution role',
      'Contribution visibility',
    ]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }

    pendingExport.resolve();
    await waitFor(() => expect(exportButton).toBeEnabled());
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
    expect(await screen.findByText('You')).toBeInTheDocument();
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenCalledTimes(2);
  });

  it('loads the next cursor page and appends contributions without losing the first page', async () => {
    dashboardApi.getBoardDeliveryIntelligence.mockImplementation((
      _boardId: string,
      _from: string,
      _to: string,
      filters: { cursor?: string },
    ) => Promise.resolve(filters.cursor === 'cursor-2'
      ? deliveryPage({ subject: 'Agent Beta', nextCursor: null })
      : deliveryPage({ nextCursor: 'cursor-2' })));
    renderFullView();

    fireEvent.click(await screen.findByRole('button', { name: 'Load more contributions' }));
    expect(await screen.findByText('Agent Beta')).toBeInTheDocument();
    expect(screen.getByText('You')).toBeInTheDocument();
    expect(dashboardApi.getBoardDeliveryIntelligence).toHaveBeenLastCalledWith(
      'board-1',
      period.from,
      period.to,
      expect.objectContaining({ cursor: 'cursor-2', limit: 25 }),
    );
    expect(screen.queryByRole('button', { name: 'Load more contributions' })).not.toBeInTheDocument();
  });

  it('keeps the loaded projection visible and retries only a failed cursor page', async () => {
    dashboardApi.getBoardDeliveryIntelligence
      .mockResolvedValueOnce(deliveryPage({ nextCursor: 'cursor-2' }))
      .mockRejectedValueOnce(new Error('Next page timed out.'))
      .mockResolvedValueOnce(deliveryPage({ subject: 'Agent Beta', nextCursor: null }));
    renderFullView();

    fireEvent.click(await screen.findByRole('button', { name: 'Load more contributions' }));
    const paginationAlert = await screen.findByRole('alert');
    expect(paginationAlert).toHaveTextContent('Next page timed out.');
    expect(screen.getByText('You')).toBeInTheDocument();

    fireEvent.click(within(paginationAlert).getByRole('button', { name: 'Retry page' }));
    expect(await screen.findByText('Agent Beta')).toBeInTheDocument();
    expect(screen.getByText('You')).toBeInTheDocument();
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
    current.resolve(deliveryPage({ subject: 'Agent Current' }));

    expect(await screen.findByText('Agent Current')).toBeInTheDocument();
    stale.resolve(deliveryPage({ subject: 'Agent Stale' }));
    await waitFor(() => expect(screen.queryByText('Agent Stale')).not.toBeInTheDocument());
    expect(screen.getByText('Agent Current')).toBeInTheDocument();
  });

  it('keeps contributions without requesting or rendering the retired forecast', async () => {
    renderFullView();
    await screen.findByRole('heading', { name: 'Contribution by role' });
    expect(dashboardApi.getBoardDeliveryForecast).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Delivery forecast state')).not.toBeInTheDocument();
    expect(screen.queryByText(/Forecast ready|forecast readiness/i)).not.toBeInTheDocument();
  });
});
