/**
 * Tests for the cognitive-readiness API client (S3.3 / card 974f5146).
 * Confirms skip/clear surface the CANONICAL backend code + HTTP status so the
 * UI can render a 409 technical blocker without masking it.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';

import {
  clearCognitiveSkip,
  getReadinessItems,
  recordCognitiveSkip,
  ReadinessActionError,
} from './cognitive-readiness-api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('cognitive-readiness-api', () => {
  test('getReadinessItems monta os query params e retorna o corpo', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(200, { items: [], summary: {}, precedence: [] }));

    await getReadinessItems('board-1', { signal: 'dlq', search: 'foo', limit: 10 });

    const url = String(fetchSpy.mock.calls[0][0]);
    expect(url).toContain('/api/v1/kg/board-1/cognitive-readiness/items');
    expect(url).toContain('signal=dlq');
    expect(url).toContain('search=foo');
    expect(url).toContain('limit=10');
  });

  test('recordCognitiveSkip retorna o corpo em 200', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(200, { status: 'skipped', reason_code: 'trivial_fix' }),
    );
    const out = await recordCognitiveSkip('b', { sourceRef: 'card:x', reasonCode: 'trivial_fix' });
    expect(out.status).toBe('skipped');
  });

  test('recordCognitiveSkip propaga 409 com code canônico e status', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(409, {
        detail: {
          error: 'technical_debt_cannot_be_skipped',
          message: 'Canonical debt is OPEN.',
          status_code: 409,
        },
      }),
    );
    await expect(
      recordCognitiveSkip('b', { sourceRef: 'card:x', reasonCode: 'trivial_fix' }),
    ).rejects.toMatchObject({
      name: 'ReadinessActionError',
      code: 'technical_debt_cannot_be_skipped',
      status: 409,
    });
  });

  test('recordCognitiveSkip propaga 400 invalid_reason_code', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(400, {
        detail: { error: 'invalid_reason_code', message: 'nope', status_code: 400 },
      }),
    );
    const err = await recordCognitiveSkip('b', {
      sourceRef: 'card:x',
      reasonCode: 'technical_dlq',
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ReadinessActionError);
    expect(err.code).toBe('invalid_reason_code');
    expect(err.status).toBe(400);
  });

  test('clearCognitiveSkip POSTa source_ref e trata 409', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(200, { status: 'pending', reason_code: null }));
    const out = await clearCognitiveSkip('b', 'bug:x');
    expect(out.status).toBe('pending');
    const body = JSON.parse(String((fetchSpy.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({ source_ref: 'bug:x' });
  });
});
