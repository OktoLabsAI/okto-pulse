import { useMemo } from 'react';
import { useApiClient } from '@/contexts/ApiContext';
import { archiveSections, type ArchiveOrigin, type ArchiveSection } from './historical-archives-api';

export type ContextTargetKind = 'spec' | 'card';
export interface HistoricalContextItem {
  binding_id: string;
  origin: ArchiveOrigin;
  archive_id: string;
  section: ArchiveSection;
  field: 'description' | 'objective' | 'expected_outcome' | null;
  record: Record<string, unknown>;
}
export interface HistoricalContextPage { items: HistoricalContextItem[]; next_offset: number | null }

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 255;
}
function fail(): never { throw new Error('Invalid historical context response'); }

export function parseHistoricalContext(
  value: unknown, boardId: string, kind: ContextTargetKind, targetId: string, offset: number, limit: number,
): HistoricalContextPage {
  if (!object(value) || value.format !== 'historical-context/v1' || value.board_id !== boardId
      || !object(value.target) || value.target.kind !== kind || value.target.id !== targetId
      || !Array.isArray(value.items) || value.items.length > limit) fail();
  const seen = new Set<string>();
  const items = value.items.map((item: unknown): HistoricalContextItem => {
    if (!object(item) || !identifier(item.binding_id) || seen.has(item.binding_id)
        || !identifier(item.archive_id) || !object(item.origin) || !identifier(item.origin.kind) || !identifier(item.origin.id)
        || !archiveSections.includes(item.section as ArchiveSection) || !object(item.record)) fail();
    if (item.section === 'content'
      ? !['description', 'objective', 'expected_outcome'].includes(item.field as string)
      : item.field !== null) fail();
    seen.add(item.binding_id);
    return { binding_id: item.binding_id, archive_id: item.archive_id,
      origin: { kind: item.origin.kind, id: item.origin.id }, section: item.section as ArchiveSection,
      field: item.field as HistoricalContextItem['field'], record: item.record };
  });
  if (value.next_offset !== null && (!Number.isInteger(value.next_offset)
      || value.next_offset !== offset + items.length || items.length === 0 || Number(value.next_offset) > 100_000)) fail();
  return { items, next_offset: value.next_offset as number | null };
}

export function useHistoricalContextApi() {
  const client = useApiClient();
  return useMemo(() => ({
    async read(boardId: string, kind: ContextTargetKind, targetId: string, offset: number, signal: AbortSignal): Promise<HistoricalContextPage> {
      const value = await client.fetchJson<unknown>(
        `/boards/${encodeURIComponent(boardId)}/historical-context/${kind}/${encodeURIComponent(targetId)}?offset=${offset}&limit=50`,
        { signal, cache: 'no-store' },
      );
      return parseHistoricalContext(value, boardId, kind, targetId, offset, 50);
    },
  }), [client]);
}
