import { describe, expect, it } from 'vitest';

import {
  INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES,
  PERMISSION_INTRODUCTION_MANIFESTS,
  SKB_PERMISSION_INTRODUCTION_V1,
  SKB_PERMISSION_INTRODUCTION_V1_LEAVES,
} from '@/components/permissions/permissionLayers';
import { hasEffectivePermission } from '@/hooks/usePermissions';
import type { PermissionsResponse } from '@/services/permissions-api';

const LEAVES = [
  'guidelines.revisions.read',
  'guidelines.revisions.create',
  'guidelines.revisions.retire',
  'guidelines.rules.author_blocking',
  'guidelines.impact.preview',
  'guidelines.adoption.manage',
  'guidelines.compliance.read',
  'guidelines.compliance.evaluate',
  'guidelines.waiver.read',
  'guidelines.waiver.request',
  'guidelines.waiver.review',
  'guidelines.waiver.revoke',
  'guidelines.waiver.revalidate',
] as const;

const AUTHORITIES = {
  'guidelines.revisions.read': 'guidelines.read',
  'guidelines.revisions.create': 'spec.entity.edit_fields',
  'guidelines.revisions.retire': 'guidelines.delete',
  'guidelines.rules.author_blocking': 'spec.entity.edit_fields',
  'guidelines.impact.preview': 'guidelines.read',
  'guidelines.adoption.manage': 'spec.entity.edit_fields',
  'guidelines.compliance.read': 'guidelines.read',
  'guidelines.compliance.evaluate': 'guidelines.read',
  'guidelines.waiver.read': 'guidelines.read',
  'guidelines.waiver.request': 'guidelines.read',
  'guidelines.waiver.review': 'spec.validation.submit',
  'guidelines.waiver.revoke': 'guidelines.delete',
  'guidelines.waiver.revalidate': 'spec.validation.submit',
} as const;

function response(
  flags: Record<string, unknown>,
  ownerReviewRequired = false,
): PermissionsResponse {
  return {
    board_id: 'board-skb',
    preset_name: 'Custom',
    flags,
    owner_review_required: ownerReviewRequired,
    review_reason: ownerReviewRequired ? 'preset_lineage_cycle' : null,
  };
}

function nestedFlags(paths: readonly string[]): Record<string, unknown> {
  const document: Record<string, unknown> = {};
  for (const path of paths) {
    let current = document;
    const parts = path.split('.');
    for (const part of parts.slice(0, -1)) {
      if (
        !current[part]
        || typeof current[part] !== 'object'
        || Array.isArray(current[part])
      ) {
        current[part] = {};
      }
      current = current[part] as Record<string, unknown>;
    }
    current[parts.at(-1)!] = true;
  }
  return document;
}

describe('usePermissions SK-B/v1 fail-closed introduction', () => {
  it('shares the exact ordered manifest and historical-authority lineage', () => {
    expect(PERMISSION_INTRODUCTION_MANIFESTS.map(({ version }) => version))
      .toEqual(['SK-A/v1', 'SK-B/v1']);
    expect(SKB_PERMISSION_INTRODUCTION_V1.version).toBe('SK-B/v1');
    expect(SKB_PERMISSION_INTRODUCTION_V1_LEAVES).toEqual(LEAVES);
    expect(SKB_PERMISSION_INTRODUCTION_V1_LEAVES).toHaveLength(13);
    expect(SKB_PERMISSION_INTRODUCTION_V1.historicalAuthorities)
      .toEqual(AUTHORITIES);
    for (const [leaf, authority] of Object.entries(AUTHORITIES)) {
      expect(INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES[leaf])
        .toBe(authority);
    }
  });

  it('denies every SK-B leaf until both explicit grants are present', () => {
    for (const leaf of LEAVES) {
      const authority = AUTHORITIES[leaf];
      expect(hasEffectivePermission(null, leaf)).toBe(false);
      expect(hasEffectivePermission(response({}), leaf)).toBe(false);
      expect(
        hasEffectivePermission(response(nestedFlags([leaf])), leaf),
      ).toBe(false);
      expect(
        hasEffectivePermission(response(nestedFlags([authority])), leaf),
      ).toBe(false);
      expect(
        hasEffectivePermission(
          response(nestedFlags([leaf, authority])),
          leaf,
        ),
      ).toBe(true);
    }
  });

  it('preserves explicit custom denies and owner-review fail-closed state', () => {
    const granted = nestedFlags([
      'guidelines.compliance.evaluate',
      'guidelines.read',
    ]);
    (
      (granted.guidelines as Record<string, unknown>)
        .compliance as Record<string, unknown>
    ).evaluate = false;
    expect(
      hasEffectivePermission(
        response(granted),
        'guidelines.compliance.evaluate',
      ),
    ).toBe(false);

    expect(
      hasEffectivePermission(
        response(
          nestedFlags([
            'guidelines.compliance.evaluate',
            'guidelines.read',
          ]),
          true,
        ),
        'guidelines.compliance.evaluate',
      ),
    ).toBe(false);
  });
});
