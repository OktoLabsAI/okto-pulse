import { describe, expect, it } from 'vitest';

import { resolveSpecSemanticAnchor } from '../specSemanticAnchors';

const digest = 'a'.repeat(64);

describe('resolveSpecSemanticAnchor', () => {
  it('authorizes whole-spec and visible Spec fields', () => {
    expect(resolveSpecSemanticAnchor({
      anchor_type: 'whole_artifact',
      anchor_ref: null,
      excerpt_hash: null,
    })).toEqual({ state: 'available', navigationTarget: 'spec:details:root' });

    expect(resolveSpecSemanticAnchor({
      anchor_type: 'field',
      anchor_ref: 'title',
      excerpt_hash: digest,
    })).toEqual({
      state: 'available',
      navigationTarget: 'spec:field:title',
    });
  });

  it('authorizes only structured children loaded by the Spec modal', () => {
    const anchorTexts = { fr_visible: 'The visible requirement.' };

    expect(resolveSpecSemanticAnchor({
      anchor_type: 'structured_child',
      anchor_ref: 'fr_visible',
      excerpt_hash: digest,
    }, anchorTexts)).toEqual({
      state: 'available',
      navigationTarget: 'spec:requirement:fr_visible',
    });
    expect(resolveSpecSemanticAnchor({
      anchor_type: 'structured_child',
      anchor_ref: 'opaque-child-id',
      excerpt_hash: digest,
    }, anchorTexts)).toEqual({ state: 'inaccessible' });
  });

  it('keeps unknown fields and Q&A anchors fail-closed', () => {
    expect(resolveSpecSemanticAnchor({
      anchor_type: 'field',
      anchor_ref: 'internal_secret',
      excerpt_hash: digest,
    })).toEqual({ state: 'inaccessible' });
    expect(resolveSpecSemanticAnchor({
      anchor_type: 'qa',
      anchor_ref: 'question-id',
      excerpt_hash: digest,
    })).toEqual({ state: 'inaccessible' });
  });
});
