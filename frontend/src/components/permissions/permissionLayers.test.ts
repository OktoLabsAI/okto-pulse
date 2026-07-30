import { describe, expect, it } from 'vitest';

import {
  applyBoardCeiling,
  applyPermissionDelta,
  boardCeilingDelta,
  composePermissionIntroductionManifests,
  INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES,
  INTRODUCED_PERMISSION_LEAVES,
  PERMISSION_INTRODUCTION_MANIFESTS,
  permissionDelta,
  SKA_PERMISSION_INTRODUCTION_V1_LEAVES,
  SKB_PERMISSION_INTRODUCTION_V1_LEAVES,
} from './permissionLayers';
import type { PermissionIntroductionManifest } from './permissionLayers';

function introductionManifest(
  version: string,
  leaves: readonly string[],
  historicalAuthorities: Record<string, string>,
): PermissionIntroductionManifest {
  return { version, leaves, historicalAuthorities };
}

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

  it.each([
    {
      caseName: 'an empty version',
      manifests: [introductionManifest(
        ' ',
        ['new.read'],
        { 'new.read': 'legacy.read' },
      )],
    },
    {
      caseName: 'a duplicate version',
      manifests: [
        introductionManifest(
          'v1',
          ['new.read'],
          { 'new.read': 'legacy.read' },
        ),
        introductionManifest(
          ' v1 ',
          ['new.write'],
          { 'new.write': 'legacy.write' },
        ),
      ],
    },
  ])('rejects $caseName', ({ manifests }) => {
    expect(() => composePermissionIntroductionManifests(manifests)).toThrow(
      'permission_introduction_manifest_version_invalid',
    );
  });

  it.each([
    {
      caseName: 'an empty leaf',
      manifests: [introductionManifest(
        'v1',
        [' '],
        { ' ': 'legacy.read' },
      )],
    },
    {
      caseName: 'a duplicate leaf',
      manifests: [
        introductionManifest(
          'v1',
          ['new.read'],
          { 'new.read': 'legacy.read' },
        ),
        introductionManifest(
          'v2',
          ['new.read'],
          { 'new.read': 'legacy.write' },
        ),
      ],
    },
  ])('rejects $caseName', ({ manifests }) => {
    expect(() => composePermissionIntroductionManifests(manifests)).toThrow(
      'permission_introduction_leaf_collision',
    );
  });

  it.each([
    {
      caseName: 'a missing authority key',
      historicalAuthorities: {} as Record<string, string>,
    },
    {
      caseName: 'an extra authority key',
      historicalAuthorities: {
        'new.read': 'legacy.read',
        'new.write': 'legacy.write',
      } as Record<string, string>,
    },
  ])('rejects $caseName', ({ historicalAuthorities }) => {
    expect(() => composePermissionIntroductionManifests([{
      version: 'v1',
      leaves: ['new.read'],
      historicalAuthorities,
    }])).toThrow('permission_introduction_authority_set_mismatch');
  });

  it.each([
    {
      caseName: 'an empty authority',
      authority: ' ',
    },
    {
      caseName: 'a self authority',
      authority: 'new.read',
    },
  ])('rejects $caseName', ({ authority }) => {
    expect(() => composePermissionIntroductionManifests([{
      version: 'v1',
      leaves: ['new.read'],
      historicalAuthorities: { 'new.read': authority },
    }])).toThrow('permission_introduction_authority_invalid');
  });

  it('rejects introduced-to-introduced authority chains', () => {
    expect(() => composePermissionIntroductionManifests([
      {
        version: 'v1',
        leaves: ['new.read'],
        historicalAuthorities: { 'new.read': 'legacy.read' },
      },
      {
        version: 'v2',
        leaves: ['new.write'],
        historicalAuthorities: { 'new.write': 'new.read' },
      },
    ])).toThrow('permission_introduction_authority_invalid');
  });

  it('composes SK-A and SK-B manifests in deterministic fail-closed order', () => {
    expect(PERMISSION_INTRODUCTION_MANIFESTS.map(({ version }) => version))
      .toEqual(['SK-A/v1', 'SK-B/v1']);
    expect(INTRODUCED_PERMISSION_LEAVES).toEqual([
      ...SKA_PERMISSION_INTRODUCTION_V1_LEAVES,
      ...SKB_PERMISSION_INTRODUCTION_V1_LEAVES,
    ]);
    const recomposed = composePermissionIntroductionManifests(
      PERMISSION_INTRODUCTION_MANIFESTS,
    );
    expect(recomposed.leaves).toEqual(INTRODUCED_PERMISSION_LEAVES);
    expect(recomposed.historicalAuthorities).toEqual(
      INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES,
    );
    expect(Object.keys(recomposed.historicalAuthorities)).toEqual(
      INTRODUCED_PERMISSION_LEAVES,
    );
    expect(new Set(INTRODUCED_PERMISSION_LEAVES).size).toBe(
      INTRODUCED_PERMISSION_LEAVES.length,
    );

    const guidelinesBase = {
      guidelines: {
        read: true,
        create: true,
        edit: true,
        delete: true,
        link: true,
        revisions: { read: true, create: true, retire: true },
        rules: { author_blocking: true },
        impact: { preview: true },
        adoption: { manage: true },
        compliance: { read: true, evaluate: true },
        waiver: {
          read: true,
          request: true,
          review: true,
          revoke: true,
          revalidate: true,
        },
      },
    };
    const desired = structuredClone(guidelinesBase);
    desired.guidelines.delete = false;
    const ceiling = boardCeilingDelta(guidelinesBase, desired);

    expect(ceiling).toMatchObject({
      guidelines: {
        delete: false,
        revisions: { read: true, create: true, retire: true },
        rules: { author_blocking: true },
        impact: { preview: true },
        adoption: { manage: true },
        compliance: { read: true, evaluate: true },
        waiver: {
          read: true,
          request: true,
          review: true,
          revoke: true,
          revalidate: true,
        },
      },
    });
    expect(applyBoardCeiling(guidelinesBase, ceiling)).toEqual(desired);

    expect(
      applyBoardCeiling(guidelinesBase, {
        guidelines: { read: true, delete: false },
      }),
    ).toMatchObject({
      guidelines: {
        read: true,
        delete: false,
        revisions: { read: false, create: false, retire: false },
        compliance: { read: false, evaluate: false },
        waiver: { request: false, revoke: false },
      },
    });
  });
});
