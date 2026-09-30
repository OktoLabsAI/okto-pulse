import { useMemo } from 'react';
import { useApiClient } from '@/contexts/ApiContext';
import type { LearningIntentRequest } from '@/types';

export interface CaptureSource {
  source_digest: string;
  source_policy_version: number;
  scenarios: { id: string; title: string; authenticated: boolean }[];
}
export interface CaptureRequest {
  board_id: string; capture_id: string; expected_source_digest: string; expected_source_version: number;
  content: string; context: string; applicability: string; scenario_ids: string[];
  intent?: LearningIntentRequest;
}
export type CaptureIntent = { kind: 'create' } | {
  kind: 'reuse' | 'supersede'; target_node_id: string; target_generation: number;
  expected_fingerprint: string; reason: string; scope?: 'source_bug';
};
export interface CaptureLineage {
  state: 'recorded' | 'unverified'; limitation: string | null;
  target_claim?: { revision: number; fingerprint: string };
  successor_birth?: { revision: number; fingerprint: string };
}
export interface CaptureHistoryItem {
  learning_id: string; generation: number; source_revision: number; fingerprint: string;
  capture: { capture_id: string; author_id: string; captured_at: string; content: string;
    context: string; applicability: string; source: { digest: string; policy_version: number }; intent: CaptureIntent };
  lineage?: CaptureLineage;
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
function parseIntent(value: unknown, format: unknown, learning: string, generation: number): CaptureIntent {
  if (!object(value) || !['learning-capture/v1', 'learning-capture/v2'].includes(String(format))) invalid();
  const scoped = format === 'learning-capture/v2';
  const keys = ['kind', 'target_node_id', 'target_generation', 'expected_fingerprint', 'reason', ...(scoped ? ['scope'] : [])];
  if (Object.keys(value).length !== keys.length || !keys.every(key => key in value)) invalid();
  if (value.kind === 'create') {
    if (scoped || [value.target_node_id, value.target_generation, value.expected_fingerprint, value.reason].some(field => field !== null)) invalid();
    return { kind: 'create' };
  }
  if (!['reuse', 'supersede'].includes(String(value.kind)) || !text(value.target_node_id)
    || !integer(value.target_generation) || !digest(value.expected_fingerprint) || !text(value.reason, 16384)
    || ((value.target_node_id === learning && value.target_generation === generation) !== (value.kind === 'reuse'))
    || (scoped && (value.kind !== 'supersede' || value.scope !== 'source_bug'))) invalid();
  return { kind: value.kind as 'reuse' | 'supersede', target_node_id: value.target_node_id,
    target_generation: value.target_generation, expected_fingerprint: value.expected_fingerprint,
    reason: value.reason, ...(scoped ? { scope: 'source_bug' as const } : {}) };
}
function parseLineage(value: unknown, intent: CaptureIntent, bug: string, fingerprint: string, revision: number): CaptureLineage | undefined {
  if (value === undefined) return undefined;
  if (intent.kind !== 'supersede' || intent.scope !== 'source_bug' || !object(value)
    || value.contract_version !== 'learning-scope-history/v1' || value.scope !== 'source_bug'
    || value.bug_id !== bug || value.capture_fingerprint !== fingerprint
    || value.current_applicability !== 'not_assessed' || value.graph_projection !== 'not_assessed'
    || !object(value.target) || value.target.learning_id !== intent.target_node_id
    || value.target.generation !== intent.target_generation || value.target.fingerprint !== intent.expected_fingerprint) invalid();
  if (value.state === 'unverified') {
    if (!['not_recorded', 'history_limit', 'history_capability_unavailable', 'target_history_unavailable'].includes(String(value.limitation))
      || value.target_claim !== undefined || value.successor_birth !== undefined) invalid();
    return { state: 'unverified', limitation: value.limitation as string };
  }
  if (value.state !== 'recorded' || value.limitation !== null || !object(value.target_claim) || !object(value.successor_birth)
    || !integer(value.target_claim.revision, 1) || !digest(value.target_claim.fingerprint)
    || value.successor_birth.revision !== revision + 1 || !digest(value.successor_birth.fingerprint)) invalid();
  return { state: 'recorded', limitation: null,
    target_claim: { revision: value.target_claim.revision, fingerprint: value.target_claim.fingerprint },
    successor_birth: { revision: value.successor_birth.revision as number, fingerprint: value.successor_birth.fingerprint } };
}
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
    if (!text(capture.capture_id) || !text(capture.author_id)
      || !text(capture.captured_at) || !Number.isFinite(Date.parse(capture.captured_at))
      || !text(capture.content, 65536) || !text(capture.context, 65536) || !text(capture.applicability, 65536)
      || !object(capture.source) || capture.source.board_id !== board || capture.source.bug_id !== bug
      || !digest(capture.source.digest) || !integer(capture.source.policy_version, 1)) invalid();
    const intent = parseIntent(capture.intent, capture.capture_format, row.learning_id, row.generation);
    const lineage = parseLineage(row.lineage, intent, bug, row.fingerprint, row.source_revision);
    const key = `${row.learning_id}:${row.generation}:${row.source_revision}`;
    if (seen.has(key)) invalid();
    seen.add(key);
    return { learning_id: row.learning_id, generation: row.generation, source_revision: row.source_revision,
      fingerprint: row.fingerprint, ...(lineage ? { lineage } : {}), capture: { capture_id: capture.capture_id, author_id: capture.author_id,
        captured_at: capture.captured_at, content: capture.content, context: capture.context, applicability: capture.applicability,
        source: { digest: capture.source.digest, policy_version: capture.source.policy_version }, intent } };
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
