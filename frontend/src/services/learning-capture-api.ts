import { useMemo } from 'react';
import { useApiClient } from '@/contexts/ApiContext';

export interface CaptureSource {
  source_digest: string;
  source_policy_version: number;
  scenarios: { id: string; title: string; authenticated: boolean }[];
}
export interface CaptureRequest {
  board_id: string; capture_id: string; expected_source_digest: string; expected_source_version: number;
  content: string; context: string; applicability: string; scenario_ids: string[];
}
export interface CaptureHistoryItem {
  learning_id: string; generation: number; source_revision: number; fingerprint: string;
  capture: { capture_id: string; author_id: string; captured_at: string; content: string;
    context: string; applicability: string; source: { digest: string; policy_version: number } };
}
export interface CaptureHistory { items: CaptureHistoryItem[]; next_cursor: string | null }
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function text(value: unknown, max = 4096): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= max;
}
function digest(value: unknown): value is string { return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value); }
function integer(value: unknown, min = 0): value is number { return Number.isSafeInteger(value) && Number(value) >= min; }
function invalid(): never { throw new Error('Invalid Learning capture response'); }
export function parseCaptureSource(value: unknown, board: string, bug: string): CaptureSource {
  if (!object(value) || value.contract_version !== 'learning-capture-context/v1'
    || value.board_id !== board || value.bug_id !== bug || !digest(value.source_digest)
    || !integer(value.source_policy_version, 1) || !Array.isArray(value.scenarios)) invalid();
  const seen = new Set<string>();
  const scenarios = value.scenarios.map(row => {
    if (!object(row) || !text(row.id) || seen.has(row.id) || !text(row.title, 65536)
      || typeof row.authenticated !== 'boolean') invalid();
    seen.add(row.id);
    return { id: row.id, title: row.title, authenticated: row.authenticated };
  });
  return { source_digest: value.source_digest, source_policy_version: value.source_policy_version, scenarios };
}
export function parseCaptureHistory(value: unknown, board: string, bug: string): CaptureHistory {
  if (!object(value) || value.contract_version !== 'learning-capture-history/v1'
    || value.board_id !== board || value.bug_id !== bug || !Array.isArray(value.items) || value.items.length > 200
    || (value.next_cursor !== null && !text(value.next_cursor))) invalid();
  const seen = new Set<string>();
  const items = value.items.map(row => {
    if (!object(row) || !text(row.learning_id) || !integer(row.generation) || !integer(row.source_revision)
      || !digest(row.fingerprint) || !object(row.capture)) invalid();
    const capture = row.capture;
    if (capture.capture_format !== 'learning-capture/v1' || !text(capture.capture_id) || !text(capture.author_id)
      || !text(capture.captured_at) || !Number.isFinite(Date.parse(capture.captured_at))
      || !text(capture.content, 65536) || !text(capture.context, 65536) || !text(capture.applicability, 65536)
      || !object(capture.source) || capture.source.board_id !== board || capture.source.bug_id !== bug
      || !digest(capture.source.digest) || !integer(capture.source.policy_version, 1)) invalid();
    const key = `${row.learning_id}:${row.generation}:${row.source_revision}`;
    if (seen.has(key)) invalid();
    seen.add(key);
    return { learning_id: row.learning_id, generation: row.generation, source_revision: row.source_revision,
      fingerprint: row.fingerprint, capture: { capture_id: capture.capture_id, author_id: capture.author_id,
        captured_at: capture.captured_at, content: capture.content, context: capture.context, applicability: capture.applicability,
        source: { digest: capture.source.digest, policy_version: capture.source.policy_version } } };
  });
  return { items, next_cursor: value.next_cursor as string | null };
}
export function useLearningCaptureApi() {
  const client = useApiClient();
  return useMemo(() => ({
    async source(board: string, bug: string, signal: AbortSignal) {
      return parseCaptureSource(await client.fetchJson<unknown>(
        `/bugs/${encodeURIComponent(bug)}/learning-capture-context?${new URLSearchParams({ board_id: board })}`,
        { signal, cache: 'no-store' }), board, bug);
    },
    async history(board: string, bug: string, cursor: string | null, signal: AbortSignal) {
      const query = new URLSearchParams({ board_id: board, limit: '20' });
      if (cursor) query.set('cursor', cursor);
      const page = parseCaptureHistory(await client.fetchJson<unknown>(
        `/bugs/${encodeURIComponent(bug)}/learning-captures?${query}`, { signal, cache: 'no-store' }), board, bug);
      if (cursor && page.next_cursor === cursor) invalid();
      return page;
    },
    async create(bug: string, request: CaptureRequest, signal: AbortSignal) {
      const value = await client.fetchJson<unknown>(`/bugs/${encodeURIComponent(bug)}/learning-captures`,
        { method: 'POST', body: JSON.stringify(request), signal });
      if (!object(value) || value.capture_id !== request.capture_id || !text(value.learning_id)
        || !digest(value.fingerprint) || value.status !== 'captured_pending_materialization') invalid();
      return { capture_id: value.capture_id, learning_id: value.learning_id };
    },
  }), [client]);
}
