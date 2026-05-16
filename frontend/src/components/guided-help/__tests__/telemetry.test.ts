/**
 * Spec f64d1aaf — ts_63744afc: Telemetry adapter respects metrics modes
 * and only emits the categorical, allowlisted payload.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS,
  createConsentAwareTelemetryAdapter,
  sanitizeGuidedHelpEvent,
  type SanitizedGuidedHelpTelemetryPayload,
} from '../telemetry';

const SAFE_EVENT = {
  action: 'viewed' as const,
  tour_surface: 'board' as const,
  step_kind: 'navigation' as const,
  status: 'success' as const,
  duration_ms: 1234,
};

describe('guided help telemetry adapter', () => {
  it('does not emit anything when mode=disabled', async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const onEmit = vi.fn();
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => 'disabled',
      send,
      onEmit,
    });

    await adapter.emit(SAFE_EVENT);

    expect(send).not.toHaveBeenCalled();
    expect(onEmit).not.toHaveBeenCalled();
  });

  it('emits a categorical payload via send when mode=local_only', async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const onEmit = vi.fn();
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => 'local_only',
      send,
      onEmit,
    });

    await adapter.emit(SAFE_EVENT);

    expect(send).toHaveBeenCalledTimes(1);
    const payload = send.mock.calls[0][0] as SanitizedGuidedHelpTelemetryPayload;
    expect(payload.event_type).toBe('guided_help');
    expect(payload.schema_version).toBe('1.0');
    expect(payload.payload).toEqual(SAFE_EVENT);
    expect(onEmit).toHaveBeenCalledWith(payload, 'local_only');
  });

  it('emits the same categorical payload when mode=anonymous_beacon', async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => 'anonymous_beacon',
      send,
    });

    await adapter.emit(SAFE_EVENT);

    expect(send).toHaveBeenCalledTimes(1);
    const payload = send.mock.calls[0][0] as SanitizedGuidedHelpTelemetryPayload;
    expect(payload.payload).toEqual(SAFE_EVENT);
  });

  it('strips forbidden fields like board_id, spec_id, selector, url, token', async () => {
    const dirtyEvent = {
      ...SAFE_EVENT,
      // The forbidden fields below MUST be removed before sending.
      board_id: 'b-123',
      spec_id: 's-abc',
      title: 'should not leak',
      selector: '#secret-button',
      url: 'https://example.com/private',
      content: 'raw user content',
      token: 'tok_xxx',
    } as Parameters<ReturnType<typeof createConsentAwareTelemetryAdapter>['emit']>[0];

    const send = vi.fn().mockResolvedValue(undefined);
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => 'local_only',
      send,
    });

    await adapter.emit(dirtyEvent);

    expect(send).toHaveBeenCalledTimes(1);
    const payload = send.mock.calls[0][0] as SanitizedGuidedHelpTelemetryPayload;
    const keys = Object.keys(payload.payload);
    for (const key of keys) {
      expect(GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS).toContain(key as never);
    }
    expect(payload.payload).not.toHaveProperty('board_id');
    expect(payload.payload).not.toHaveProperty('spec_id');
    expect(payload.payload).not.toHaveProperty('title');
    expect(payload.payload).not.toHaveProperty('selector');
    expect(payload.payload).not.toHaveProperty('url');
    expect(payload.payload).not.toHaveProperty('content');
    expect(payload.payload).not.toHaveProperty('token');
  });

  it('treats mode-resolution failures as disabled (UI never blocks)', async () => {
    const send = vi.fn();
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => {
        throw new Error('cannot read mode');
      },
      send,
    });

    await adapter.emit(SAFE_EVENT);

    expect(send).not.toHaveBeenCalled();
  });

  it('swallows network errors so callers never block', async () => {
    const send = vi.fn().mockRejectedValue(new Error('network down'));
    const adapter = createConsentAwareTelemetryAdapter({
      getMode: () => 'local_only',
      send,
    });

    await expect(adapter.emit(SAFE_EVENT)).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('sanitizeGuidedHelpEvent rejects unknown keys', () => {
    const result = sanitizeGuidedHelpEvent({
      ...SAFE_EVENT,
      // Cast to bypass type check — the runtime guard is what we care about.
      board_id: 'leak',
    } as Parameters<typeof sanitizeGuidedHelpEvent>[0] & { board_id: string });

    expect(Object.keys(result.payload).sort()).toEqual(
      [...GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS].sort(),
    );
    expect(result.payload).not.toHaveProperty('board_id');
  });
});
