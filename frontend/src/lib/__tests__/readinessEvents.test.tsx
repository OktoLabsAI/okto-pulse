import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { AuthenticatedFetch } from '../authFetch';
import { READINESS_CHANGED, affectsDeliveryReadiness, notifyReadinessChanged } from '../readinessEvents';
import { patchBoardSettings } from '@/services/board-settings-api';
import { useBoardDeliveryReadiness } from '@/components/code-traceability/useBoardDeliveryReadiness';
import { useDeliveryReadiness } from '@/components/code-traceability/useDeliveryReadiness';
import { boardSummary, cards, readiness, metric } from '@/components/code-traceability/__tests__/readinessFixture';

const api = vi.hoisted(() => ({ getDeliveryReadinessSummary: vi.fn(), getDeliveryReadiness: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it.each([
  ['/specs/s/scenarios/ts/evidence/reports', 'POST'],
  ['/specs/s/scenarios/ts/link-task/test', 'POST'],
  ['/specs/s/scenarios/ts/evidence', 'PATCH'],
  ['/boards/b/cards/c/implementation-targets/t', 'PATCH'],
  ['/boards/b/cards/c/implementation-targets/t/executions', 'POST'],
  ['/boards/b/cards/c/specs/s/delivery-evidence', 'POST'],
  ['/boards/b/specs/s/decision-reviews', 'POST'],
  ['/boards/b/specs/s/delivery-evidence', 'POST'],
  ['/boards/b/code-investigation-receipts/r/revoke', 'POST'],
  ['/boards/b/code-traceability-waivers/w', 'DELETE'],
  ['/boards/b/policy-waivers/w', 'DELETE'],
  ['/specs/s/structured-entities/functional_requirement/fr', 'PATCH'],
  ['/specs/s/move', 'POST'],
  ['/cards/c/move', 'POST'],
  ['/api/v1/boards/b', 'PATCH'],
  ['/boards/b/guidelines/g/versions', 'POST'],
  ['/agents/a/boards/b', 'PATCH'],
])('invalidates on successful native mutation %s %s', async (url, method) => {
  expect(affectsDeliveryReadiness(url, method)).toBe(true);
  const changed = vi.fn(); window.addEventListener(READINESS_CHANGED, changed);
  try {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }));
    await new AuthenticatedFetch(async () => null).fetch(url, { method });
    await Promise.resolve();
    expect(changed).toHaveBeenCalledOnce();
  } finally { window.removeEventListener(READINESS_CHANGED, changed); }
});

it.each([
  ['/boards/b/specs/s/delivery-evidence?view=readiness', 'GET', 200],
  ['/boards/b/specs/s/code-evidence/rebase/preview', 'POST', 200],
  ['/kg/boards/b/query', 'POST', 200],
  ['/cards/test/move', 'POST', 409],
  ['/boards/b/specs/s/delivery-evidence', 'POST', 403],
])('does not signal reads/previews/unrelated calls or rejected writes: %s', async (url, method, status) => {
  const changed = vi.fn(); window.addEventListener(READINESS_CHANGED, changed);
  try {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status }));
    await new AuthenticatedFetch(async () => null).fetch(url, { method });
    await Promise.resolve(); expect(changed).not.toHaveBeenCalled();
  } finally { window.removeEventListener(READINESS_CHANGED, changed); }
});

it('coalesces a synchronous burst and includes the direct board-settings client', async () => {
  const changed = vi.fn(); window.addEventListener(READINESS_CHANGED, changed);
  try {
    notifyReadinessChanged(); notifyReadinessChanged(); notifyReadinessChanged();
    await Promise.resolve(); expect(changed).toHaveBeenCalledOnce();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ settings: { delivery_evidence_gate: 'blocking' } })));
    await patchBoardSettings('b', { delivery_evidence_gate: 'blocking' });
    await Promise.resolve(); expect(changed).toHaveBeenCalledTimes(2);
  } finally { window.removeEventListener(READINESS_CHANGED, changed); }
});

it('U07: a Test result mutation refreshes the implementation cover and open Spec without reopening either', async () => {
  api.getDeliveryReadinessSummary.mockResolvedValue(boardSummary());
  api.getDeliveryReadiness.mockResolvedValue(readiness(null));
  const view = renderHook(() => ({
    board: useBoardDeliveryReadiness('b', cards, true, '0'),
    spec: useDeliveryReadiness('b', 's'),
  }));
  await waitFor(() => expect(view.result.current.spec.value?.verification.percent).toBe(75));
  await waitFor(() => expect(view.result.current.board.c.value?.verification.percent).toBe(75));
  const next = boardSummary();
  next.cards.forEach(row => { row.delivery_readiness.verification = metric(100, 8); });
  api.getDeliveryReadinessSummary.mockResolvedValue(next);
  api.getDeliveryReadiness.mockResolvedValue({ ...readiness(null), verification: metric(100, 8) });
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'));
  await act(async () => {
    await new AuthenticatedFetch(async () => null).fetch('/specs/s/scenarios/ts/evidence/reports', { method: 'POST' });
  });
  await waitFor(() => expect(view.result.current.board.c.value?.verification.percent).toBe(100));
  await waitFor(() => expect(view.result.current.spec.value?.verification.percent).toBe(100));
  expect(api.getDeliveryReadinessSummary).toHaveBeenCalledTimes(2);
  expect(api.getDeliveryReadiness).toHaveBeenCalledTimes(2);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(cards[0].status).toBe('in_progress');
});
