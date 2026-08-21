import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CanonicalCoveragePanel } from './CanonicalCoveragePanel';
import { FlowHealthPanel } from './FlowHealthPanel';
import type { CanonicalCoverageResponse, FlowHealthResponse } from './analyticsCanonicalTypes';

const apiMock = vi.hoisted(() => ({
  getBoardFlowHealthSettings: vi.fn(),
  saveBoardFlowHealthSettings: vi.fn(),
  restoreBoardFlowHealthSettings: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

const coverage: CanonicalCoverageResponse = {
  contract_version: '1',
  query_fingerprint: 'a'.repeat(64),
  as_of: '2026-08-20T12:00:00Z',
  totals: {
    state: 'available',
    applicable: 1,
    covered: 1,
    uncovered: 0,
    skipped: 0,
    value: 100,
    n: 1,
    reason: null,
  },
  coverage: [{
    obligation_type: 'ac',
    state: 'available',
    applicable: 1,
    covered: 1,
    uncovered: 0,
    skipped: 0,
    value: 100,
    n: 1,
    reason: null,
    rows: [{
      identity: {
        spec_id: 'spec-1',
        obligation_type: 'ac',
        obligation_id: 'ac-1',
        edition: 7,
        currentness: 'current',
      },
      state: 'covered',
      applicable: true,
      covered: true,
      skip: { state: 'not_skipped', effective: false, reason_code: null, currentness: null },
      authority_ref: 'spec:spec-1:ac:ac-1',
      reason: null,
      evidence: [{
        evidence_id: 'evidence-1',
        evidence_type: 'test',
        source_ref: 'card:card-1',
        obligation: 'ac-1',
        relation_type: 'tests',
        evidence_content_sha256: 'b'.repeat(64),
        parent_card_id: 'card-1',
        delivery_state: 'active',
        lifecycle_status: 'active',
        currentness: 'current',
        currentness_reason: null,
        authority_ref: 'evidence:evidence-1',
        eligibility: 'eligible',
      }],
    }],
  }],
  code_evidence: {
    state: 'available',
    reason: null,
    targets: [{
      target_id: 'target-1',
      card_id: 'card-1',
      source_ref: 'card:card-1',
      revision: 3,
      lifecycle_status: 'active',
      delivery_state: 'active',
      currentness: 'current',
      currentness_reason: null,
      current_resolution_id: 'resolution-1',
    }],
    resolutions: [{
      resolution_id: 'resolution-1',
      target_id: 'target-1',
      target_revision: 3,
      state: 'resolved',
      currentness: 'current',
      currentness_reason: null,
      authority_ref: 'resolution:resolution-1',
    }],
    executions: [{
      execution_id: 'execution-1',
      target_id: 'target-1',
      target_revision: 3,
      disposition: 'touched',
      currentness: 'current',
      authority_ref: 'receipt:accepted-1',
    }],
    overlaps: [{
      overlap_id: 'overlap-1',
      target_a_id: 'target-1',
      target_b_id: 'target-2',
      resolution_a_id: 'resolution-1',
      resolution_b_id: 'resolution-2',
      severity: 'medium',
      disposition: 'accepted_parallel',
      currentness: 'current',
    }],
    waivers: [{
      waiver_id: 'waiver-1',
      entity_type: 'card',
      entity_id: 'card-1',
      scope: 'target_overlap',
      reason_code: 'documentation_only',
      active: true,
      currentness: 'current',
      authority_ref: 'waiver:waiver-1',
    }],
  },
};

const flowHealth: FlowHealthResponse = {
  contract_version: '1',
  query_fingerprint: 'c'.repeat(64),
  as_of: '2026-08-20T12:00:00Z',
  effective_policy: {
    version: 4,
    authority_ref: 'board:board-1:flow-health:v4',
    general_stale_hours: 48,
    rejected_stale_hours: 72,
    overrides: [{ state: 'in_progress', stale_hours: 24 }],
  },
  summary: {
    healthy: 0,
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
    threshold: {
      state: 'in_progress',
      stale_hours: 24,
      provenance: 'override',
      policy_version: 4,
      authority_ref: 'board:board-1:flow-health:v4',
    },
    current_episode: {
      state: 'in_progress',
      entered_at: '2026-08-19T12:00:00Z',
      age_seconds: 86400,
      entry_event_id: 'event-2',
      authority_ref: 'domain-event:event-2',
    },
    rework: [{
      attempt: 1,
      rejected_at: '2026-08-18T10:00:00Z',
      rejection_event_id: 'event-1',
      rejection_kind: 'quality',
      rejection_code: 'tests_failed',
      rejection_summary: 'Tests failed',
      resumed_at: '2026-08-18T11:00:00Z',
      completed_at: null,
    }],
    blockers: [{
      code: 'spec_pending_validation',
      authority_state: 'current',
      authority_ref: 'spec:spec-1:validation:current',
      effective_skip: false,
    }],
    source_authority: {
      source_name: 'domain_events',
      source_version: '1',
      authoritative_timestamp_field: 'occurred_at',
    },
  }],
};

describe('canonical Analytics A3/A4 panels', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: {
        version: 4,
        general_stale_hours: 48,
        rejected_stale_hours: 72,
        overrides: { in_progress: 24 },
      },
    });
    apiMock.saveBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: {
        version: 5,
        general_stale_hours: 36,
        rejected_stale_hours: 60,
        overrides: { in_progress: 24 },
      },
    });
    apiMock.restoreBoardFlowHealthSettings.mockResolvedValue({
      board_id: 'board-1',
      settings: {
        version: 6,
        general_stale_hours: 72,
        rejected_stale_hours: 96,
        overrides: {},
      },
    });
  });

  it('renders canonical KPIs, filters, and the complete per-Spec Code Evidence drilldown', () => {
    const onOpenSpec = vi.fn();
    render(
      <CanonicalCoveragePanel
        data={coverage}
        loading={false}
        error={null}
        exporting={false}
        from="2026-08-01"
        to="2026-08-20"
        specTitles={{ 'spec-1': 'Checkout reliability' }}
        onRetry={vi.fn()}
        onExport={vi.fn()}
        onOpenSpec={onOpenSpec}
      />,
    );

    const panel = screen.getByTestId('canonical-coverage-panel');
    expect(within(panel).getByRole('heading', { name: 'Canonical Coverage & Traceability' })).toBeInTheDocument();
    expect(within(panel).getByText('Per-Spec Code Evidence Matrix')).toBeInTheDocument();
    expect(within(within(panel).getByLabelText('Canonical coverage KPIs')).getByText('100%')).toBeInTheDocument();
    expect(within(panel).getByText('1 overlaps')).toBeInTheDocument();
    expect(within(panel).getByText('1 waivers')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: /Checkout reliability.*Edition 7/i }));
    expect(within(panel).getByRole('columnheader', { name: 'Evidence / receipt' })).toBeInTheDocument();
    expect(within(panel).getByText('evidence-1')).toBeInTheDocument();
    expect(within(panel).getByText('target-1')).toBeInTheDocument();
    expect(within(panel).getByText('resolution-1')).toBeInTheDocument();
    expect(within(panel).getByText('execution-1')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Open Spec' }));
    expect(onOpenSpec).toHaveBeenCalledWith('spec-1', 'Checkout reliability');

    fireEvent.change(within(panel).getByLabelText('Outcome'), { target: { value: 'skipped' } });
    expect(within(panel).getByText('No Specs match the current filters.')).toBeInTheDocument();
  });

  it('shows N/A rather than zero coverage when there are no applicable obligations', () => {
    render(
      <CanonicalCoveragePanel
        data={{
          ...coverage,
          totals: { ...coverage.totals, state: 'not_applicable', applicable: 0, covered: 0, value: null, n: 0 },
          coverage: [],
          code_evidence: { ...coverage.code_evidence!, state: 'not_applicable', targets: [], resolutions: [], executions: [], overlaps: [], waivers: [] },
        }}
        loading={false}
        error={null}
        exporting={false}
        from="2026-08-01"
        to="2026-08-20"
        specTitles={{}}
        onRetry={vi.fn()}
        onExport={vi.fn()}
        onOpenSpec={vi.fn()}
      />,
    );

    expect(screen.getByText('N/A')).toBeInTheDocument();
    expect(screen.getAllByText('No applicable obligations').length).toBeGreaterThan(0);
    expect(screen.getByText('No canonical obligations are applicable in this period.')).toBeInTheDocument();
  });

  it('renders governed Flow Health facts and persists/restores the versioned policy', async () => {
    const onReload = vi.fn();
    render(
      <FlowHealthPanel
        boardId="board-1"
        data={flowHealth}
        loading={false}
        error={null}
        exporting={false}
        from="2026-08-01"
        to="2026-08-20"
        subjectTitles={{ 'card:card-1': 'Checkout implementation' }}
        onRetry={vi.fn()}
        onExport={vi.fn()}
        onReload={onReload}
        onOpenSubject={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('flow-health-panel');
    const subjectButton = within(panel).getByRole('button', { name: 'Checkout implementation' });
    expect(subjectButton).toHaveAttribute('title', 'card:card-1');
    fireEvent.click(subjectButton);
    expect(within(panel).getByText('override')).toBeInTheDocument();
    expect(within(panel).getAllByText('spec_pending_validation').length).toBeGreaterThan(0);
    expect(within(panel).getByText('Tests failed')).toBeInTheDocument();
    expect(within(panel).getAllByText('Not supplied by the canonical Flow Health authority.')).toHaveLength(3);
    expect(within(panel).getByText('Authoritative Timestamp Field')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: /Thresholds/i }));
    await waitFor(() => expect(apiMock.getBoardFlowHealthSettings).toHaveBeenCalledWith('board-1'));
    fireEvent.change(within(panel).getByLabelText('General stale after (hours)'), { target: { value: '36' } });
    fireEvent.change(within(panel).getByLabelText('Rejected stale after (hours)'), { target: { value: '60' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Save policy' }));

    await waitFor(() => expect(apiMock.saveBoardFlowHealthSettings).toHaveBeenCalledWith('board-1', {
      expected_version: 4,
      general_stale_hours: 36,
      rejected_stale_hours: 60,
      overrides: { in_progress: 24 },
    }));
    expect(await within(panel).findByText('Flow Health policy saved.')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Restore defaults' }));
    await waitFor(() => expect(apiMock.restoreBoardFlowHealthSettings).toHaveBeenCalledWith('board-1', 5));
    expect(await within(panel).findByText('Default Flow Health policy restored.')).toBeInTheDocument();
    expect(onReload).toHaveBeenCalledTimes(2);
  });
});
