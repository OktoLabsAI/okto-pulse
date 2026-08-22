import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DeliveryForecastPanel } from './DeliveryForecastPanel';
import { KgEffectivenessPanel } from './KgEffectivenessPanel';
import type { BoardKgAnalyticsResponse } from './analyticsCanonicalTypes';
import type { DeliveryForecastResponse, SprintAnalyticsResponse } from './analyticsDeliveryTypes';

const sprints: SprintAnalyticsResponse = {
  contract_version: '1',
  query_fingerprint: 'a'.repeat(64),
  as_of: '2026-08-21T12:00:00Z',
  summary: {
    total_sprints: 0,
    status_breakdown: {},
    avg_completion_rate: null,
    sprint_evaluation: { total_submitted: 0, approve_rate: null, avg_overall_score: null },
  },
  sprints: [],
};

const forecastBase = {
  contract_version: '1',
  dependency_versions: { analytics_foundation: '1', delivery_phase_1: '1' },
  query_fingerprint: 'b'.repeat(64),
  filters: [],
  as_of: '2026-08-21T12:00:00Z',
  board_id: 'board-1',
  provenance: {
    observed_at: '2026-08-21T12:00:00Z',
    currentness: 'current' as const,
    reason: null,
    sources: [{ authority: 'sprint_delivery', reference: 'board:board-1', timestamp_field: 'completed_at' }],
  },
  population_scope: { scope_ref: 'board:board-1', accessible_count: 8, excluded_count: 0 },
  exclusions: { restricted_count: 0, excluded_count: 0, reasons: [] },
};

