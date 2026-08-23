import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CanonicalCoverageFullView } from './CanonicalCoverageFullView';
import { CanonicalCoveragePanel } from './CanonicalCoveragePanel';
import type { CanonicalCoverageResponse } from './analyticsCanonicalTypes';
import {
  canonicalCoverageFullViewPath,
  canonicalCoverageQueryState,
  parseCanonicalCoverageQuery,
} from './canonicalCoverageQueryState';

const query = canonicalCoverageQueryState({
  from: '2026-08-01',
  to: '2026-08-21',
  lifecycle: 'active',
  outcome: 'incomplete',
  search: 'checkout',
});

const coverage: CanonicalCoverageResponse = {
  contract_version: '1',
  query_fingerprint: 'a'.repeat(64),
  as_of: '2026-08-21T12:00:00Z',
  totals: {
    state: 'available',
    applicable: 4,
    covered: 2,
    uncovered: 1,
    skipped: 1,
    value: 50,
    n: 4,
    reason: null,
  },
  coverage: [
    {
      obligation_type: 'ac',
      state: 'available',
      applicable: 4,
      covered: 2,
      uncovered: 1,
      skipped: 1,
      value: 50,
      n: 4,
      reason: null,
      rows: [{
        identity: {
          spec_id: 'spec-1',
          obligation_type: 'ac',
          obligation_id: 'ac-1',
          edition: 2,
          currentness: 'current',
        },
        state: 'skipped',
        applicable: true,
        covered: false,
        skip: {
          state: 'skipped',
          effective: true,
          reason_code: 'board_policy',
          authority_ref: 'board:board-1:coverage-skip',
          currentness: 'current',
        },
        authority_ref: 'spec:spec-1:ac:ac-1',
        reason: 'board_policy',
        evidence: [],
      }],
    },
    {
      obligation_type: 'api_contract',
      state: 'not_applicable',
      applicable: 0,
      covered: 0,
      uncovered: 0,
      skipped: 0,
      value: null,
      n: 0,
      reason: 'no_current_contracts',
      rows: [],
    },
  ],
  code_evidence: {
    state: 'available',
    reason: null,
    targets: [],
    resolutions: [],
    executions: [],
    overlaps: [],
    waivers: [],
  },
};

const callbacks = () => ({
  onRetry: vi.fn(),
  onExport: vi.fn().mockResolvedValue(undefined),
  onOpenSpec: vi.fn(),
});

describe('Canonical Coverage full-view contract', () => {
  it('keeps the dashboard summary compact and opens the full view with its current query', () => {
    const onOpenFullView = vi.fn();
    render(
      <CanonicalCoveragePanel
        {...callbacks()}
        data={coverage}
        loading={false}
        error={null}
        exporting={false}
        from={query.from}
        to={query.to}
        specTitles={{ 'spec-1': 'Checkout reliability' }}
        queryState={query}
        viewMode="summary"
        onOpenFullView={onOpenFullView}
      />,
    );

    const panel = screen.getByTestId('canonical-coverage-panel');
    const kpis = within(panel).getByLabelText('Canonical coverage KPIs');
    expect(within(kpis).getByText('Native coverage')).toBeInTheDocument();
    expect(within(kpis).getByText('Effective skip rate')).toBeInTheDocument();
    expect(within(kpis).getByText('25%')).toBeInTheDocument();
    expect(within(kpis).getByText('Board 25% · Spec 0%')).toBeInTheDocument();
    expect(within(panel).queryByText('Per-Spec Code Evidence Matrix')).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Open full view' }));
    expect(onOpenFullView).toHaveBeenCalledWith(query);
  });

  it('exposes a dedicated accessible page and emits controlled query changes', () => {
    const onQueryStateChange = vi.fn();
    const onBack = vi.fn();
    render(
      <CanonicalCoverageFullView
        {...callbacks()}
        boardId="board-1"
        data={coverage}
        loading={false}
        error={null}
        exporting={false}
        specTitles={{ 'spec-1': 'Checkout reliability' }}
        queryState={query}
        onQueryStateChange={onQueryStateChange}
        onBack={onBack}
      />,
    );

    const page = screen.getByTestId('canonical-coverage-full-view');
    expect(page).toHaveAccessibleName('Coverage and Traceability analytics');
    expect(within(page).getByRole('heading', { level: 1, name: 'Coverage & Traceability' })).toBeInTheDocument();
    expect(within(page).getByText('No Current Contracts')).toBeInTheDocument();

    fireEvent.change(within(page).getByLabelText('From date'), { target: { value: '2026-07-31' } });
    expect(onQueryStateChange).toHaveBeenCalledWith({ ...query, from: '2026-07-31' });
    fireEvent.change(within(page).getByLabelText('Search coverage'), { target: { value: 'evidence-17' } });
    expect(onQueryStateChange).toHaveBeenCalledWith({ ...query, search: 'evidence-17' });
    fireEvent.click(within(page).getByRole('button', { name: 'Back to Board Analytics' }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it('round-trips the reserved route and fails closed on an unknown outcome', () => {
    expect(canonicalCoverageFullViewPath('board/one', query)).toBe(
      '/analytics/boards/board%2Fone/canonical-coverage?from=2026-08-01&to=2026-08-21&lifecycle=active&outcome=incomplete&search=checkout',
    );

    expect(parseCanonicalCoverageQuery(
      '?from=2026-07-01&to=2026-07-31&lifecycle=current&outcome=fabricated&search=%20evidence-9%20',
      { from: query.from, to: query.to },
    )).toEqual({
      from: '2026-07-01',
      to: '2026-07-31',
      lifecycle: 'current',
      outcome: 'all',
      search: 'evidence-9',
    });
  });
});
