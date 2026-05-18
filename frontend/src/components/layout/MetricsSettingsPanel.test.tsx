import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MetricsSettingsPanel } from './MetricsSettingsPanel';
import type { MetricsSummary } from '@/services/metrics-api';

const metricsApi = vi.hoisted(() => ({
  exportLocalMetrics: vi.fn(),
  getMetricsSummary: vi.fn(),
  purgeLocalMetrics: vi.fn(),
  updateMetricsMode: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/metrics-api', () => ({
  CURRENT_METRICS_SCHEMA_VERSION: '1.1.0',
  ...metricsApi,
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

const ACK_IDS = [
  'schema',
  'privacy_policy',
  'hourly_aggregates',
  'product_aggregates',
  'no_pii',
  'local_control',
];

type MetricsSummaryOverrides = Partial<Omit<MetricsSummary, 'consent'>> & {
  consent?: Partial<MetricsSummary['consent']>;
};

function summary(overrides: MetricsSummaryOverrides = {}): MetricsSummary {
  const base: MetricsSummary = {
    mode: 'local_only',
    source: 'persisted_consent',
    metrics_dir: 'D:\\metrics',
    retention_days: 30,
    schema_version: '1.1.0',
    product_aggregate_families: [],
    summary: {
      event_count: 0,
      by_event_type: {},
      by_day: {},
      files_count: 0,
    },
    beacon_status: {
      enabled: false,
      last_handshake_at: null,
      last_send_at: null,
      circuit_open_until: null,
      schema_status: 'current',
    },
    next_opt_in_prompt_after: null,
    consent: {
      source: 'settings_ui',
      changed_at: '2026-05-14T00:00:00Z',
      policy_version: '2026-05-11',
      schema_version: '1.1.0',
      acknowledged_items: [],
    },
    resolved_precedence: [],
  };
  return { ...base, ...overrides, consent: { ...base.consent, ...overrides.consent } };
}

describe('MetricsSettingsPanel', () => {
  beforeEach(() => {
    metricsApi.exportLocalMetrics.mockReset();
    metricsApi.getMetricsSummary.mockReset();
    metricsApi.purgeLocalMetrics.mockReset();
    metricsApi.updateMetricsMode.mockReset();
    toastMock.error.mockReset();
    toastMock.success.mockReset();
    metricsApi.updateMetricsMode.mockResolvedValue({
      mode: 'local_only',
      changed_at: '2026-05-14T00:00:00Z',
      schema_version: '1.1.0',
      acknowledged_items: [],
      next_opt_in_prompt_after: null,
    });
  });

  it('loads persisted checklist marks and saves changes explicitly', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(
      summary({ consent: { acknowledged_items: ['schema'] } }),
    );

    render(<MetricsSettingsPanel onClose={() => {}} />);

    expect(await screen.findByLabelText(/Telemetry schema/)).toBeChecked();
    expect(screen.getByTestId('metrics-save')).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/Privacy terms reviewed/));

    expect(screen.getByTestId('metrics-save')).toBeEnabled();
    expect(metricsApi.updateMetricsMode).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('metrics-save'));

    await waitFor(() =>
      expect(metricsApi.updateMetricsMode).toHaveBeenCalledWith('local_only', ['schema', 'privacy_policy']),
    );
  });

  it('does not enable beacon until the checklist is complete and saved', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(summary());

    render(<MetricsSettingsPanel onClose={() => {}} />);

    await screen.findByTestId('metrics-save');
    fireEvent.click(screen.getByTestId('metrics-mode-anonymous_beacon'));

    expect(metricsApi.updateMetricsMode).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('metrics-save'));

    expect(metricsApi.updateMetricsMode).not.toHaveBeenCalled();
    expect(toastMock.error).toHaveBeenCalledWith('Check every opt-in item before enabling Beacon');

    fireEvent.click(screen.getByText('Confirm all'));
    fireEvent.click(screen.getByTestId('metrics-save'));

    await waitFor(() =>
      expect(metricsApi.updateMetricsMode).toHaveBeenCalledWith('anonymous_beacon', ACK_IDS),
    );
  });
});
