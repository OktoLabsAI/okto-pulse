import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { KgEffectivenessPanel } from './KgEffectivenessPanel';
import type { BoardKgAnalyticsResponse } from './analyticsCanonicalTypes';

describe('Analytics KG panels', () => {
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
