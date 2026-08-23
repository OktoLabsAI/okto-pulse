import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlowHealthResponse } from './analyticsCanonicalTypes';

const apiMock = vi.hoisted(() => ({
  getBoardFlowHealth: vi.fn(),
  exportBoardFlowHealthCsv: vi.fn(),
  getBoardFlowHealthSettings: vi.fn(),
  saveBoardFlowHealthSettings: vi.fn(),
  restoreBoardFlowHealthSettings: vi.fn(),
  getBoardAnalyticsEntities: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));

import { FlowHealthFullView } from './FlowHealthFullView';
import { FlowHealthSettingsPage } from './FlowHealthSettingsPage';
import { FlowHealthSummary } from './FlowHealthSummary';

const flowHealth: FlowHealthResponse = {
  contract_version: '1',
  query_fingerprint: 'f'.repeat(64),
  as_of: '2026-08-21T12:00:00Z',
  effective_policy: {
    version: 4,
    source: 'board_override',
    authority_ref: 'board:board-1:flow-health:v4',
    general_stale_hours: 48,
    rejected_stale_hours: 72,
    overrides: [],
  },
  summary: {
    blocked: 1,
    blocker_occurrences: 2,
    blocker_entities: 1,
    rejected_wip: 1,
    rejected_age_p95_hours: 31,
    recovery_rate: 0.75,
    recovery_n: 4,
    dependency_wait_p50_hours: 12,
    dependency_depth: 3,
    open_bugs: 2,
    high_severity_bugs: 1,
  },
  items: [{
    subject: { type: 'card', id: 'card-1', title: 'Billing retry contract' },
    state: 'blocked',
    owner: 'Maya',
    reason_codes: ['spec_pending_validation'],
    current_episode: {
      state: 'rejected',
      age_seconds: 111600,
      entered_at: '2026-08-20T05:00:00Z',
      authority_ref: 'event:event-1',
    },
    threshold: {
      state: 'rejected',
      stale_hours: 72,
      policy_version: 4,
      authority_ref: 'board:board-1:flow-health:v4',
    },
    blockers: [{
      code: 'spec_pending_validation',
      authority_state: 'current',
      authority_ref: 'spec:spec-1:validation:current',
      remediation: 'Complete the Current validation.',
    }, {
      code: 'rework_required',
      authority_state: 'current',
      authority_ref: 'card:card-1:rejection:current',
    }],
    rework: [{ attempt: 1, rejected_at: '2026-08-20T05:00:00Z', completed_at: null }],
  }],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => { resolve = resolver; });
  return { promise, resolve };
}

