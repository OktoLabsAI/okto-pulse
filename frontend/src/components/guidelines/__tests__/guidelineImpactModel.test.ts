import { describe, expect, it } from 'vitest';

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import type { BoardGuidelineEntry } from '@/types';
import type {
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
    preview_id: 'preview-1',
    preview_digest: 'b'.repeat(64),
    items_page: {
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
        entity_version: 3,
        details_digest: 'd'.repeat(64),
      }],
      next_cursor: null,
    },
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
    expect(isGuidelineImpactPreviewResponse(preview())).toBe(true);
    expect(isGuidelineImpactPreviewResponse({ preview: preview() })).toBe(false);
    expect(isGuidelineImpactPreviewResponse({
      ...preview(),
      receipt: {},
    })).toBe(false);
    expect(isGuidelineImpactPreviewResponse({
      ...preview(),
      preview_digest: 'not-a-sha256',
    })).toBe(false);

    const duplicate = structuredClone(preview());
    duplicate.items_page.items.push({
      ...duplicate.items_page.items[0],
    });
    expect(isGuidelineImpactPreviewResponse(duplicate)).toBe(false);

    const extraItemField = structuredClone(preview());
    (
      extraItemField.items_page.items[0] as Record<string, unknown>
    ).unexpected = true;
    expect(isGuidelineImpactPreviewResponse(extraItemField)).toBe(false);
  });

  it('counts only the canonical first-page impact items', () => {
    const result = preview();
    expect(countGuidelineImpactItems(result.items_page.items)).toEqual({
      binding: 1,
      target: 1,
      artifact: 0,
      waiver: 0,
    });
  });

  it('accepts only the direct adoption result at the fenced revision', () => {
    const response = {
      binding_id: 'binding-1',
      binding_revision: 5,
      configuration_digest: 'e'.repeat(64),
      replayed: false,
    };
    expect(isGuidelineAdoptionResponseForPreview(response, 5)).toBe(true);
    expect(isGuidelineAdoptionResponseForPreview(response, 4)).toBe(false);
    expect(isGuidelineAdoptionResponseForPreview({
      binding: response,
    }, 5)).toBe(false);
    expect(isGuidelineAdoptionResponseForPreview({
      ...response,
      receipt: {},
    }, 5)).toBe(false);
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
