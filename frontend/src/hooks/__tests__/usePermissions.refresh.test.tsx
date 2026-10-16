import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { PermissionsResponse } from '@/services/permissions-api';

const api = vi.hoisted(() => ({ getMyPermissions: vi.fn() }));
vi.mock('@/services/permissions-api', () => api);
import { refreshBoardPermissions, usePermissions } from '../usePermissions';

const permissions = (board_id: string, read: boolean): PermissionsResponse => ({
  board_id, preset_name: 'Custom', flags: { spec: { entity: { read } } },
  owner_review_required: false, review_reason: null,
});

describe('authoritative permission refresh', () => {
  it('bypasses the TTL and updates every mounted reader of the Board', async () => {
    const board = 'refresh-subscribers';
    api.getMyPermissions.mockResolvedValue(permissions(board, true));
    const first = renderHook(() => usePermissions(board));
    const second = renderHook(() => usePermissions(board));
    await waitFor(() => expect(first.result.current.isLoading).toBe(false));
    expect(first.result.current.has('spec.entity.read')).toBe(true);
    api.getMyPermissions.mockResolvedValue(permissions(board, false));
    await act(async () => { await refreshBoardPermissions(board); });
    expect(first.result.current.has('spec.entity.read')).toBe(false);
    expect(second.result.current.has('spec.entity.read')).toBe(false);
  });

  it('does not let an old authorization request repopulate the refreshed cache', async () => {
    const board = 'refresh-race';
    let old!: (value: PermissionsResponse) => void;
    api.getMyPermissions.mockImplementationOnce(() => new Promise(resolve => { old = resolve; }));
    const first = renderHook(() => usePermissions(board));
    api.getMyPermissions.mockResolvedValue(permissions(board, false));
    await act(async () => { await refreshBoardPermissions(board); });
    await act(async () => { old(permissions(board, true)); });
    const second = renderHook(() => usePermissions(board));
    await waitFor(() => expect(second.result.current.isLoading).toBe(false));
    expect(first.result.current.has('spec.entity.read')).toBe(false);
    expect(second.result.current.has('spec.entity.read')).toBe(false);
  });

  it('fails closed when changed authority cannot be reverified', async () => {
    const board = 'refresh-denied';
    api.getMyPermissions.mockResolvedValue(permissions(board, true));
    const hook = renderHook(() => usePermissions(board));
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    api.getMyPermissions.mockRejectedValue(new Error('Access revoked'));
    await act(async () => { await expect(refreshBoardPermissions(board)).rejects.toThrow('Access revoked'); });
    expect(hook.result.current.error?.message).toBe('Access revoked');
    expect(hook.result.current.has('spec.entity.read')).toBe(false);
  });
});
