import { useMemo } from 'react';
import { useApiClient } from '@/contexts/ApiContext';

export const archiveSections = ['content', 'qa', 'evaluations', 'history'] as const;
export type ArchiveSection = typeof archiveSections[number];
export interface ArchiveOrigin { kind: string; id: string }
export interface ArchiveItem {
  origin: ArchiveOrigin;
  archive_id: string;
  sections: ArchiveSection[];
}
export interface ArchiveList { items: ArchiveItem[]; next_offset: number | null }
export interface ArchivePage { records: Record<string, unknown>[]; next_offset: number | null }

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 255;
}
function fail(): never { throw new Error('Invalid historical archive response'); }
function nextOffset(value: unknown, offset: number, count: number): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || value !== offset + count || count === 0 || Number(value) > 100_000) fail();
  return value as number;
}
function origin(value: unknown): value is ArchiveOrigin {
  return record(value) && identifier(value.kind) && identifier(value.id);
}

export function parseArchiveList(value: unknown, boardId: string, offset: number, limit: number): ArchiveList {
  if (!record(value) || value.format !== 'historical-archive-discovery/v1' || value.board_id !== boardId
      || !Array.isArray(value.items) || value.items.length > limit) fail();
  const seen = new Set<string>();
  const items = value.items.map((item: unknown): ArchiveItem => {
    if (!record(item) || !origin(item.origin) || !identifier(item.archive_id) || !Array.isArray(item.sections)
        || !item.sections.includes('content') || new Set(item.sections).size !== item.sections.length
        || item.sections.some(section => !archiveSections.includes(section))) fail();
    const key = JSON.stringify([item.origin.kind, item.origin.id]);
    if (seen.has(key)) fail();
    seen.add(key);
    return { origin: { kind: item.origin.kind, id: item.origin.id }, archive_id: item.archive_id,
      sections: item.sections as ArchiveSection[] };
  });
  return { items, next_offset: nextOffset(value.next_offset, offset, items.length) };
}

export function parseArchivePage(
  value: unknown, boardId: string, item: ArchiveItem, section: ArchiveSection, offset: number, limit: number,
): ArchivePage {
  if (!record(value) || value.format !== 'historical-archive-section/v1' || value.board_id !== boardId
      || !origin(value.origin) || value.origin.kind !== item.origin.kind || value.origin.id !== item.origin.id
      || value.archive_id !== item.archive_id || value.section !== section || !Array.isArray(value.records)
      || value.records.length > limit || value.records.some(item => !record(item))) fail();
  return { records: value.records as Record<string, unknown>[],
    next_offset: nextOffset(value.next_offset, offset, value.records.length) };
}

export function useHistoricalArchivesApi() {
  const client = useApiClient();
  return useMemo(() => ({
    async list(boardId: string, offset: number, signal: AbortSignal): Promise<ArchiveList> {
      const result = await client.fetchJson<unknown>(
        `/boards/${encodeURIComponent(boardId)}/historical-archives?offset=${offset}&limit=50`,
        { signal, cache: 'no-store' },
      );
      return parseArchiveList(result, boardId, offset, 50);
    },
    async read(boardId: string, item: ArchiveItem, section: ArchiveSection, offset: number, signal: AbortSignal): Promise<ArchivePage> {
      const path = [boardId, item.origin.kind, item.origin.id].map(encodeURIComponent);
      const result = await client.fetchJson<unknown>(
        `/boards/${path[0]}/historical-archives/${path[1]}/${path[2]}/${section}?offset=${offset}&limit=50`,
        { signal, cache: 'no-store' },
      );
      return parseArchivePage(result, boardId, item, section, offset, 50);
    },
  }), [client]);
}
