import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { KGHealth } from '@/services/kg-health-api';
import { KGHealthOverview } from '../KGHealthOverview';

afterEach(cleanup);

// Only the presentation fields are needed; the parent owns data acquisition.
const snapshot = {
  overall_state: 'healthy', discovery_state: 'healthy', metric_status: 'available',
  total_nodes: 2468, queue_depth: 3, current_kg_generation_id: null,
} as KGHealth;

describe('KG Health overview', () => {
  it('does not recommend rebuilding a healthy graph without a rebuild generation', () => {
    render(<KGHealthOverview health={snapshot} stale={false} />);
    expect(screen.getByRole('heading', { name: 'Operational' })).toBeInTheDocument();
    expect(screen.getByText(/No recovery is indicated/)).toBeInTheDocument();
    expect(screen.getByText((2468).toLocaleString())).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View diagnostics' })).toHaveAttribute('href', '#kg-health-diagnostics');
    expect(screen.queryByRole('link', { name: 'Review recovery' })).not.toBeInTheDocument();
  });

  it.each(['unavailable', undefined])('does not present unavailable metric counts as measured: %s', (metricStatus) => {
    render(<KGHealthOverview health={{ ...snapshot, total_nodes: 0, metric_status: metricStatus }} stale={false} />);
    expect(screen.getByText('Not measured')).toBeInTheDocument();
    expect(screen.queryByText('0')).not.toBeInTheDocument();
  });

  it('preserves a measured zero', () => {
    render(<KGHealthOverview health={{ ...snapshot, total_nodes: 0 }} stale={false} />);
    expect(screen.getByText('0')).toBeInTheDocument();
    expect(screen.queryByText('Not measured')).not.toBeInTheDocument();
  });

  it.each(['at_risk', 'backpressure'])('directs %s to diagnosis, not automatic recovery', (overallState) => {
    render(<KGHealthOverview health={{ ...snapshot, overall_state: overallState }} stale={false} />);
    expect(screen.getByRole('heading', { name: 'Needs attention' })).toBeInTheDocument();
    expect(screen.getByText(/warning does not by itself/)).toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', '#kg-health-diagnostics');
  });

  it.each(['recovery_needed', 'quarantined', 'corrupted', 'failed'])('reports the component limitation for %s without a recovery workflow', (overallState) => {
    render(<KGHealthOverview health={{ ...snapshot, overall_state: overallState }} stale={false} />);
    expect(screen.getByRole('heading', { name: 'Component unavailable' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View diagnostics' })).toHaveAttribute('href', '#kg-health-diagnostics');
    expect(screen.getByText(/this view cannot repair the graph/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rebuild|recover/i })).not.toBeInTheDocument();
  });

  it('does not infer health or discovery status from missing or new backend states', () => {
    render(<KGHealthOverview health={{ ...snapshot, overall_state: 'new_state', discovery_state: undefined }} stale={false} />);
    expect(screen.getByRole('heading', { name: 'Status unknown' })).toBeInTheDocument();
    expect(screen.getByText('Not reported')).toBeInTheDocument();
  });

  it.each(['healthy', 'recovery_needed'])('does not offer a fresh verdict from a stale %s snapshot', (state) => {
    render(<KGHealthOverview health={{ ...snapshot, overall_state: state }} stale />);
    expect(screen.getByRole('heading', { name: 'Snapshot needs a refresh' })).toBeInTheDocument();
    expect(screen.getByText(/previous observations/)).toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', '#kg-health-diagnostics');
  });

  it('offers keyboard help in a portal without adding it to the panel layout', () => {
    render(<KGHealthOverview health={snapshot} stale={false} />);
    const help = screen.getByRole('button', { name: 'Help: About indexed nodes' });
    fireEvent.focus(help);
    const tooltip = screen.getByRole('tooltip');
    expect(tooltip).toHaveTextContent('An unavailable reading is not zero nodes');
    expect(tooltip).toHaveClass('fixed');
    expect(within(screen.getByTestId('kg-health-overview')).queryByRole('tooltip')).not.toBeInTheDocument();
    expect(help).toHaveAttribute('aria-describedby', tooltip.id);
    fireEvent.keyDown(help, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});
