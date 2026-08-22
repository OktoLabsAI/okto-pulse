import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  parseEntityExportPreflight,
  useEntityExportApi,
} from './entity-export-api';

const FINGERPRINT = 'a'.repeat(64);

const apiClientMock = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => apiClientMock,
}));

function preflightPayload() {
  return {
    schema_version: 'entity-export/v1',
    formats: ['markdown', 'html'],
    scope: 'complete',
    identity: {
      entity_type: 'spec',
      entity_id: 'spec-1',
      title: 'Governed export',
      status: 'approved',
    },
    snapshot_fingerprint: FINGERPRINT,
    complete_for_actor: true,
    source_complete: true,
    sections: [{ section_key: 'base', state: 'included', total_count: 1 }],
  };
}

describe('entity export preflight contract', () => {
  beforeEach(() => {
    apiClientMock.fetchJson.mockReset();
    apiClientMock.fetch.mockReset();
  });

  it('normalizes Core manifest vocabulary and preserves completeness authority', () => {
    const result = parseEntityExportPreflight({
      schema_version: 'entity-export/v1',
      formats: ['markdown', 'html', 'html'],
      scope: 'complete',
      identity: {
        entity_type: 'spec',
        entity_id: 'spec-1',
        title: 'Governed export',
        status: 'approved',
        edition: 4,
        version: 19,
      },
      snapshot_fingerprint: FINGERPRINT,
      complete_for_actor: true,
      source_complete: false,
      manifest: [
        { section_key: 'requirements', state: 'included', total_count: 14 },
        {
          section_key: 'code_evidence',
          state: 'omitted',
          reason_code: 'permission_denied',
        },
      ],
    }, 'spec', 'spec-1', 'complete');

    expect(result.formats).toEqual(['markdown', 'html']);
    expect(result.identity).toMatchObject({
      entity_type: 'spec',
      entity_id: 'spec-1',
      edition: 4,
      version: 19,
    });
    expect(result.sections).toEqual([
      expect.objectContaining({
        section_key: 'requirements',
        label: 'Requirements',
        state: 'included',
        total_count: 14,
      }),
      expect.objectContaining({
        section_key: 'code_evidence',
        label: 'Code Evidence',
        state: 'omitted',
        reason_code: 'permission_denied',
      }),
    ]);
    expect(result.complete_for_actor).toBe(true);
    expect(result.source_complete).toBe(false);
  });

  it('accepts the nested Core bundle vocabulary exposed by a thin preflight route', () => {
    const result = parseEntityExportPreflight({
      contract_version: 'entity-export-bundle/v1',
      supported_formats: ['markdown', 'html'],
      history_scope: 'current',
      subject: {
        entity_type: 'card',
        entity_id: 'card-1',
        title: 'Regression card',
        status: 'started',
      },
      snapshot_fingerprint: FINGERPRINT,
      manifest: {
        contract_version: 'entity-export-manifest/v1',
        complete_for_actor: true,
        source_complete: true,
        entries: [
          { section_key: 'base', status: 'included', total_count: 1 },
          { section_key: 'history', status: 'not_applicable', reason_code: 'scope_current' },
        ],
      },
    }, 'card', 'card-1', 'current');

    expect(result.scope).toBe('current');
    expect(result.identity.status).toBe('started');
    expect(result.sections).toEqual([
      expect.objectContaining({ section_key: 'base', state: 'included' }),
      expect.objectContaining({ section_key: 'history', state: 'not_applicable' }),
    ]);
  });

  it.each([
    ['identity mismatch', {
      formats: ['markdown'], scope: 'complete', snapshot_fingerprint: FINGERPRINT,
      complete_for_actor: true, source_complete: true,
      identity: { entity_type: 'spec', entity_id: 'wrong', title: 'x' },
      manifest: [{ section_key: 'identity', state: 'included' }],
    }],
    ['unknown section state', {
      formats: ['markdown'], scope: 'complete', snapshot_fingerprint: FINGERPRINT,
      complete_for_actor: true, source_complete: true,
      identity: { entity_type: 'spec', entity_id: 'spec-1', title: 'x' },
      manifest: [{ section_key: 'identity', state: 'stale' }],
    }],
    ['missing fingerprint', {
      formats: ['markdown'], scope: 'complete',
      complete_for_actor: true, source_complete: true,
      identity: { entity_type: 'spec', entity_id: 'spec-1', title: 'x' },
      manifest: [{ section_key: 'identity', state: 'included' }],
    }],
    ['malformed fingerprint', {
      formats: ['markdown'], scope: 'complete', snapshot_fingerprint: 'sha256:not-canonical',
      complete_for_actor: true, source_complete: true,
      identity: { entity_type: 'spec', entity_id: 'spec-1', title: 'x' },
      manifest: [{ section_key: 'identity', state: 'included' }],
    }],
    ['missing completeness authority', {
      formats: ['markdown'], scope: 'complete', snapshot_fingerprint: FINGERPRINT,
      identity: { entity_type: 'spec', entity_id: 'spec-1', title: 'x' },
      manifest: [{ section_key: 'identity', state: 'included' }],
    }],
  ])('fails closed for %s', (_label, payload) => {
    expect(() => parseEntityExportPreflight(
      payload,
      'spec',
      'spec-1',
      'complete',
    )).toThrow();
  });

  it('posts the exact approved preflight route and request body', async () => {
    apiClientMock.fetchJson.mockResolvedValue(preflightPayload());
    const { result } = renderHook(() => useEntityExportApi());
    const signal = new AbortController().signal;

    const response = await result.current.preflight(
      'board/one',
      'spec',
      'spec-1',
      { scope: 'complete', sections: ['base', 'requirements'] },
      signal,
    );

    expect(response.snapshot_fingerprint).toBe(FINGERPRINT);
    expect(apiClientMock.fetchJson).toHaveBeenCalledWith(
      '/boards/board%2Fone/entity-exports/spec/spec-1/preflight',
      {
        method: 'POST',
        body: JSON.stringify({ scope: 'complete', sections: ['base', 'requirements'] }),
        signal,
      },
    );
  });

  it('downloads server-rendered attachment bytes through the approved POST contract', async () => {
    apiClientMock.fetch.mockResolvedValue(new Response('# Report', {
      status: 200,
      headers: {
        'Content-Type': 'text/markdown; charset=utf-8',
        'Content-Disposition': 'attachment; filename="spec_governed-export_complete.md"',
        'X-Export-Snapshot-Fingerprint': FINGERPRINT,
      },
    }));
    const { result } = renderHook(() => useEntityExportApi());
    const signal = new AbortController().signal;
    const request = {
      format: 'markdown' as const,
      scope: 'complete' as const,
      sections: ['base'],
      expected_snapshot_fingerprint: FINGERPRINT,
    };

    const attachment = await result.current.download(
      'board-1',
      'spec',
      'spec-1',
      request,
      'Governed export',
      signal,
    );

    expect(await attachment.blob.text()).toBe('# Report');
    expect(attachment.filename).toBe('spec_governed-export_complete.md');
    expect(attachment.snapshot_fingerprint).toBe(FINGERPRINT);
    expect(apiClientMock.fetch).toHaveBeenCalledWith(
      '/boards/board-1/entity-exports/spec/spec-1/download',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request),
        signal,
      }),
    );
  });
});
