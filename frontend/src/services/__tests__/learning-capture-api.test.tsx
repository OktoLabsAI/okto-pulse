import { renderHook, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { parseCaptureHistory, parseCaptureSource, useLearningCaptureApi } from '../learning-capture-api';

const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
beforeEach(() => client.fetchJson.mockReset());
afterEach(cleanup);
const source = { contract_version: 'learning-capture-context/v1', board_id: 'board', bug_id: 'bug',
  source_digest: 'a'.repeat(64), source_policy_version: 1,
  scenarios: [{ id: 'scenario', title: 'Inspection', authenticated: true }] };
const item = { learning_id: 'learning', generation: 0, source_revision: 1, fingerprint: 'b'.repeat(64),
  capture: { capture_format: 'learning-capture/v1', capture_id: 'capture', author_id: 'author',
    captured_at: '2026-09-24T15:00:00Z', content: 'Lesson', context: 'Context', applicability: 'Scope',
    source: { board_id: 'board', bug_id: 'bug', digest: 'a'.repeat(64), policy_version: 1 } } };
const history = { contract_version: 'learning-capture-history/v1', board_id: 'board', bug_id: 'bug', items: [item], next_cursor: null };

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
