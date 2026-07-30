import { describe, expect, it } from 'vitest';

import type {
  BoardGuidelineEntry,
} from '@/types';
import type {
  GuidelineAdoptionResponse,
  GuidelineImpactReceipt,
  GuidelineRevisionAuthorityResponse,
} from '@/types/policy-governance';

import {
  countGuidelineImpactItems,
  isCompleteBoardGuidelineBindingAuthority,
  isGuidelineAdoptionResponseForReceipt,
  isGuidelineImpactConflict,
  isGuidelineImpactReceiptForPreview,
  isGuidelineRevisionAuthorityForTarget,
  latestGuidelineRevisionTargetFromAuthority,
  validatedGuidelineImpactPage,
} from '../guidelineImpactModel';
import { PolicyGovernanceApiError } from '@/services/policy-governance-api';

const digest = (character: string) => character.repeat(64);

function receipt(): GuidelineImpactReceipt {
  return {
    impact_receipt_id: 'impact-1',
    board_id: 'board-1',
    guideline_id: 'guideline-1',
    binding_id: 'binding-1',
    to_revision_id: 'revision-2',
    to_revision_number: 2,
    to_semantic_version: '2.0.0',
    to_revision_digest: digest('a'),
    expected_head_revision: 2,
    expected_binding_revision: 1,
    expected_binding_state: 'active',
    binding_digest: digest('b'),
    binding_head_digest_before: digest('c'),
    binding_head_digest_after: digest('d'),
    policy_set_digest_before: digest('e'),
    policy_set_digest_after: digest('f'),
    artifact_snapshot_digest: digest('1'),
    waiver_snapshot_digest: digest('2'),
    proposed_priority: 4,
    proposed_default_enforcement: 'blocking',
    affected_entity_types: ['spec'],
    items: [
      {
        impact_item_id: 'item-binding',
        item_kind: 'binding',
        entity_type: 'board',
        entity_id: 'board-1',
        related_id: 'binding-1',
        entity_version: 1,
        details_digest: digest('3'),
      },
      {
        impact_item_id: 'item-waiver',
        item_kind: 'waiver',
        entity_type: 'spec',
        entity_id: 'spec-1',
        related_id: 'waiver-1',
        entity_version: 7,
        details_digest: digest('4'),
      },
    ],
    added_rule_ids: ['rule-2'],
    changed_rule_ids: ['rule-1'],
    removed_rule_ids: [],
    requested_by: 'agent-1',
    created_at: '2026-07-29T00:00:00Z',
    impact_digest: digest('5'),
    from_revision_id: 'revision-1',
    from_semantic_version: '1.0.0',
    from_revision_digest: digest('6'),
    requires_explicit_adoption: true,
  };
}

const expectation = {
  boardId: 'board-1',
  guidelineId: 'guideline-1',
  revisionId: 'revision-2',
  revisionNumber: 2,
  revisionDigest: digest('a'),
  semanticVersion: '2.0.0',
  priority: 4,
  enforcement: 'blocking' as const,
  adoptedBinding: {
    bindingId: 'binding-1',
    bindingRevision: 1,
    bindingState: 'active' as const,
    revisionId: 'revision-1',
    semanticVersion: '1.0.0',
    revisionDigest: digest('6'),
  },
};

function authority(): GuidelineRevisionAuthorityResponse {
  return {
    guideline: {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'global',
      created_at: '2026-07-29T00:00:00Z',
      context_scope: 'all',
    },
    revision: {
      revision_id: 'revision-2',
      guideline_id: 'guideline-1',
      revision_number: 2,
      semantic_version: '2.0.0',
      title: 'Secure specifications',
      content: 'Context',
      content_digest: digest('7'),
      rules: [{
        rule_id: 'rule-1',
        code: 'evidence_required',
        title: 'Evidence is required',
        description: 'Require evidence',
        target_entity_types: ['spec'],
        predicates: [{
          predicate_code: 'field_present',
          parameters: [['field', 'evidence']],
        }],
        enforcement: 'blocking',
        operator: 'all',
        waivable: true,
        policy_class: 'quality',
      }],
      created_by: 'owner-1',
      created_at: '2026-07-29T00:00:00Z',
      tags: [],
    },
    head: {
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      revision_number: 2,
      semantic_version: '2.0.0',
      head_revision: 2,
      updated_at: '2026-07-29T00:00:00Z',
    },
  };
}

