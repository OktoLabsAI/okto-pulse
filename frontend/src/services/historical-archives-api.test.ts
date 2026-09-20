import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { parseArchiveList, parseArchivePage, useHistoricalArchivesApi, type ArchiveItem } from './historical-archives-api';

const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
const item: ArchiveItem = { origin: { kind: 'old/type', id: 'origin ?' }, archive_id: 'archive', sections: ['content', 'history'] };
const listing = { format: 'historical-archive-discovery/v1', board_id: 'board', items: [item], next_offset: null };
const page = { format: 'historical-archive-section/v1', board_id: 'board', origin: item.origin,
  archive_id: item.archive_id, section: 'history', records: [{ summary: '  original\r\n ' }], next_offset: null };

describe('historical archive response boundaries', () => {
  it.each([
    { board_id: 'foreign' }, { format: 'future' }, { next_offset: 9 },
    { items: [item, item] }, { items: [{ ...item, sections: ['history'] }] },
    { items: [item, { ...item, origin: { id: item.origin.id, kind: item.origin.kind } }] },
    { items: [{ ...item, sections: ['content', 'private'] }] },
  ])('rejects mismatched or malformed discovery: %j', changes => {
    expect(() => parseArchiveList({ ...listing, ...changes }, 'board', 0, 50)).toThrow();
  });
  it.each([
    { board_id: 'foreign' }, { origin: { kind: 'old/type', id: 'foreign' } }, { archive_id: 'foreign' },
    { section: 'qa' }, { records: [null] }, { next_offset: 0 }, { records: [], next_offset: 1 },
  ])('rejects mismatched or malformed section: %j', changes => {
    expect(() => parseArchivePage({ ...page, ...changes }, 'board', item, 'history', 0, 50)).toThrow();
  });
  it('preserves original text and validates a monotonic next page', () => {
    expect(parseArchivePage({ ...page, next_offset: 1 }, 'board', item, 'history', 0, 50))
      .toEqual({ records: page.records, next_offset: 1 });
  });
  it('uses authenticated scoped GETs with encoded IDs, cancellation and no cache', async () => {
    const signal = new AbortController().signal;
    client.fetchJson.mockResolvedValueOnce(listing).mockResolvedValueOnce(page);
    const { result } = renderHook(useHistoricalArchivesApi);
    await result.current.list('board', 0, signal);
    await result.current.read('board', item, 'history', 0, signal);
    expect(client.fetchJson).toHaveBeenNthCalledWith(1, '/boards/board/historical-archives?offset=0&limit=50', { signal, cache: 'no-store' });
    expect(client.fetchJson).toHaveBeenNthCalledWith(2, '/boards/board/historical-archives/old%2Ftype/origin%20%3F/history?offset=0&limit=50', { signal, cache: 'no-store' });
  });
});
