import { describe, expect, it } from 'vitest';

import { resolveCardSemanticAnchor } from '../cardSemanticAnchors';

const digest = 'a'.repeat(64);

describe('resolveCardSemanticAnchor', () => {
  it('authorizes whole-card and Details fields with stable navigation targets', () => {
    expect(resolveCardSemanticAnchor({
      anchor_type: 'whole_artifact',
      anchor_ref: null,
      excerpt_hash: null,
    })).toEqual({ state: 'available', navigationTarget: 'card:details:root' });

    expect(resolveCardSemanticAnchor({
      anchor_type: 'field',
      anchor_ref: 'description',
      excerpt_hash: digest,
    })).toEqual({
      state: 'available',
      navigationTarget: 'card:details:description',
    });
  });

  it('keeps opaque or non-visible anchors fail-closed', () => {
    expect(resolveCardSemanticAnchor({
      anchor_type: 'structured_child',
      anchor_ref: 'opaque-child-id',
      excerpt_hash: digest,
    })).toEqual({ state: 'inaccessible' });
    expect(resolveCardSemanticAnchor({
      anchor_type: 'field',
      anchor_ref: 'internal_secret',
      excerpt_hash: digest,
    })).toEqual({ state: 'inaccessible' });
  });
});

