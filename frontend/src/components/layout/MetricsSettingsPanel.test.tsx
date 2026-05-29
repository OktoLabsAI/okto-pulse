import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MetricsSettingsPanel } from './MetricsSettingsPanel';
import type { MetricsSummary } from '@/services/metrics-api';

const metricsApi = vi.hoisted(() => ({
  exportLocalMetrics: vi.fn(),
  getMetricsSummary: vi.fn(),
  markMetricsMigrationNoticeSeen: vi.fn(),
  purgeLocalMetrics: vi.fn(),
  updateMetricsMode: vi.fn(),
}));

const toastMock = vi.hoisted(() =>
  Object.assign(vi.fn(), {
    error: vi.fn(),
    success: vi.fn(),
  }),
);

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
    mode: 'disabled',
    ui_mode: 'off',
    enabled: false,
    normalized_from: null,
    migration_notice: null,
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
    metricsApi.markMetricsMigrationNoticeSeen.mockReset();
    metricsApi.purgeLocalMetrics.mockReset();
    metricsApi.updateMetricsMode.mockReset();
    toastMock.mockReset();
    toastMock.error.mockReset();
    toastMock.success.mockReset();
    metricsApi.markMetricsMigrationNoticeSeen.mockResolvedValue({
      notice_key: 'local_only_to_disabled',
      pending: false,
      seen_at: '2026-05-28T12:00:00Z',
      idempotent: false,
    });
    metricsApi.updateMetricsMode.mockResolvedValue({
      mode: 'disabled',
      ui_mode: 'off',
      enabled: false,
      normalized_from: null,
      migration_notice: null,
      changed_at: '2026-05-14T00:00:00Z',
      schema_version: '1.1.0',
      acknowledged_items: [],
      next_opt_in_prompt_after: null,
    });
  });

  it('renders only the On/Off toggle and saves the full anonymous metrics package', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(summary());

    render(<MetricsSettingsPanel onClose={() => {}} />);

    await screen.findByTestId('metrics-save');
    expect(screen.queryByTestId('metrics-mode-local_only')).not.toBeInTheDocument();
    expect(screen.queryByTestId('metrics-mode-anonymous_beacon')).not.toBeInTheDocument();
    expect(screen.queryByTestId('metrics-mode-disabled')).not.toBeInTheDocument();
    expect(screen.getByTestId('metrics-on-off-toggle')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText('Anonymous metrics included')).toBeInTheDocument();
    expect(screen.getByText(/Telemetry schema 1.1.0 reviewed/)).toBeInTheDocument();
    expect(screen.queryByText('Confirm all')).not.toBeInTheDocument();
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);

    fireEvent.click(screen.getByTestId('metrics-on-off-toggle'));
    expect(screen.getByTestId('metrics-on-off-toggle')).toHaveAttribute('aria-checked', 'true');

    expect(metricsApi.updateMetricsMode).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('metrics-save'));

    await waitFor(() =>
      expect(metricsApi.updateMetricsMode).toHaveBeenCalledWith('anonymous_beacon', ACK_IDS),
    );
    expect(toastMock.error).not.toHaveBeenCalledWith('Check every opt-in item before turning metrics on');
  });

  it('saves Off without granular metric selections', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(
      summary({
        mode: 'anonymous_beacon',
        ui_mode: 'on',
        enabled: true,
        consent: { acknowledged_items: ACK_IDS },
      }),
    );

    render(<MetricsSettingsPanel onClose={() => {}} />);

    await screen.findByTestId('metrics-save');
    expect(screen.getByTestId('metrics-on-off-toggle')).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);

    fireEvent.click(screen.getByTestId('metrics-on-off-toggle'));
    expect(screen.getByTestId('metrics-on-off-toggle')).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(screen.getByTestId('metrics-save'));

    await waitFor(() =>
      expect(metricsApi.updateMetricsMode).toHaveBeenCalledWith('disabled', []),
    );
  });

  it('does not expose individual metric counts, event types, paths, or local export actions', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(
      summary({
        metrics_dir: 'D:\\metrics\\private',
        product_aggregate_families: ['product_feature_usage'],
        summary: {
          event_count: 43537,
          by_event_type: { http: 43537 },
          by_day: { '2026-05-28': 43537 },
          files_count: 16,
        },
      }),
    );

    render(<MetricsSettingsPanel onClose={() => {}} />);

    expect(await screen.findByTestId('metrics-scope')).toHaveTextContent(
      'All eligible anonymous aggregate metrics are included',
    );
    expect(screen.queryByText('Events')).not.toBeInTheDocument();
    expect(screen.queryByText('Files')).not.toBeInTheDocument();
    expect(screen.queryByText('Days')).not.toBeInTheDocument();
    expect(screen.queryByText('Event types')).not.toBeInTheDocument();
    expect(screen.queryByText('http')).not.toBeInTheDocument();
    expect(screen.queryByText('43537')).not.toBeInTheDocument();
    expect(screen.queryByText('product_feature_usage')).not.toBeInTheDocument();
    expect(screen.queryByText('Path')).not.toBeInTheDocument();
    expect(screen.queryByText('D:\\metrics\\private')).not.toBeInTheDocument();
    expect(screen.queryByText('Export')).not.toBeInTheDocument();
    expect(screen.queryByText('Purge')).not.toBeInTheDocument();
  });

  it('shows the migration toast once and marks it as seen', async () => {
    metricsApi.getMetricsSummary.mockResolvedValue(
      summary({
        normalized_from: 'local_only',
        migration_notice: {
          type: 'local_only_to_disabled',
          reason: 'legacy_local_only_disabled',
          from_mode: 'local_only',
          to_mode: 'disabled',
          pending: true,
          seen_at: null,
          message: 'Previous Local metrics mode was migrated to Off.',
        },
      }),
    );

    render(<MetricsSettingsPanel onClose={() => {}} />);

    await screen.findByTestId('metrics-save');

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith('Metrics were turned off'));
    expect(metricsApi.markMetricsMigrationNoticeSeen).toHaveBeenCalledTimes(1);
    expect(metricsApi.markMetricsMigrationNoticeSeen).toHaveBeenCalledWith('local_only_to_disabled');
  });
});
