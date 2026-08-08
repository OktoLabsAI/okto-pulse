import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  hasEffectivePermission,
  SKA_PERMISSION_INTRODUCTION_V1_LEAVES,
  usePermissions,
} from '@/hooks/usePermissions';
import { getMyPermissions } from '@/services/permissions-api';
import type { PermissionsResponse } from '@/services/permissions-api';

vi.mock('@/services/permissions-api', async (importOriginal) => {
  const original = await importOriginal<
    typeof import('@/services/permissions-api')
  >();
  return {
    ...original,
    getMyPermissions: vi.fn(),
  };
});

const getMyPermissionsMock = vi.mocked(getMyPermissions);

function response(
  flags: Record<string, unknown>,
  ownerReviewRequired = false,
  boardId = 'board-1',
): PermissionsResponse {
  return {
    board_id: boardId,
    preset_name: 'Custom',
    flags,
    owner_review_required: ownerReviewRequired,
    review_reason: ownerReviewRequired ? 'preset_lineage_cycle' : null,
  };
}

describe('usePermissions SK-A/v1 fail-closed introduction', () => {
  beforeEach(() => {
    getMyPermissionsMock.mockReset();
  });

  it('pins the exact ten introduced leaves', () => {
    expect(new Set(SKA_PERMISSION_INTRODUCTION_V1_LEAVES)).toEqual(
      new Set([
        'ideation.quality.read',
        'ideation.quality.assess',
        'refinement.quality.read',
        'refinement.quality.assess',
        'spec.quality.read',
        'spec.quality.assess',
        'refinement.research_decisions.read',
        'refinement.research_decisions.append',
        'spec.checklist.read',
        'spec.checklist.execute',
      ]),
    );
  });

  it('denies introduced leaves while data is unavailable or absent', () => {
    expect(hasEffectivePermission(null, 'spec.quality.read')).toBe(false);
    expect(
      hasEffectivePermission(response({ spec: { quality: {} } }), 'spec.quality.read'),
    ).toBe(false);
  });

  it('keeps historical render-through compatibility', () => {
    expect(hasEffectivePermission(null, 'board.read')).toBe(true);
    expect(hasEffectivePermission(response({}), 'board.read')).toBe(true);
  });

  it('requires both explicit true and historical authority', () => {
    const granted = response({
      spec: {
        entity: { read: true },
        quality: { read: true },
      },
    });
    expect(hasEffectivePermission(granted, 'spec.quality.read')).toBe(true);
    expect(
      hasEffectivePermission(
        response({
          spec: {
            entity: { read: false },
            quality: { read: true },
          },
        }),
        'spec.quality.read',
      ),
    ).toBe(false);
  });

  it('uses the exact Core historical authority leaves for SK-A writes', () => {
    const coreAuthority = response({
      ideation: { quality: { assess: true } },
      refinement: {
        quality: { assess: true },
        research_decisions: { append: true },
        entity: { edit_fields: false },
      },
      spec: { entity: { edit_fields: true } },
    });
    expect(hasEffectivePermission(coreAuthority, 'ideation.quality.assess')).toBe(
      true,
    );
    expect(
      hasEffectivePermission(coreAuthority, 'refinement.quality.assess'),
    ).toBe(true);
    expect(
      hasEffectivePermission(
        coreAuthority,
        'refinement.research_decisions.append',
      ),
    ).toBe(true);

    const staleUiAuthorityOnly = response({
      ideation: {
        quality: { assess: true },
        entity: { evaluate: true },
      },
      refinement: {
        quality: { assess: true },
        research_decisions: { append: true },
        entity: { edit_fields: true },
      },
      spec: { entity: { edit_fields: false } },
    });
    expect(
      hasEffectivePermission(staleUiAuthorityOnly, 'ideation.quality.assess'),
    ).toBe(false);
    expect(
      hasEffectivePermission(staleUiAuthorityOnly, 'refinement.quality.assess'),
    ).toBe(false);
    expect(
      hasEffectivePermission(
        staleUiAuthorityOnly,
        'refinement.research_decisions.append',
      ),
    ).toBe(false);
  });

  it('denies malformed truthy values and review-required lineages', () => {
    expect(
      hasEffectivePermission(
        response({ board: { read: 'true' } }),
        'board.read',
      ),
    ).toBe(false);
    expect(
      hasEffectivePermission(
        response({
          spec: {
            entity: { read: true },
            quality: { read: true },
          },
        }, true),
        'spec.quality.read',
      ),
    ).toBe(false);
  });

  it('drops board A immediately while board B is pending, failed, or cleared', async () => {
    let rejectBoardB!: (reason?: unknown) => void;
    const boardBPending = new Promise<PermissionsResponse>((_resolve, reject) => {
      rejectBoardB = reject;
    });
    getMyPermissionsMock.mockImplementation((boardId) => {
      if (boardId === 'switch-board-a') {
        return Promise.resolve(
          response(
            {
              spec: {
                entity: { read: true },
                quality: { read: true },
              },
            },
            false,
            boardId,
          ),
        );
      }
      return boardBPending;
    });

    const { result, rerender } = renderHook(
      ({ boardId }: { boardId: string | null }) => usePermissions(boardId),
      { initialProps: { boardId: 'switch-board-a' as string | null } },
    );
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
      expect(result.current.has('spec.quality.read')).toBe(true);
    });

    rerender({ boardId: 'switch-board-b' });
    expect(result.current.isLoading).toBe(true);
    expect(result.current.preset).toBeNull();
    expect(result.current.has('spec.quality.read')).toBe(false);

    await act(async () => {
      rejectBoardB(new Error('board B unavailable'));
      await boardBPending.catch(() => undefined);
    });
    await waitFor(() => expect(result.current.error?.message).toBe(
      'board B unavailable',
    ));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.has('spec.quality.read')).toBe(false);

    rerender({ boardId: null });
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.preset).toBeNull();
    expect(result.current.has('spec.quality.read')).toBe(false);
  });
});
