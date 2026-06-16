import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MetricsHealthPanel } from './MetricsHealthPanel';
import type { PublishHealth } from '@/services/metrics-health-api';

const healthApi = vi.hoisted(() => ({
  getPublishHealth: vi.fn(),
}));

vi.mock('@/services/metrics-health-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/metrics-health-api')>();
  return { ...actual, getPublishHealth: healthApi.getPublishHealth };
});

function health(overrides: Partial<PublishHealth> = {}): PublishHealth {
  return {
    status: 'degraded',
    source: 'combined',
    severity: 'warning',
    reason_code: null,
    reason_category: 'source_gap',
    http_status: null,
    last_success_at: '2026-06-15T13:00:00Z',
    last_failure_at: null,
    next_retry_at: null,
    retry_count: 0,
    freshness: { last_success_at: '2026-06-15T13:00:00Z', age_seconds: 60, is_stale: false, stale_threshold_seconds: 21600 },
    install_id_redacted: 'iid_abc123def456',
    message: 'This health source has no adapter in this build; reported as an observability gap, not healthy.',
    sources: [
      { name: 'local', status: 'healthy', severity: 'none', reason_category: 'none', message: 'Publishing is healthy.', available: true, last_success_at: '2026-06-15T13:00:00Z' },
      { name: 'install_lifecycle', status: 'healthy', severity: 'none', reason_category: 'none', message: 'ok', available: true, last_success_at: null },
      { name: 'aws_ingest', status: 'degraded', severity: 'warning', reason_category: 'source_gap', message: 'no adapter in this build', available: false, last_success_at: null },
      { name: 'report_athena', status: 'degraded', severity: 'warning', reason_category: 'source_gap', message: 'no adapter in this build', available: false, last_success_at: null },
    ],
    redaction_applied: true,
    ...overrides,
  };
}

describe('MetricsHealthPanel', () => {
  beforeEach(() => {
    healthApi.getPublishHealth.mockReset();
  });

  it('renders the degraded gap state as degraded — never upgraded to healthy', async () => {
    healthApi.getPublishHealth.mockResolvedValue(health());
    render(<MetricsHealthPanel onClose={() => {}} />);

    const badge = await screen.findByTestId('health-status');
    expect(badge.textContent?.toLowerCase()).toContain('degraded');
    expect(badge.textContent?.toLowerCase()).not.toContain('healthy');
    // the overall is degraded even though local + lifecycle are healthy (no proxy upgrade)
    const aws = screen.getByTestId('health-source-aws_ingest');
    expect(within(aws).getByText(/aws_ingest/)).toBeTruthy();
    expect(aws.textContent).toContain('degraded');
  });

  it.each([
    ['healthy', health({ status: 'healthy', message: 'Publishing is healthy; the last publish succeeded.' })],
    ['failing', health({ status: 'failing', reason_code: 'INVALID_SIGNATURE' })],
    ['stale', health({ status: 'stale' })],
    ['disabled', health({ status: 'disabled' })],
    ['unavailable', health({ status: 'unavailable' })],
    ['recovering', health({ status: 'recovering' })],
  ])('renders the %s status verbatim from the DTO', async (status, dto) => {
    healthApi.getPublishHealth.mockResolvedValue(dto);
    render(<MetricsHealthPanel onClose={() => {}} />);
    const badge = await screen.findByTestId('health-status');
    expect(badge.textContent?.toLowerCase()).toContain(status);
  });

  it('renders the redacted install id, never a raw secret or payload dump', async () => {
    // a deliberately hostile DTO with an extra unknown field carrying a secret-looking
    // value; the panel must ignore extras and never dump them.
    const hostile = {
      ...health({ status: 'failing' }),
      install_id_redacted: 'iid_safehash',
      debug_extra: 'oat_SHOULD_NOT_RENDER_secret',
    } as unknown as PublishHealth;
    healthApi.getPublishHealth.mockResolvedValue(hostile);
    const { container } = render(<MetricsHealthPanel onClose={() => {}} />);

    await screen.findByTestId('health-status');
    expect(screen.getByTestId('health-install-id').textContent).toContain('iid_safehash');
    // the unknown extra field is never rendered (no raw dump / textarea)
    expect(container.textContent).not.toContain('oat_SHOULD_NOT_RENDER_secret');
    expect(container.querySelector('textarea')).toBeNull();
  });

  it('is robust to null / missing fields and empty sources without rendering blank', async () => {
    const sparse = {
      status: 'unavailable',
      last_success_at: null,
      reason_code: null,
      sources: [],
      message: 'No publish outcome has been recorded yet.',
    } as PublishHealth;
    healthApi.getPublishHealth.mockResolvedValue(sparse);
    render(<MetricsHealthPanel onClose={() => {}} />);

    expect((await screen.findByTestId('health-status')).textContent?.toLowerCase()).toContain('unavailable');
    expect(screen.getByTestId('health-no-sources')).toBeTruthy();
    expect(screen.getByTestId('health-last-success').textContent).toBe('—');
    expect(screen.getByTestId('health-retry-count').textContent).toBe('0');
  });

  it('renders the structured HEALTH_SOURCE_UNAVAILABLE response as unavailable', async () => {
    healthApi.getPublishHealth.mockResolvedValue({
      error: 'HEALTH_SOURCE_UNAVAILABLE',
      source: 'local',
      message: 'No publish-health source could be read.',
      redaction_applied: true,
    } as PublishHealth);
    render(<MetricsHealthPanel onClose={() => {}} />);

    expect((await screen.findByTestId('health-status')).textContent?.toLowerCase()).toContain('unavailable');
    expect(screen.getByTestId('health-source-unavailable')).toBeTruthy();
  });

  it('shows a transport error without crashing', async () => {
    healthApi.getPublishHealth.mockRejectedValue(new Error('Failed to load publish health (HTTP 500)'));
    render(<MetricsHealthPanel onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('health-error')).toBeTruthy());
  });
});