describe('guidelineImpactModel closed contracts', () => {
  it('accepts an exact immutable receipt and derives exhaustive counts', () => {
    const value = receipt();

    expect(
      isGuidelineImpactReceiptForPreview(value, expectation),
    ).toBe(true);
    expect(countGuidelineImpactItems(value.items)).toEqual({
      binding: 1,
      target: 0,
      artifact: 0,
      waiver: 1,
    });
  });

  it('accepts only absent or explicitly unlinked authority for an unbound row', () => {
    const value = receipt();
    const unboundExpectation = { ...expectation, adoptedBinding: null };
    value.expected_binding_revision = null;
    value.expected_binding_state = null;
    value.from_revision_id = null;
    value.from_semantic_version = null;
    value.from_revision_digest = null;
    expect(
      isGuidelineImpactReceiptForPreview(value, unboundExpectation),
    ).toBe(true);

    value.expected_binding_revision = 2;
    value.expected_binding_state = 'unlinked';
    value.from_revision_id = 'revision-1';
    value.from_semantic_version = '1.0.0';
    value.from_revision_digest = digest('6');
    expect(
      isGuidelineImpactReceiptForPreview(value, unboundExpectation),
    ).toBe(true);

    value.expected_binding_state = 'active';
    expect(
      isGuidelineImpactReceiptForPreview(value, unboundExpectation),
    ).toBe(false);
  });

  it.each([
    ['mismatched target', (value: GuidelineImpactReceipt) => {
      value.to_revision_id = 'revision-other';
    }],
    ['implicit adoption', (value: GuidelineImpactReceipt) => {
      value.requires_explicit_adoption = false;
    }],
    ['duplicate item identity', (value: GuidelineImpactReceipt) => {
      value.items[1].impact_item_id = value.items[0].impact_item_id;
    }],
    ['overlapping rule delta', (value: GuidelineImpactReceipt) => {
      value.removed_rule_ids = ['rule-1'];
    }],
    ['unsupported item kind', (value: GuidelineImpactReceipt) => {
      value.items[0].item_kind = 'negative' as never;
    }],
    ['waiver without governed identity', (value: GuidelineImpactReceipt) => {
      value.items[1].related_id = null;
    }],
    ['advanced head fence', (value: GuidelineImpactReceipt) => {
      value.expected_head_revision = 3;
    }],
    ['mismatched revision digest', (value: GuidelineImpactReceipt) => {
      value.to_revision_digest = digest('f');
    }],
    ['mismatched active binding identity', (value: GuidelineImpactReceipt) => {
      value.binding_id = 'binding-other';
    }],
    ['mismatched active binding revision', (value: GuidelineImpactReceipt) => {
      value.expected_binding_revision = 2;
    }],
    ['mismatched adopted revision', (value: GuidelineImpactReceipt) => {
      value.from_revision_id = 'revision-other';
    }],
  ])('rejects %s fail-closed', (_label, mutate) => {
    const value = receipt();
    mutate(value);

    expect(
      isGuidelineImpactReceiptForPreview(value, expectation),
    ).toBe(false);
  });

  it('requires context_scope all and closed executable rule targets', () => {
    const value = authority();
    expect(
      isGuidelineRevisionAuthorityForTarget(value, {
        guidelineId: 'guideline-1',
        revisionId: 'revision-2',
        semanticVersion: '2.0.0',
      }),
    ).toBe(true);

    value.guideline.context_scope = 'spec' as never;
    expect(
      isGuidelineRevisionAuthorityForTarget(value, {
        guidelineId: 'guideline-1',
        revisionId: 'revision-2',
        semanticVersion: '2.0.0',
      }),
    ).toBe(false);
  });

  it('discovers an inline latest head from a validated historical authority', () => {
    const value = authority();
    value.revision.revision_id = 'revision-1';
    value.revision.revision_number = 1;
    value.revision.semantic_version = '1.0.0';

    expect(latestGuidelineRevisionTargetFromAuthority(value, {
      guidelineId: 'guideline-1',
      requestedRevisionId: 'revision-1',
    })).toEqual({
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    });

    value.guideline.context_scope = 'spec' as never;
    expect(latestGuidelineRevisionTargetFromAuthority(value, {
      guidelineId: 'guideline-1',
      requestedRevisionId: 'revision-1',
    })).toBeNull();
  });

  it('requires complete exact binding authority before policy mutation', () => {
    const entry: BoardGuidelineEntry = {
      id: 'entry-1',
      binding_id: 'binding-1',
      binding_revision: 1,
      binding_state: 'active',
      default_enforcement: 'advisory',
      source_kind: 'native',
      priority: 0,
      scope: 'inline',
      guideline: {
        id: 'guideline-1',
        title: 'Inline',
        content: 'Context',
        tags: [],
        scope: 'inline',
        board_id: 'board-1',
        owner_id: 'owner-1',
        semantic_version: '1.0.0',
        revision_id: 'revision-1',
        revision_digest: digest('a'),
        created_at: '2026-07-29T00:00:00Z',
        updated_at: '2026-07-29T00:00:00Z',
      },
    };
    expect(isCompleteBoardGuidelineBindingAuthority(entry)).toBe(true);
    delete entry.guideline.revision_digest;
    expect(isCompleteBoardGuidelineBindingAuthority(entry)).toBe(false);
  });

  it('normalizes the REST terminal null cursor and rejects audit gaps', () => {
    expect(validatedGuidelineImpactPage({
      items: [{
        impact_item_id: 'item-1',
        item_kind: 'target',
        entity_type: 'spec',
        entity_id: 'spec-1',
        related_id: 'rule-1',
        entity_version: 2,
        details_digest: digest('8'),
      }],
      limit: 25,
      has_more: false,
      next_cursor: null,
    })).toEqual({
      items: [expect.objectContaining({ impact_item_id: 'item-1' })],
      limit: 25,
      has_more: false,
    });

    expect(() => validatedGuidelineImpactPage({
      items: [{
        impact_item_id: 'item-waiver',
        item_kind: 'waiver',
        entity_type: 'spec',
        entity_id: 'spec-1',
        details_digest: digest('9'),
      }],
      limit: 25,
      has_more: false,
      next_cursor: null,
    })).toThrow(/malformed cursor page/i);

    expect(() => validatedGuidelineImpactPage({
      items: [],
      limit: 25,
      has_more: true,
      next_cursor: 'opaque',
    })).toThrow(/malformed cursor page/i);
  });

  it('accepts adoption only when the binding and receipt match exactly', () => {
    const expectedReceipt = receipt();
    const response: GuidelineAdoptionResponse = {
      binding: {
        binding_id: 'binding-1',
        board_id: 'board-1',
        guideline_id: 'guideline-1',
        revision_id: 'revision-2',
        semantic_version: '2.0.0',
        revision_digest: digest('a'),
        priority: 4,
        binding_revision: 2,
        adopted_by: 'agent-1',
        adopted_at: '2026-07-29T00:00:01Z',
        default_enforcement: 'blocking',
        state: 'active',
        source_kind: 'native',
      },
      receipt: expectedReceipt,
    };
    expect(
      isGuidelineAdoptionResponseForReceipt(response, expectedReceipt),
    ).toBe(true);
    response.binding.priority = 5;
    expect(
      isGuidelineAdoptionResponseForReceipt(response, expectedReceipt),
    ).toBe(false);
  });

  it('rejects adoption when any receipt echo or CAS binding relation drifts', () => {
    const expectedReceipt = receipt();
    const response: GuidelineAdoptionResponse = {
      binding: {
        binding_id: 'binding-1',
        board_id: 'board-1',
        guideline_id: 'guideline-1',
        revision_id: 'revision-2',
        semantic_version: '2.0.0',
        revision_digest: digest('a'),
        priority: 4,
        binding_revision: 2,
        adopted_by: 'agent-1',
        adopted_at: '2026-07-29T00:00:01Z',
        default_enforcement: 'blocking',
        state: 'active',
        source_kind: 'native',
      },
      receipt: {
        ...expectedReceipt,
        waiver_snapshot_digest: digest('9'),
      },
    };
    expect(
      isGuidelineAdoptionResponseForReceipt(response, expectedReceipt),
    ).toBe(false);

    response.receipt = expectedReceipt;
    response.binding.binding_revision = 3;
    expect(
      isGuidelineAdoptionResponseForReceipt(response, expectedReceipt),
    ).toBe(false);
  });

  it('treats semantic stale reason codes as conflicts during rolling upgrades', () => {
    expect(isGuidelineImpactConflict(new PolicyGovernanceApiError({
      message: 'stale',
      status: 400,
      code: 'validation_failed',
      details: { reason_code: 'guideline_impact_stale' },
    }))).toBe(true);
  });
});
