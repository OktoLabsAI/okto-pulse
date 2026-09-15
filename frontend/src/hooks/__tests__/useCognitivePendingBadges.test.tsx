import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useCognitivePendingBadges } from '../useCognitivePendingBadges';
import { getKGCognitivePendingBadges, type KGCognitivePendingBadgesResponse } from '@/services/kg-health-api';

const permissions = vi.hoisted(() => ({ allowed: true }));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ isLoading: false, error: null, ownerReviewRequired: false, has: () => permissions.allowed }),
}));
vi.mock('@/services/kg-health-api', () => ({ getKGCognitivePendingBadges: vi.fn() }));

const response = (generation: string): KGCognitivePendingBadgesResponse => ({
  board_id: 'board', readonly: true,
  badges: {}, selected_kg_generation_id: generation, eligible_entity_types: ['spec'],
});
beforeEach(() => { vi.resetAllMocks(); permissions.allowed = true; });
afterEach(cleanup);

it('refresh alone triggers another batch and keeps callback identity stable', async () => {
  vi.mocked(getKGCognitivePendingBadges).mockResolvedValueOnce(response('one')).mockResolvedValueOnce(response('two'));
  const { result } = renderHook(() => useCognitivePendingBadges('board', ['spec:a', 'spec:a']));
  await waitFor(() => expect(result.current.selectedKgGenerationId).toBe('one'));
  const refresh = result.current.refresh;
  act(refresh);
  await waitFor(() => expect(result.current.selectedKgGenerationId).toBe('two'));
  expect(getKGCognitivePendingBadges).toHaveBeenCalledTimes(2);
  expect(getKGCognitivePendingBadges).toHaveBeenLastCalledWith('board', ['spec:a'], { kgGenerationId: null }, expect.any(AbortSignal));
  expect(result.current.refresh).toBe(refresh);
});

it('ignores superseded responses even if the transport resolves after abort', async () => {
  let resolveOld!: (value: KGCognitivePendingBadgesResponse) => void;
  vi.mocked(getKGCognitivePendingBadges).mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; })).mockResolvedValueOnce(response('new'));
  const { result } = renderHook(() => useCognitivePendingBadges('board', ['spec:a']));
  const oldSignal = vi.mocked(getKGCognitivePendingBadges).mock.calls[0][3];
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.selectedKgGenerationId).toBe('new'));
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => resolveOld(response('old')));
  expect(result.current.selectedKgGenerationId).toBe('new');
});

it('can refresh after an error', async () => {
  vi.mocked(getKGCognitivePendingBadges).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(response('recovered'));
  const { result } = renderHook(() => useCognitivePendingBadges('board', ['spec:a']));
  await waitFor(() => expect(result.current.error?.message).toBe('offline'));
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.selectedKgGenerationId).toBe('recovered'));
  expect(result.current.error).toBeNull();
});

it.each(['missing-board', 'empty-refs', 'denied'])('refresh preserves the %s request guard', async (mode) => {
  permissions.allowed = mode !== 'denied';
  const { result } = renderHook(() => useCognitivePendingBadges(mode === 'missing-board' ? null : 'board', mode === 'empty-refs' ? [] : ['spec:a']));
  act(() => result.current.refresh());
  expect(getKGCognitivePendingBadges).not.toHaveBeenCalled();
  expect(result.current.loading).toBe(false);
});
