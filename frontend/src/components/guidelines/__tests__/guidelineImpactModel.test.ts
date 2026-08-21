import { describe, expect, it } from 'vitest';

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import type { BoardGuidelineEntry } from '@/types';
import type {
  GuidelineAdoptionResponse,
  GuidelineImpactPreviewResponse,
} from '@/types/policy-governance';

import {
  countGuidelineImpactItems,
  isCompleteBoardGuidelineBindingAuthority,
  isGuidelineAdoptionResponseForPreview,
  isGuidelineImpactConflict,
  isGuidelineImpactPreviewResponse,
  isGuidelineRevisionAuthorityForTarget,
  latestGuidelineRevisionTargetFromAuthority,
} from '../guidelineImpactModel';

function authority() {
  return {
    guideline: {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'global',
      created_at: '2026-07-30T10:00:00Z',
      context_scope: 'all',
    },
    revision: {
      revision_id: 'revision-2',
      guideline_id: 'guideline-1',
      revision_number: 2,
      semantic_version: '2.0.0',
      title: 'Delivery quality',
      content: 'Attach traceable evidence.',
      revision_digest: 'a'.repeat(64),
      metrics: [{
        metric_id: 'metric-1',
        code: 'evidence_strength',
        title: 'Evidence strength',
        description: 'How strongly evidence supports the proposal.',
        evaluation_rubric: '0 has no evidence; 100 is independently traceable.',
        target_entity_types: ['spec'],
        direction: 'minimum',
        default_threshold: 70,
      }],
      created_by: 'owner-1',
      created_at: '2026-07-30T11:00:00Z',
      parent_revision_id: 'revision-1',
      tags: ['delivery'],
    },
    head: {
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      revision_number: 2,
      semantic_version: '2.0.0',
      head_revision: 2,
      updated_at: '2026-07-30T11:00:00Z',
    },
  };
}

function preview(): GuidelineImpactPreviewResponse {
  return {
    receipt: {
      impact_receipt_id: 'preview-1',
      board_id: 'board-1',
      guideline_id: 'guideline-1',
      binding_id: 'binding-1',
      to_revision_id: 'revision-2',
      to_revision_number: 2,
      to_semantic_version: '2.0.0',
      to_revision_digest: 'a'.repeat(64),
      expected_head_revision: 2,
      expected_binding_revision: 4,
      expected_binding_state: 'active',
      binding_digest: 'e'.repeat(64),
      binding_head_digest_before: '1'.repeat(64),
      binding_head_digest_after: '2'.repeat(64),
      policy_set_digest_before: '3'.repeat(64),
      policy_set_digest_after: '4'.repeat(64),
      artifact_snapshot_digest: '5'.repeat(64),
      waiver_snapshot_digest: '6'.repeat(64),
      proposed_priority: 1,
      proposed_enforcement: 'blocking',
      proposed_minimum_confidence: 80,
      proposed_metric_threshold_overrides: { evidence_strength: 75 },
      affected_entity_types: ['spec'],
      items: [{
        impact_item_id: 'impact-1',
        item_kind: 'binding',
        entity_type: 'board',
        entity_id: 'board-1',
        related_id: 'binding-1',
        entity_version: 4,
        details_digest: 'c'.repeat(64),
      }, {
        impact_item_id: 'impact-2',
        item_kind: 'target',
        entity_type: 'spec',
        entity_id: 'spec-1',
        related_id: null,
        entity_version: 3,
        details_digest: 'd'.repeat(64),
      }],
      added_metric_ids: [],
      changed_metric_ids: ['metric-1'],
      removed_metric_ids: [],
      requested_by: 'owner-1',
      created_at: '2026-07-30T12:00:00Z',
      impact_digest: 'b'.repeat(64),
      from_revision_id: 'revision-2',
      from_semantic_version: '2.0.0',
      from_revision_digest: 'a'.repeat(64),
      requires_explicit_adoption: true,
    },
  };
}

function expectedPreview() {
  return {
    boardId: 'board-1',
    guidelineId: 'guideline-1',
    targetRevisionId: 'revision-2',
    targetSemanticVersion: '2.0.0',
    targetRevisionDigest: 'a'.repeat(64),
    proposedPriority: 1,
    proposedEnforcement: 'blocking' as const,
    proposedMinimumConfidence: 80,
    proposedMetricThresholdOverrides: { evidence_strength: 75 },
    bindingId: 'binding-1',
    bindingRevision: 4,
    fromRevisionId: 'revision-2',
    fromSemanticVersion: '2.0.0',
    fromRevisionDigest: 'a'.repeat(64),
  };
}

