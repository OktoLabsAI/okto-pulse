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

  it('never surfaces any secret category in the rendered DOM (ts_26974d8d)', async () => {
    const sentinels = {
      install_token: 'oat_INSTALLTOKENsentinel_aaaa1111',
      token_hash: 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3',
      signature: 'sig_SIGNATUREsentinel_cccc3333',
      nonce: 'NONCEsentinel0000aaaa1111bbbb2222',
      raw_install_id: 'install-RAWID-sentinel-dddd4444',
      payload: 'PAYLOADsentinel_eeee5555_body',
    };
    // a hostile DTO: redacted known fields, but sentinels planted in UNKNOWN/extra
    // nested fields/lists the panel must never dump.
    const hostile = {
      ...health({ status: 'degraded' }),
      install_id_redacted: 'iid_onlythis',
      debug_blob: sentinels.payload,
      nested: { token: sentinels.install_token, list: [{ sig: sentinels.signature }] },
      raw_state: {
        install_id: sentinels.raw_install_id,
        nonce: sentinels.nonce,
        token_hash: sentinels.token_hash,
      },
    } as unknown as PublishHealth;
    healthApi.getPublishHealth.mockResolvedValue(hostile);

    const { container } = render(<MetricsHealthPanel onClose={() => {}} />);
    await screen.findByTestId('health-status');
    const dom = container.textContent ?? '';

    for (const value of Object.values(sentinels)) {
      expect(dom).not.toContain(value);
    }
    // only the redacted id is shown; no raw payload dump element.
    expect(screen.getByTestId('health-install-id').textContent).toContain('iid_onlythis');
    expect(container.querySelector('textarea')).toBeNull();
    // sanity: the panel DID render real content, so the negative checks are meaningful.
    expect(dom.toLowerCase()).toContain('degraded');
  });
});

describe('MetricsHealthPanel — main states render without raw log (ts_c66a417e)', () => {
  beforeEach(() => {
    healthApi.getPublishHealth.mockReset();
  });

  // color token expected on the status badge per state (visual indicator).
  const stateCases = [
    { status: 'healthy', severity: 'none', reason_code: null, color: 'green' },
    { status: 'degraded', severity: 'warning', reason_code: 'USAGE_503', color: 'amber' },
    { status: 'failing', severity: 'critical', reason_code: 'INVALID_SIGNATURE', color: 'red' },
    { status: 'stale', severity: 'warning', reason_code: null, color: 'amber' },
    { status: 'disabled', severity: 'info', reason_code: null, color: 'gray' },
  ];

  it.each(stateCases)(
    'renders the $status state: status, severity, timestamps, next action — no raw log',
    async ({ status, severity, reason_code, color }) => {
      const dto = health({
        status,
        severity,
        reason_code,
        last_success_at: '2026-06-15T13:00:00Z',
        last_failure_at: '2026-06-15T12:55:00Z',
        next_retry_at: '2026-06-15T13:10:00Z',
        retry_count: 2,
      });
      healthApi.getPublishHealth.mockResolvedValue(dto);
      const { container } = render(<MetricsHealthPanel onClose={() => {}} />);

      // status label (verbatim) + a coherent COLORED visual indicator + severity
      const badge = await screen.findByTestId('health-status');
      expect(badge.textContent?.toLowerCase()).toContain(status);
      expect(badge.querySelector('span')).toBeTruthy();
      expect(badge.className).toContain(color); // visual indicator matches the state
      if (status !== 'healthy') {
        // NO-PROXY: a non-healthy state is never painted green / promoted to healthy.
        expect(badge.className).not.toContain('green');
        expect(badge.textContent?.toLowerCase()).not.toContain('healthy');
      }
      expect(screen.getByTestId('health-severity').textContent).toContain(severity);

      // relevant timestamps
      expect(screen.getByTestId('health-last-success').textContent).toContain('2026-06-15T13:00:00Z');
      expect(screen.getByTestId('health-last-failure').textContent).toContain('2026-06-15T12:55:00Z');
      expect(screen.getByTestId('health-next-retry').textContent).toContain('2026-06-15T13:10:00Z');
      expect(screen.getByTestId('health-retry-count').textContent).toBe('2');

      // an actionable next action / message
      expect((screen.getByTestId('health-message').textContent ?? '').trim().length).toBeGreaterThan(0);

      // NO raw log / serialized dump: no textarea, <pre>, <code>, or JSON of the DTO.
      expect(container.querySelector('textarea')).toBeNull();
      expect(container.querySelector('pre')).toBeNull();
      expect(container.querySelector('code')).toBeNull();
      const dom = container.textContent ?? '';
      expect(dom).not.toContain('"status":');
      expect(dom).not.toContain('redaction_applied');
      expect(dom).not.toContain('"sources"');
      expect(dom).not.toContain('stale_threshold_seconds');
    },
  );

  it('does not render hostile internal debug/log/payload fields (anti-tautology)', async () => {
    const hostile = {
      ...health({ status: 'degraded' }),
      debug_log: 'RAWLOGsentinel_should_not_render',
      _internal_payload: 'PAYLOADsentinel_xyz',
      raw_dump: { token: 'tok_internal_sentinel', signature: 'sig_internal_sentinel' },
    } as unknown as PublishHealth;
    healthApi.getPublishHealth.mockResolvedValue(hostile);
    const { container } = render(<MetricsHealthPanel onClose={() => {}} />);
    await screen.findByTestId('health-status');
    const dom = container.textContent ?? '';

    expect(dom).not.toContain('RAWLOGsentinel_should_not_render');
    expect(dom).not.toContain('PAYLOADsentinel_xyz');
    expect(dom).not.toContain('tok_internal_sentinel');
    expect(dom).not.toContain('sig_internal_sentinel');
    expect(container.querySelector('textarea')).toBeNull();
    expect(container.querySelector('pre')).toBeNull();
    expect(container.querySelector('code')).toBeNull();
    // sanity: the panel did render real content, painted for degraded (amber, not green).
    expect(dom.toLowerCase()).toContain('degraded');
    expect((screen.getByTestId('health-status') as HTMLElement).className).not.toContain('green');
  });
});

