import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { initAuthFetch } from '@/lib/authFetch';
import {
  redriveAllDeadLetterRows,
  redriveDeadLetterRows,
} from '@/services/dead-letter-api';

describe('dead-letter API authenticated transport', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    initAuthFetch(async () => 'token-for-test', 'https://pulse.example/api/v1');
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({
      success: true, blocked: false, mutated: true, scope: 'generic',
      requested: 1, selected: 1, requeued_count: 1, already_queued_count: 0,
    }), { status: 200 }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('authenticates selected-row and all-row redrive requests through the configured base URL', async () => {
    await redriveDeadLetterRows('board-1', ['dead-1']);
    await redriveAllDeadLetterRows('board-1');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchMock.mock.calls as Array<[string, RequestInit]>) {
      expect(url).toBe('https://pulse.example/api/v1/kg/queue/dead-letter/redrive');
      expect(init.method).toBe('POST');
      expect(new Headers(init.headers).get('Authorization')).toBe('Bearer token-for-test');
    }
  });
});