function adoption(): GuidelineAdoptionResponse {
  const impact = preview();
  return {
    binding: {
      binding_id: 'binding-1',
      board_id: 'board-1',
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      semantic_version: '2.0.0',
      revision_digest: 'a'.repeat(64),
      priority: 1,
      binding_revision: 5,
      adopted_by: 'owner-1',
      adopted_at: '2026-07-30T12:01:00Z',
      enforcement: 'blocking',
      minimum_confidence: 80,
      metric_threshold_overrides: { evidence_strength: 75 },
      configuration_digest: '7'.repeat(64),
      state: 'active',
      source_kind: 'native',
    },
    receipt: impact.receipt,
  };
}

function boardEntry(): BoardGuidelineEntry {
  return {
    id: 'entry-1',
    guideline: {
      id: 'guideline-1',
      title: 'Delivery quality',
      content: 'Attach traceable evidence.',
      tags: ['delivery'],
      scope: 'global',
      board_id: null,
      owner_id: 'owner-1',
      revision_id: 'revision-2',
      revision_digest: 'a'.repeat(64),
      semantic_version: '2.0.0',
      created_at: '2026-07-30T10:00:00Z',
      updated_at: '2026-07-30T11:00:00Z',
    },
    priority: 1,
    scope: 'global',
    binding_id: 'binding-1',
    binding_revision: 4,
    binding_state: 'active',
    enforcement: 'blocking',
    minimum_confidence: 80,
    metric_threshold_overrides: {
      evidence_strength: 75,
    },
    source_kind: 'native',
  };
}