describe('Analytics A5/A6 panels', () => {
  it('renders only the governed ready forecast, confidence bounds and backtest', () => {
    const onExport = vi.fn().mockResolvedValue(undefined);
    const forecast: DeliveryForecastResponse = {
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
        source_period: { from: '2026-07-01T00:00:00Z', to: '2026-08-21T00:00:00Z' },
        method_version: 'empirical-v1',
      },
      backtest: {
        state: 'available',
        error: 1.5,
        calibration: 0.82,
        method_version: 'empirical-v1',
        sample_size: 5,
        evaluation_window: { from: '2026-07-01T00:00:00Z', to: '2026-08-01T00:00:00Z' },
        reason: null,
      },
    };

    render(<DeliveryForecastPanel sprints={sprints} forecast={forecast} forecastLoading={false} forecastError={null} forecastExporting={false} from="2026-07-01" to="2026-08-21" onRetryForecast={vi.fn()} onExportForecast={onExport} />);
    const panel = screen.getByTestId('delivery-forecast-panel');
    expect(within(panel).getByRole('heading', { name: 'Sprint Delivery & Forecasting' })).toBeInTheDocument();
    expect(within(panel).getByText('9 → 15')).toBeInTheDocument();
    expect(within(panel).getByText('stable_scope, observed_history_only')).toBeInTheDocument();
    expect(within(panel).getByText(/Error 1.5 · calibration 0.82/)).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole('button', { name: 'Complete CSV' }));
    expect(onExport).toHaveBeenCalledTimes(1);
  });

  it('keeps a non-ready forecast explicitly absent and shows remediation', () => {
    const forecast: DeliveryForecastResponse = {
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

    render(<DeliveryForecastPanel sprints={sprints} forecast={forecast} forecastLoading={false} forecastError={null} forecastExporting={false} from="2026-07-01" to="2026-08-21" onRetryForecast={vi.fn()} onExportForecast={vi.fn()} />);
    const panel = screen.getByTestId('delivery-forecast-panel');
    expect(within(panel).getByText(/Complete more governed Sprints/)).toBeInTheDocument();
    expect(within(panel).getAllByText('Unavailable').length).toBeGreaterThan(0);
    expect(within(panel).queryByText('Confidence bounds')).not.toBeNull();
    expect(within(panel).queryByText(/stable_scope/)).not.toBeInTheDocument();
  });

  it('renders KG health separately from partial effectiveness and exposes v2 debt facts', () => {
    const data: BoardKgAnalyticsResponse = {
      contract_version: '2',
      foundation_version: '1',
      query_fingerprint: 'c'.repeat(64),
      query: { window: { from: '2026-08-01T00:00:00Z', to: '2026-08-22T00:00:00Z' }, cognitive_status: [], artifact_types: [], cursor: null, limit: 100 },
      filters: [],
      as_of: '2026-08-21T12:00:00Z',
      board_id: 'board-1',
      result_state: 'partial',
      provenance: { observed_at: '2026-08-21T12:00:00Z', currentness: 'partial', reason: 'one_domain_unavailable', sources: [{ authority: 'kg_health', reference: 'board:board-1', timestamp_field: 'observed_at' }] },
      health: {
        state: 'healthy',
        classification_reason: 'within_operational_policy',
        reason_codes: [],
        availability: {
          active_queue: 'available',
          technical_dlq: 'available',
          canonical_debt: 'available',
          policy_projection_debt: 'available',
          cognitive_backlog: 'available',
          canonical_partition: 'available',
        },
        components: [{ component: 'canonical_partition', health_state: 'healthy', result_state: 'available', classification_reason: 'canonical_partition_healthy' }],
      },
      domains: [
        { domain: 'active_queue', result_state: 'available', count: 4, severity: 'at_risk', age: { result_state: 'available', sample_count: 4, p50_hours: 2, p95_hours: 7, oldest_hours: 9, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
        { domain: 'technical_dlq', result_state: 'available', count: 1, severity: 'blocking', age: { result_state: 'available', sample_count: 1, p50_hours: 10, p95_hours: 10, oldest_hours: 10, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
        { domain: 'canonical_debt', result_state: 'available', count: 2, severity: 'at_risk', age: { result_state: 'available', sample_count: 2, p50_hours: 8, p95_hours: 12, oldest_hours: 14, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
        { domain: 'policy_projection_debt', result_state: 'available', count: 3, severity: 'at_risk', age: { result_state: 'available', sample_count: 3, p50_hours: 4, p95_hours: 9, oldest_hours: 11, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
        { domain: 'cognitive_backlog', result_state: 'available', count: 5, severity: 'informational', age: { result_state: 'available', sample_count: 5, p50_hours: 5, p95_hours: 15, oldest_hours: 18, reason: null }, drill_down: { allowed: false, target: null }, reason: null },
      ],
      cognitive_inventory: { result_state: 'available', by_status: { pending: 3, in_progress: 2, consolidated: 6 }, total: 11, overdue_revisits: 1, age: { result_state: 'available', sample_count: 11, p50_hours: 4, p95_hours: 14, oldest_hours: 20, reason: null }, reason: null },
      effectiveness: { state: 'available', numerator: 6, denominator: 8, rate: 0.75, candidate_count: 8, persisted_count: 6, conversion_rate: 0.75, method_version: 'candidate-persistence-v1', sample_period: { from: '2026-08-01T00:00:00Z', to: '2026-08-22T00:00:00Z' }, timing: { state: 'available', sample_count: 6, p50_hours: 2.5, p95_hours: 8, reason: null }, reason: null },
      provenance_mix: { result_state: 'available', total: 8, by_kind: { cognitive: { count: 6, rate: 0.75 }, deterministic: { count: 2, rate: 0.25 } }, reason: null },
      diagnostics: [{ domain: 'technical_dlq', severity: 'blocking', reason: 'one_item_requires_recovery', next_step: { allowed: false, target: null } }],
      redactions: [],
      population_scope: { scope_ref: 'board:board-1', accessible_count: 8, excluded_count: 0 },
      exclusions: { restricted_count: 0, excluded_count: 0, reasons: [] },
      next_cursor: null,
    };

    render(<KgEffectivenessPanel data={data} loading={false} error={null} exporting={false} from="2026-08-01" to="2026-08-21" onRetry={vi.fn()} onExport={vi.fn()} />);
    const panel = screen.getByTestId('kg-effectiveness-panel');
    expect(within(panel).getByRole('heading', { name: 'Board KG Analytics' })).toBeInTheDocument();
    expect(within(panel).getByLabelText('KG health and availability')).toHaveTextContent('Healthy');
    expect(within(panel).getByLabelText('KG health and availability')).toHaveTextContent('Partial');
    expect(within(panel).getAllByText('75%').length).toBeGreaterThanOrEqual(2);
    expect(within(panel).getByText('2.5h')).toBeInTheDocument();
    expect(within(panel).getByRole('row', { name: /Policy Projection Debt.*3/ })).toBeInTheDocument();
    expect(within(panel).getByText('One Item Requires Recovery')).toBeInTheDocument();
  });
});