describe('Flow Health governed surfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoardFlowHealth.mockResolvedValue(flowHealth);
    apiMock.getBoardAnalyticsEntities.mockResolvedValue({ total: 0, items: [] });
    apiMock.exportBoardFlowHealthCsv.mockResolvedValue(undefined);
    apiMock.getBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: { version: 1, general_stale_hours: 72, rejected_stale_hours: 96, overrides: {} },
    });
    apiMock.saveBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: { version: 2, general_stale_hours: 73, rejected_stale_hours: 96, overrides: {} },
    });
    apiMock.restoreBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: { version: 3, general_stale_hours: 72, rejected_stale_hours: 96, overrides: {} },
    });
  });

  it('keeps the dashboard summary compact and opens the dedicated view', () => {
    const onOpenFullView = vi.fn();
    render(<FlowHealthSummary data={flowHealth} loading={false} error={null} from="2026-08-01" to="2026-08-21" onRetry={vi.fn()} onOpenFullView={onOpenFullView} />);

    const summary = screen.getByTestId('flow-health-summary');
    expect(within(summary).getAllByText('2', { selector: 'p' }).length).toBeGreaterThan(0);
    expect(within(summary).getByText('75%')).toBeInTheDocument();
    expect(within(summary).getByText('12h')).toBeInTheDocument();
    expect(within(summary).queryByText('Blockers & remediation')).not.toBeInTheDocument();
    fireEvent.click(within(summary).getByRole('button', { name: /Open full view/i }));
    expect(onOpenFullView).toHaveBeenCalledOnce();
  });

  it('renders the full operational view with URL-controlled filters, human identity, and separate settings navigation', async () => {
    const onFiltersChange = vi.fn();
    const onOpenSettings = vi.fn();
    render(
      <FlowHealthFullView
        boardId="board-1"
        from="2026-08-01"
        to="2026-08-21"
        filters={{ search: '', workType: 'all', owner: 'all', health: 'all', blockersOnly: false }}
        onFiltersChange={onFiltersChange}
        onBack={vi.fn()}
        onOpenSettings={onOpenSettings}
        onSelectEntity={vi.fn()}
      />,
    );

    expect(await screen.findByRole('heading', { name: 'Flow Health & Governed Rework' })).toBeInTheDocument();
    const panel = screen.getByTestId('flow-health-panel');
    expect(within(panel).getAllByText('Billing retry contract').length).toBeGreaterThan(0);
    expect(within(panel).getAllByText('Card · card-1').length).toBeGreaterThan(0);
    expect(within(panel).getByText('75%')).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: 'Thresholds' })).not.toBeInTheDocument();

    fireEvent.change(within(panel).getByLabelText('Work type'), { target: { value: 'card' } });
    expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ workType: 'card' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Board settings' })[0]);
    expect(onOpenSettings).toHaveBeenCalledOnce();
  });

  it('keeps loaded Flow Health data visible when CSV export fails', async () => {
    apiMock.exportBoardFlowHealthCsv.mockRejectedValue(new Error('download unavailable'));
    render(
      <FlowHealthFullView
        boardId="board-1"
        from="2026-08-01"
        to="2026-08-21"
        filters={{ search: '', workType: 'all', owner: 'all', health: 'all', blockersOnly: false }}
        onFiltersChange={vi.fn()}
        onBack={vi.fn()}
        onOpenSettings={vi.fn()}
        onSelectEntity={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId('flow-health-panel');
    expect(within(panel).getAllByText('Billing retry contract').length).toBeGreaterThan(0);
    fireEvent.click(within(panel).getByRole('button', { name: 'Complete CSV' }));

    expect(await within(panel).findByText(/CSV export failed: download unavailable/)).toBeInTheDocument();
    expect(within(panel).getAllByText('Billing retry contract').length).toBeGreaterThan(0);
    fireEvent.click(within(panel).getByRole('button', { name: 'Retry export' }));
    await waitFor(() => expect(apiMock.exportBoardFlowHealthCsv).toHaveBeenCalledTimes(2));
  });

  it('persists and restores the versioned Board policy on its separate surface', async () => {
    render(<FlowHealthSettingsPage boardId="board-1" onBack={vi.fn()} />);

    const general = await screen.findByLabelText('General stale after');
    fireEvent.change(general, { target: { value: '73' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save thresholds' }));
    await waitFor(() => expect(apiMock.saveBoardFlowHealthSettings).toHaveBeenCalledWith('board-1', {
      expected_version: 1,
      general_stale_hours: 73,
      rejected_stale_hours: 96,
      overrides: {},
    }));
    expect(await screen.findByText(/Flow Health policy saved/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Restore safe defaults' }));
    await waitFor(() => expect(apiMock.restoreBoardFlowHealthSettings).toHaveBeenCalledWith('board-1', 2));
    expect(await screen.findByText(/Safe defaults restored/)).toBeInTheDocument();
  });

  it('ignores a superseded settings response after navigating to another board', async () => {
    const stale = deferred<Awaited<ReturnType<typeof apiMock.getBoardFlowHealthSettings>>>();
    const current = deferred<Awaited<ReturnType<typeof apiMock.getBoardFlowHealthSettings>>>();
    apiMock.getBoardFlowHealthSettings
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(current.promise);

    const view = render(<FlowHealthSettingsPage boardId="board-1" onBack={vi.fn()} />);
    view.rerender(<FlowHealthSettingsPage boardId="board-2" onBack={vi.fn()} />);
    current.resolve({
      board_id: 'board-2',
      settings: { version: 7, general_stale_hours: 24, rejected_stale_hours: 48, overrides: {} },
    });

    expect(await screen.findByText('Effective policy v7')).toBeInTheDocument();
    expect(screen.getByLabelText('General stale after')).toHaveValue(24);
    stale.resolve({
      board_id: 'board-1',
      settings: { version: 1, general_stale_hours: 72, rejected_stale_hours: 96, overrides: {} },
    });
    await waitFor(() => expect(screen.queryByText('Effective policy v1')).not.toBeInTheDocument());
    expect(screen.getByLabelText('General stale after')).toHaveValue(24);
  });

  it('does not coerce restricted authority to healthy zeroes', () => {
    const restricted: FlowHealthResponse = {
      ...flowHealth,
      summary: { restricted: 1 },
      items: [],
    };
    render(<FlowHealthSummary data={restricted} loading={false} error={null} from="2026-08-01" to="2026-08-21" onRetry={vi.fn()} onOpenFullView={vi.fn()} />);
    expect(screen.getByText(/Flow Health is restricted/)).toBeInTheDocument();
    expect(screen.getAllByText('N/A').length).toBeGreaterThan(0);
    expect(screen.queryByText('Flow Health is healthy')).not.toBeInTheDocument();
  });
});