describe('MetricsHealthPanel — minimum health state coverage (ts_155de001)', () => {
  beforeEach(() => {
    healthApi.getPublishHealth.mockReset();
  });

  const minStates = [
    { status: 'healthy', severity: 'none', color: 'green' },
    { status: 'degraded', severity: 'warning', color: 'amber' },
    { status: 'failing', severity: 'critical', color: 'red' },
    { status: 'stale', severity: 'warning', color: 'amber' },
    { status: 'disabled', severity: 'info', color: 'gray' },
    { status: 'unavailable', severity: 'warning', color: 'gray' },
  ];

  it('covers exactly the six minimum states (teeth: dropping one breaks coverage)', () => {
    expect(new Set(minStates.map((s) => s.status)).size).toBe(6);
    expect(minStates.map((s) => s.status).slice().sort()).toEqual([
      'degraded',
      'disabled',
      'failing',
      'healthy',
      'stale',
      'unavailable',
    ]);
  });

  it.each(minStates)(
    'renders the $status state with severity + message; non-healthy never green',
    async ({ status, severity, color }) => {
      const dto = health({
        status,
        severity,
        message: `Actionable guidance for ${status}.`,
        last_success_at: '2026-06-15T13:00:00Z',
      });
      healthApi.getPublishHealth.mockResolvedValue(dto);
      const { container } = render(<MetricsHealthPanel onClose={() => {}} />);

      const badge = await screen.findByTestId('health-status');
      expect(badge.textContent?.toLowerCase()).toContain(status);
      expect(badge.className).toContain(color); // visual indicator matches the state
      expect(screen.getByTestId('health-severity').textContent).toContain(severity);
      expect(screen.getByTestId('health-message').textContent).toContain(`Actionable guidance for ${status}.`);

      if (status === 'healthy') {
        expect(badge.className).toContain('green');
      } else {
        // a non-healthy state is NEVER rendered as a success/green visual.
        expect(badge.className).not.toContain('green');
      }
      expect(container.querySelector('textarea')).toBeNull();
      expect(container.querySelector('pre')).toBeNull();
    },
  );
});
