import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { initAuthFetch } from '@/lib/authFetch';
import { getNodeSource } from '@/services/kg-api';
import * as kgApi from '@/services/kg-api';

describe('KG API authenticated transport', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    initAuthFetch(async () => 'token-for-test', 'https://pulse.example/api/v1');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not export the retired manual undo client and keeps audit reads', () => {
    expect('undoSession' in kgApi).toBe(false);
    expect(typeof kgApi.listAudit).toBe('function');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uses the configured authenticated client for node source resolution', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      status: 'resolved', source_artifact_ref: 'spec:one', target: null,
    }), { status: 200 }));

    await getNodeSource('board/one', 'node?one');

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      'https://pulse.example/api/v1/kg/boards/board%2Fone/nodes/node%3Fone/source',
    );
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer token-for-test');
  });
});
