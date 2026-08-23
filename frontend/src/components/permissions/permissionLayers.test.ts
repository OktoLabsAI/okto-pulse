import { describe, expect, it } from 'vitest';

import {
  applyBoardCeiling,
  applyPermissionDelta,
  boardCeilingDelta,
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1,
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1_LEAVES,
  CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES,
  composePermissionIntroductionManifests,
  INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES,
  INTRODUCED_PERMISSION_LEAVES,
  isIntroducedPermissionLeaf,
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

  it('composes the versioned manifests in deterministic fail-closed order', () => {
    expect(PERMISSION_INTRODUCTION_MANIFESTS.map(({ version }) => version))
      .toEqual([
        'SK-A/v1',
        'SK-B3/v1',
        'CODE-EVIDENCE-LEGACY-CLASSIFICATION/v1',
      ]);
    expect(INTRODUCED_PERMISSION_LEAVES).toEqual([
      ...SKA_PERMISSION_INTRODUCTION_V1_LEAVES,
      ...SKB_PERMISSION_INTRODUCTION_V1_LEAVES,
      ...CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1_LEAVES,
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
        metrics: { author: true },
        impact: { preview: true },
        adoption: { manage: true },
        assessments: { read: true, record: true },
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
        metrics: { author: true },
        impact: { preview: true },
        adoption: { manage: true },
        assessments: { read: true, record: true },
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
        assessments: { read: false, record: false },
        waiver: { request: false, revoke: false },
      },
    });
  });

  it.each([
    'agent.entity.create',
    'board.admin.delete',
    'board.share.manage',
    'permission_preset.entity.update',
    'default_board_config.entity.update',
    'design_system.entity.import',
    'runtime.settings.write',
    'metrics.read',
    'amendment.entity.approve',
    'kg.operations.rebuild',
    'ideation.move.review_to_approved',
    'test_scenario.move.ready_to_automated',
    'test_scenario.interact_in.passed',
    'sprint.tasks.assign',
    'card.interact_in.rejected',
    'card.move.rejected_to_in_progress',
    'code_traceability.investigation.read',
    'code_traceability.investigation.revoke',
    'code_traceability.evidence.read',
    'code_traceability.target.read',
    'code_traceability.overlap.read',
  ])('recognizes post-SK-B introduced leaf %s', (leaf) => {
    expect(isIntroducedPermissionLeaf(leaf)).toBe(true);
  });

  it('pins Core CODE-TRACEABILITY/v1 and keeps the namespace future fail-closed', () => {
    expect(CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES).toHaveLength(22);
    for (const leaf of CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES) {
      expect(isIntroducedPermissionLeaf(leaf)).toBe(true);
    }
    expect(isIntroducedPermissionLeaf('code_traceability.future_capability.read'))
      .toBe(true);
  });

  it('keeps legacy Evidence classification in its own historical-authority manifest', () => {
    expect(CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES).toHaveLength(22);
    expect(CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES)
      .not.toContain('code_traceability.evidence.classify_legacy');
    expect(CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1)
      .toEqual({
        version: 'CODE-EVIDENCE-LEGACY-CLASSIFICATION/v1',
        leaves: ['code_traceability.evidence.classify_legacy'],
        historicalAuthorities: {
          'code_traceability.evidence.classify_legacy': 'spec.entity.edit_fields',
        },
      });
    expect(
      INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES[
        'code_traceability.evidence.classify_legacy'
      ],
    ).toBe('spec.entity.edit_fields');
  });

  it.each([
    'board.read',
    'card.entity.edit_fields',
    'sprint.entity.assign',
    'guidelines.read',
    'story.move.draft_to_ready',
    'sprint.move.active_to_review',
    'card.move.in_progress_to_validation',
  ])('does not reclassify historical leaf %s', (leaf) => {
    expect(isIntroducedPermissionLeaf(leaf)).toBe(false);
  });
});
