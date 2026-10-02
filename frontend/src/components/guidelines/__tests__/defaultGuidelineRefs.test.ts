import { describe, expect, it } from 'vitest';

import type { DefaultGuidelineCandidate } from '@/types';

import {
  canonicalDefaultGuidelineRef,
  currentDefaultGuidelineRefs,
  defaultGuidelineRefFromCandidate,
} from '../defaultGuidelineRefs';

const digest = (character: string) => character.repeat(64);

const candidate: DefaultGuidelineCandidate = {
  guideline_id: 'guideline-1',
  title: 'Guideline one',
  scope: 'global',
  head_revision: {
    revision_id: 'revision-2',
    revision_number: 2,
    semantic_version: '2.0.0',
    revision_digest: digest('b'),
  },
  default_revision: {
    revision_id: 'revision-1',
    revision_number: 1,
    semantic_version: '1.0.0',
    revision_digest: digest('a'),
  },
  retired: false,
  eligible: true,
  eligibility_reason: null,
  is_default: true,
  priority: 3,
};

describe('default guideline exact revision pins', () => {
  it('rejects old aliases instead of silently stripping them', () => {
    const ref = { guideline_id: candidate.guideline_id, priority: 3, ...candidate.head_revision, guideline_version: 2 };
    expect(() => canonicalDefaultGuidelineRef(ref)).toThrow('default_guideline_ref_invalid');
  });

  it('preserves default_revision and never silently promotes it to head', () => {
    expect(currentDefaultGuidelineRefs([candidate])).toEqual([{
      guideline_id: 'guideline-1',
      priority: 3,
      ...candidate.default_revision,
    }]);
  });

  it('uses head_revision only for a newly selected default', () => {
    expect(
      defaultGuidelineRefFromCandidate(
        { ...candidate, is_default: false, default_revision: null },
        'head',
        4,
      ),
    ).toEqual({
      guideline_id: 'guideline-1',
      priority: 4,
      ...candidate.head_revision,
    });
  });

  it('fails closed for retired candidates and malformed pins', () => {
    expect(() =>
      defaultGuidelineRefFromCandidate(
        { ...candidate, retired: true, eligible: false },
        'head',
        1,
      ),
    ).toThrow('default_guideline_candidate_ineligible');
    expect(() =>
      canonicalDefaultGuidelineRef({
        guideline_id: 'guideline-1',
        priority: 0,
        revision_id: 'revision-1',
        revision_number: 1,
        semantic_version: '1.0.0',
        revision_digest: 'not-a-digest',
      }),
    ).toThrow('default_guideline_revision_pin_invalid');
  });
});
