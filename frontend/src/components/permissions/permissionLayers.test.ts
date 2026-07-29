import { describe, expect, it } from 'vitest';

import {
  applyBoardCeiling,
  applyPermissionDelta,
  boardCeilingDelta,
  permissionDelta,
} from './permissionLayers';

const base = {
  board: {
    read: true,
    update: true,
  },
  ideation: {
    quality: {
      read: true,
      assess: true,
    },
  },
  refinement: {
    quality: {
      read: true,
      assess: true,
    },
    research_decisions: {
      read: true,
      append: true,
    },
  },
  spec: {
    quality: {
      read: true,
      assess: true,
    },
    checklist: {
      read: true,
      execute: true,
    },
  },
};

describe('permission layers', () => {
  it('round-trips a direct agent edit as a sparse delta', () => {
    const desired = structuredClone(base);
    desired.board.update = false;

    const delta = permissionDelta(base, desired);

    expect(delta).toEqual({ board: { update: false } });
    expect(applyPermissionDelta(base, delta)).toEqual(desired);
  });

  it('fails closed for malformed direct deltas and board ceilings', () => {
    expect(applyPermissionDelta(base, { board: { read: 'yes' } })).toEqual({
      board: { read: false, update: false },
      ideation: { quality: { read: false, assess: false } },
      refinement: {
        quality: { read: false, assess: false },
        research_decisions: { read: false, append: false },
      },
      spec: {
        quality: { read: false, assess: false },
        checklist: { read: false, execute: false },
      },
    });
    expect(applyBoardCeiling(base, ['not-a-mapping'])).toEqual(
      applyPermissionDelta(base, { board: { read: 'yes' } }),
    );
  });

  it('keeps introduced permissions explicitly admitted in a restrictive ceiling', () => {
    const desired = structuredClone(base);
    desired.board.update = false;

    const ceiling = boardCeilingDelta(base, desired);

    expect(ceiling).toMatchObject({
      board: { update: false },
      ideation: { quality: { read: true, assess: true } },
      refinement: {
        quality: { read: true, assess: true },
        research_decisions: { read: true, append: true },
      },
      spec: {
        quality: { read: true, assess: true },
        checklist: { read: true, execute: true },
      },
    });
    expect(applyBoardCeiling(base, ceiling)).toEqual(desired);
  });

  it('uses null for an unrestricted ceiling and denies omitted introduced leaves', () => {
    expect(boardCeilingDelta(base, structuredClone(base))).toBeNull();

    const effective = applyBoardCeiling(base, { board: { update: false } });
    expect(effective).toMatchObject({
      board: { update: false, read: true },
      ideation: { quality: { read: false } },
      spec: { checklist: { execute: false } },
    });
  });
});
