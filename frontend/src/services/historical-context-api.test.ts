import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { parseHistoricalContext, useHistoricalContextApi } from './historical-context-api';

const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
const item = { binding_id: 'binding', origin: { kind: 'sprint', id: 'original' }, archive_id: 'archive',
  section: 'qa', field: null, record: { question: '  Original\r\n ', asked_by: 'author', answer: null } };
const envelope = () => ({ format: 'historical-context/v1', board_id: 'board', target: { kind: 'card', id: 'card' },
  items: [structuredClone(item)], next_offset: null });

describe('historical context transport', () => {
  it('preserves source text and drops transport extras', () => {
    const value = envelope();
    const result = parseHistoricalContext({ ...value, internal: 'private' }, 'board', 'card', 'card', 0, 50);
    expect(result).toEqual({ items: [item], next_offset: null });
  });

  it.each(['board', 'target-kind', 'target-id', 'format', 'origin', 'archive', 'section', 'field', 'record', 'duplicate', 'cursor', 'limit', 'missing-cursor'])('rejects malformed or wrong-scope %s', mutation => {
    const value: Record<string, unknown> & { target: Record<string, unknown>; items: Record<string, unknown>[] } = envelope();
    if (mutation === 'board') value.board_id = 'foreign';
    if (mutation === 'target-kind') value.target.kind = 'spec';
    if (mutation === 'target-id') value.target.id = 'foreign';
    if (mutation === 'format') value.format = 'unrecognized';
    if (mutation === 'origin') value.items[0].origin = { kind: 'sprint', id: '' };
    if (mutation === 'archive') value.items[0].archive_id = null;
    if (mutation === 'section') value.items[0].section = 'governance';
    if (mutation === 'field') value.items[0].field = 'policy';
    if (mutation === 'record') value.items[0].record = [];
    if (mutation === 'duplicate') value.items.push(value.items[0]);
    if (mutation === 'cursor') value.next_offset = 2;
    if (mutation === 'limit') value.items = Array(51).fill(item);
    if (mutation === 'missing-cursor') delete value.next_offset;
    expect(() => parseHistoricalContext(value, 'board', 'card', 'card', 0, 50)).toThrow('Invalid historical context');
  });

  it('requires a selected field for root content', () => {
    const value = { ...envelope(), items: [{ ...item, section: 'content', field: null }] };
    expect(() => parseHistoricalContext(value, 'board', 'card', 'card', 0, 50)).toThrow();
    expect(parseHistoricalContext({ ...value, items: [{ ...item, section: 'content', field: 'objective' }] },
      'board', 'card', 'card', 0, 50).items[0].field).toBe('objective');
  });

  it('uses encoded destination, bounded pagination, no-store and cancellation', async () => {
    const signal = new AbortController().signal;
    client.fetchJson.mockResolvedValue({ ...envelope(), board_id: 'b /?', target: { kind: 'spec', id: 's /?' } });
    const { result } = renderHook(() => useHistoricalContextApi());
    await result.current.read('b /?', 'spec', 's /?', 10, signal);
    expect(client.fetchJson).toHaveBeenCalledWith('/boards/b%20%2F%3F/historical-context/spec/s%20%2F%3F?offset=10&limit=50',
      { signal, cache: 'no-store' });
  });
});