describe('guidelineImpactModel semantic contracts', () => {
  it('accepts semantic revision authority and rejects legacy-only revisions', () => {
    expect(isGuidelineRevisionAuthorityForTarget(authority(), {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(true);

    const legacyOnly = structuredClone(authority());
    delete (legacyOnly.revision as Record<string, unknown>).metrics;
    (legacyOnly.revision as Record<string, unknown>).rules = [];
    expect(isGuidelineRevisionAuthorityForTarget(legacyOnly, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(false);
  });

  it('matches Core metric-code syntax, reservations, and casefold uniqueness', () => {
    const coreCompatible = structuredClone(authority());
    coreCompatible.revision.metrics[0].code = 'Evidence.v2:API-check';
    expect(isGuidelineRevisionAuthorityForTarget(coreCompatible, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(true);

    const reserved = structuredClone(authority());
    reserved.revision.metrics[0].code = 'Confidence';
    expect(isGuidelineRevisionAuthorityForTarget(reserved, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(false);

    const duplicate = structuredClone(authority());
    duplicate.revision.metrics.push({
      ...duplicate.revision.metrics[0],
      metric_id: 'metric-2',
      code: 'EVIDENCE_STRENGTH',
    });
    expect(isGuidelineRevisionAuthorityForTarget(duplicate, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(false);
  });

  it('rejects legacy and arbitrary fields recursively in revision authority', () => {
    for (const legacyField of ['predicates', 'policy_class']) {
      const withLegacyMetric = structuredClone(authority());
      (
        withLegacyMetric.revision.metrics[0] as Record<string, unknown>
      )[legacyField] = legacyField === 'predicates' ? [] : 'standard';
      expect(isGuidelineRevisionAuthorityForTarget(withLegacyMetric, {
        guidelineId: 'guideline-1',
        revisionId: 'revision-2',
        semanticVersion: '2.0.0',
      })).toBe(false);
    }

    const withRules = structuredClone(authority());
    (withRules.revision as Record<string, unknown>).rules = [];
    expect(isGuidelineRevisionAuthorityForTarget(withRules, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(false);

    const withExtra = structuredClone(authority());
    (withExtra.revision as Record<string, unknown>).unexpected = true;
    expect(isGuidelineRevisionAuthorityForTarget(withExtra, {
      guidelineId: 'guideline-1',
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    })).toBe(false);
  });

  it('accepts only the direct exact preview envelope', () => {
    const expected = expectedPreview();
    expect(isGuidelineImpactPreviewResponse(preview(), expected)).toBe(true);
    expect(isGuidelineImpactPreviewResponse(
      { preview: preview() },
      expected,
    )).toBe(false);
    expect(isGuidelineImpactPreviewResponse({
      ...preview(),
      receipt: { ...preview().receipt, impact_digest: 'not-a-sha256' },
    }, expected)).toBe(false);

    const duplicate = structuredClone(preview());
    duplicate.receipt.items.push({
      ...duplicate.receipt.items[0],
    });
    expect(isGuidelineImpactPreviewResponse(duplicate, expected)).toBe(false);

    const extraItemField = structuredClone(preview());
    (
      extraItemField.receipt.items[0] as unknown as Record<string, unknown>
    ).unexpected = true;
    expect(isGuidelineImpactPreviewResponse(extraItemField, expected))
      .toBe(false);

    const staleConfiguration = structuredClone(preview());
    staleConfiguration.receipt.proposed_minimum_confidence = 79;
    expect(isGuidelineImpactPreviewResponse(staleConfiguration, expected))
      .toBe(false);
  });

  it('counts the immutable impact items sealed by the receipt', () => {
    const result = preview();
    expect(countGuidelineImpactItems(result.receipt.items)).toEqual({
      binding: 1,
      target: 1,
      artifact: 0,
      waiver: 0,
    });
  });

  it('accepts only the direct adoption result at the fenced revision', () => {
    const response = adoption();
    const impact = preview();
    expect(isGuidelineAdoptionResponseForPreview(response, impact, 5))
      .toBe(true);
    expect(isGuidelineAdoptionResponseForPreview(response, impact, 4))
      .toBe(false);

    const mismatchedReceipt = structuredClone(response);
    mismatchedReceipt.receipt.impact_digest = 'f'.repeat(64);
    expect(isGuidelineAdoptionResponseForPreview(
      mismatchedReceipt,
      impact,
      5,
    )).toBe(false);

    const mismatchedBinding = structuredClone(response);
    mismatchedBinding.binding.minimum_confidence = 79;
    expect(isGuidelineAdoptionResponseForPreview(
      mismatchedBinding,
      impact,
      5,
    )).toBe(false);

    const malformedConfigurationDigest = structuredClone(response);
    malformedConfigurationDigest.binding.configuration_digest = 'not-a-sha256';
    expect(isGuidelineAdoptionResponseForPreview(
      malformedConfigurationDigest,
      impact,
      5,
    )).toBe(false);
  });

  it('requires the full semantic board-binding configuration', () => {
    expect(isCompleteBoardGuidelineBindingAuthority(boardEntry())).toBe(true);

    const missingConfidence = boardEntry();
    delete missingConfidence.minimum_confidence;
    expect(isCompleteBoardGuidelineBindingAuthority(missingConfidence))
      .toBe(false);

    const invalidOverride = boardEntry();
    invalidOverride.metric_threshold_overrides = { Confidence: 70 };
    expect(isCompleteBoardGuidelineBindingAuthority(invalidOverride))
      .toBe(false);

    const compatibleOverride = boardEntry();
    compatibleOverride.metric_threshold_overrides = {
      'Evidence.v2:API-check': 70,
    };
    expect(isCompleteBoardGuidelineBindingAuthority(compatibleOverride))
      .toBe(true);

    const duplicateOverride = boardEntry();
    duplicateOverride.metric_threshold_overrides = {
      evidence_strength: 70,
      EVIDENCE_STRENGTH: 75,
    };
    expect(isCompleteBoardGuidelineBindingAuthority(duplicateOverride))
      .toBe(false);

    const legacyBinding = boardEntry() as BoardGuidelineEntry
      & Record<string, unknown>;
    legacyBinding.default_enforcement = 'blocking';
    expect(isCompleteBoardGuidelineBindingAuthority(legacyBinding))
      .toBe(false);
  });

  it('discovers a newer semantic head before opening mutation UI', () => {
    const value = authority();
    value.revision.revision_id = 'revision-1';
    value.revision.revision_number = 1;
    value.revision.semantic_version = '1.0.0';
    delete (value.revision as Partial<typeof value.revision>).parent_revision_id;

    expect(latestGuidelineRevisionTargetFromAuthority(value, {
      guidelineId: 'guideline-1',
      requestedRevisionId: 'revision-1',
    })).toEqual({
      revisionId: 'revision-2',
      semanticVersion: '2.0.0',
    });
  });

  it('classifies both stale-preview and binding-head conflicts', () => {
    for (const code of ['guideline_impact_stale', 'binding_head_conflict']) {
      expect(isGuidelineImpactConflict(new PolicyGovernanceApiError({
        message: 'Conflict',
        status: 409,
        kind: 'conflict',
        code,
      }))).toBe(true);
    }
  });
});
