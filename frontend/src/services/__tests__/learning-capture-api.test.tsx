import { renderHook, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { parseCaptureHistory, parseCaptureSource, useLearningCaptureApi } from '../learning-capture-api';
import type { LearningIntentRequest } from '@/types';

const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
beforeEach(() => client.fetchJson.mockReset());
afterEach(cleanup);
const source = { contract_version: 'learning-capture-context/v1', board_id: 'board', bug_id: 'bug',
  source_digest: 'a'.repeat(64), source_policy_version: 1,
  scenarios: [{ id: 'scenario', title: 'Inspection', authenticated: true }] };
const item = { learning_id: 'learning', generation: 0, source_revision: 1, fingerprint: 'b'.repeat(64),
  capture: { capture_format: 'learning-capture/v2', capture_id: 'capture', author_id: 'author',
    captured_at: '2026-09-24T15:00:00Z', content: 'Lesson', context: 'Context', applicability: 'Scope',
    source: { board_id: 'board', bug_id: 'bug', digest: 'a'.repeat(64), policy_version: 1 },
    intent: { kind: 'create', target_node_id: null, target_generation: null, expected_fingerprint: null, reason: null, scope: null } } };
const history = { contract_version: 'learning-capture-history/v1', board_id: 'board', bug_id: 'bug', items: [item], next_cursor: null };

const suggestions = { contract_version: 'learning-candidates/v1', status: 'available', exhaustive: false,
  applicability: 'not_assessed', data_source: 'graph_and_cognitive_source', similarity_floor: 0.6, candidate_window: 20,
  limitation: null, limitations: [], graph_snapshot: '12', vector_regime: 'exact', items: [{ learning_id: 'target',
    generation: 0, source_revision: 1, fingerprint: 'd'.repeat(64), content: 'Old lesson', context: 'Old context', similarity: .96, suggestion: 'reuse' }] };

it('loads candidates only on request through the existing scoped context surface', async () => {
  client.fetchJson.mockResolvedValue({ ...source, candidates: suggestions });
  const { result } = renderHook(() => useLearningCaptureApi());
  const signal = new AbortController().signal;
  const page = await result.current.candidates('board', 'bug', 'A related lesson', signal);
  expect(page.items[0].fingerprint).toBe('d'.repeat(64));
  expect(client.fetchJson).toHaveBeenCalledWith('/bugs/bug/learning-capture-context?board_id=board&candidate_query=A+related+lesson', { signal, cache: 'no-store' });
});

it.each(['scope', 'current', 'score', 'band', 'fingerprint', 'duplicate', 'limit', 'unknown'])('rejects malformed candidate evidence: %s', damage => {
  const bad = structuredClone(suggestions);
  if (damage === 'scope') bad.data_source = 'untrusted';
  else if (damage === 'current') bad.applicability = 'approved';
  else if (damage === 'score') bad.items[0].similarity = .59;
  else if (damage === 'band') bad.items[0].suggestion = 'related';
  else if (damage === 'fingerprint') bad.items[0].fingerprint = 'bad';
  else if (damage === 'duplicate') bad.items.push(bad.items[0]);
  else if (damage === 'limit') bad.candidate_window = 1000;
  else bad.status = 'unknown';
  expect(() => parseCaptureSource({ ...source, candidates: bad }, 'board', 'bug')).toThrow();
});

it('preserves unavailable search without manufacturing an empty successful page', () => {
  const result = parseCaptureSource({ ...source, candidates: { ...suggestions, status: 'unavailable', items: [],
    limitation: 'search_unavailable' } }, 'board', 'bug');
  expect(result.candidates).toMatchObject({ status: 'unavailable', limitation: 'search_unavailable', items: [] });
});

it.each(['reuse', 'supersede'] as const)('sends an explicit %s intent unchanged without selecting a new target on conflict', async kind => {
  const intent: LearningIntentRequest = { kind, target_node_id: 'target', target_generation: 2,
    expected_fingerprint: 'c'.repeat(64), reason: 'Explicit applicability', ...(kind === 'supersede' ? { scope: 'source_bug' as const } : {}) } as LearningIntentRequest;
  const request = { board_id: 'board', capture_id: 'capture', expected_source_digest: source.source_digest,
    expected_source_version: 1, content: 'L', context: 'C', applicability: 'A', scenario_ids: ['scenario'], intent };
  const conflict = new Error('learning_capture_target_changed');
  client.fetchJson.mockRejectedValueOnce(conflict);
  const { result } = renderHook(() => useLearningCaptureApi());
  const signal = new AbortController().signal;
  await expect(result.current.create('bug', request, signal)).rejects.toBe(conflict);
  expect(client.fetchJson).toHaveBeenCalledTimes(1);
  expect(JSON.parse(client.fetchJson.mock.calls[0][1].body).intent).toEqual(intent);
});

function scopedHistory() {
  return { ...history, items: [{ ...item, capture: { ...item.capture, capture_format: 'learning-capture/v2',
    intent: { kind: 'supersede', target_node_id: 'previous', target_generation: 0,
      expected_fingerprint: 'c'.repeat(64), reason: 'Corrected lesson', scope: 'source_bug' } },
    lineage: { contract_version: 'learning-scope-history/v1', scope: 'source_bug', bug_id: 'bug',
      capture_fingerprint: item.fingerprint, target: { learning_id: 'previous', generation: 0, fingerprint: 'c'.repeat(64) },
      state: 'recorded', limitation: null as string | null, current_applicability: 'not_assessed', graph_projection: 'not_assessed',
      target_claim: { revision: 1, fingerprint: 'd'.repeat(64) }, successor_birth: { revision: 2, fingerprint: 'e'.repeat(64) } } }] };
}

it('preserves qualified scoped history without treating it as current graph state', () => {
  const result = parseCaptureHistory(scopedHistory(), 'board', 'bug').items[0];
  expect(result.capture.intent).toMatchObject({ kind: 'supersede', scope: 'source_bug', target_node_id: 'previous' });
  expect(result.lineage).toMatchObject({ state: 'recorded', successor_birth: { revision: 2 } });
  expect(result).not.toHaveProperty('current');
});

it.each(['bug', 'target', 'capture', 'revision', 'current', 'graph', 'limitation', 'self', 'scope', 'extra'])('rejects inconsistent scoped lineage: %s', damage => {
  const bad = scopedHistory(); const row = bad.items[0];
  if (damage === 'bug') row.lineage.bug_id = 'foreign';
  else if (damage === 'target') row.lineage.target.fingerprint = 'f'.repeat(64);
  else if (damage === 'capture') row.lineage.capture_fingerprint = 'f'.repeat(64);
  else if (damage === 'revision') row.lineage.successor_birth.revision = 5;
  else if (damage === 'current') row.lineage.current_applicability = 'approved';
  else if (damage === 'graph') row.lineage.graph_projection = 'available';
  else if (damage === 'limitation') row.lineage.limitation = 'history_limit';
  else if (damage === 'self') row.capture.intent.target_node_id = row.learning_id;
  else if (damage === 'scope') row.capture.intent.scope = 'global';
  else Object.assign(row.capture.intent, { approval: true });
  expect(() => parseCaptureHistory(bad, 'board', 'bug')).toThrow();
});

it.each(['not_recorded', 'history_limit', 'history_capability_unavailable', 'target_history_unavailable'])('preserves an explicit historical limitation: %s', limitation => {
  const raw = scopedHistory(); const { target_claim: _claim, successor_birth: _birth, ...lineage } = raw.items[0].lineage;
  const row = { ...raw.items[0], lineage: { ...lineage, state: 'unverified', limitation } };
  expect(parseCaptureHistory({ ...raw, items: [row] }, 'board', 'bug').items[0].lineage).toEqual({ state: 'unverified', limitation });
});

it('keeps explicit null scope unscoped and accepts reuse only for the selected identity', () => {
  const { scope: _scope, ...intent } = scopedHistory().items[0].capture.intent;
  const unscoped = { ...item, capture: { ...item.capture, intent: { ...intent, scope: null } } };
  expect(parseCaptureHistory({ ...history, items: [unscoped] }, 'board', 'bug').items[0].capture.intent).not.toHaveProperty('scope');
  const reuse = { ...item, capture: { ...item.capture, intent: { ...intent, scope: null, kind: 'reuse', target_node_id: item.learning_id } } };
  expect(parseCaptureHistory({ ...history, items: [reuse] }, 'board', 'bug').items[0].capture.intent.kind).toBe('reuse');
  reuse.capture.intent.target_node_id = 'foreign';
  expect(() => parseCaptureHistory({ ...history, items: [reuse] }, 'board', 'bug')).toThrow();
});

it('parses scoped source choices and history without treating presence as approval', () => {
  expect(parseCaptureSource(source, 'board', 'bug').scenarios[0].authenticated).toBe(true);
  const result = parseCaptureHistory(history, 'board', 'bug');
  expect(result.items[0].capture.content).toBe('Lesson');
  expect(result.items[0]).not.toHaveProperty('current');
});

it.each(['board', 'digest', 'version', 'duplicate', 'trust'])('rejects malformed source %s', kind => {
  const bad = structuredClone(source);
  if (kind === 'board') bad.board_id = 'foreign';
  else if (kind === 'digest') bad.source_digest = 'bad';
  else if (kind === 'version') bad.source_policy_version = 0;
  else if (kind === 'duplicate') bad.scenarios.push(bad.scenarios[0]);
  else (bad.scenarios[0] as unknown as { authenticated: string }).authenticated = 'true';
  expect(() => parseCaptureSource(bad, 'board', 'bug')).toThrow();
});

it.each(['scope', 'format', 'duplicate', 'cursor', 'time'])('rejects malformed capture history %s', kind => {
  const bad = structuredClone(history);
  if (kind === 'scope') bad.items[0].capture.source.bug_id = 'foreign';
  else if (kind === 'format') bad.items[0].capture.capture_format = 'unknown/v2';
  else if (kind === 'duplicate') bad.items.push(bad.items[0]);
  else if (kind === 'cursor') (bad as unknown as { next_cursor: string }).next_cursor = '';
  else bad.items[0].capture.captured_at = 'not-a-date';
  expect(() => parseCaptureHistory(bad, 'board', 'bug')).toThrow();
});

it('uses the authenticated client, scoped URLs and cancellation for every operation', async () => {
  const signal = new AbortController().signal;
  client.fetchJson.mockResolvedValueOnce(source).mockResolvedValueOnce(history)
    .mockResolvedValueOnce({ capture_id: 'capture', learning_id: 'learning', fingerprint: 'b'.repeat(64), status: 'captured_pending_materialization' });
  const { result } = renderHook(() => useLearningCaptureApi());
  await result.current.source('board', 'bug', signal);
  await result.current.history('board', 'bug', 'cursor 1', signal);
  const request = { board_id: 'board', capture_id: 'capture', expected_source_digest: 'a'.repeat(64), expected_source_version: 1,
    content: 'Lesson', context: 'Context', applicability: 'Scope', scenario_ids: ['scenario'] };
  await result.current.create('bug', request, signal);
  expect(client.fetchJson.mock.calls[0]).toEqual(['/bugs/bug/learning-capture-context?board_id=board', { signal, cache: 'no-store' }]);
  expect(client.fetchJson.mock.calls[1][0]).toContain('cursor=cursor+1');
  expect(client.fetchJson.mock.calls[2]).toEqual(['/bugs/bug/learning-captures', { method: 'POST', body: JSON.stringify(request), signal }]);
});

it('rejects an unrelated acknowledgement and a non-advancing history cursor', async () => {
  const signal = new AbortController().signal;
  client.fetchJson.mockResolvedValueOnce({ ...history, next_cursor: 'same' }).mockResolvedValueOnce({ capture_id: 'foreign' });
  const { result } = renderHook(() => useLearningCaptureApi());
  await expect(result.current.history('board', 'bug', 'same', signal)).rejects.toThrow();
  await expect(result.current.create('bug', { board_id: 'board', capture_id: 'capture', expected_source_digest: 'a'.repeat(64),
    expected_source_version: 1, content: 'L', context: 'C', applicability: 'A', scenario_ids: ['scenario'] }, signal)).rejects.toThrow();
});

it.each(['old_format', 'missing_scope'])('refuses incompatible capture without rewriting history: %s', damage => {
  const bad = structuredClone(history);
  if (damage === 'old_format') bad.items[0].capture.capture_format = 'learning-capture/v1';
  else Reflect.deleteProperty(bad.items[0].capture.intent, 'scope');
  const before = structuredClone(bad);
  expect(() => parseCaptureHistory(bad, 'board', 'bug')).toThrow();
  expect(bad).toEqual(before);
});
