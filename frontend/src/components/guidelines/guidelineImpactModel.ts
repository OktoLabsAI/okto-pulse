import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type { BoardGuidelineEntry } from '@/types';
import type {
  GuidelineImpactItem,
  GuidelineImpactReceipt,
  GuidelineAdoptionResponse,
  GuidelineEnforcement,
  GuidelineImpactItemKind,
  GuidelineImpactPageItem,
  GuidelineImpactPreviewResponse,
  GuidelineMetricThresholdOverrides,
  GuidelineRevisionAuthorityResponse,
  PolicyEntityType,
} from '@/types/policy-governance';

import { isValidCustomMetricCode } from './semanticMetricEditorModel';

const ENTITY_TYPES = new Set<PolicyEntityType>([
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
]);
const ITEM_KINDS = new Set<GuidelineImpactItemKind>([
  'binding',
  'target',
  'artifact',
  'waiver',
]);
const ENFORCEMENTS = new Set<GuidelineEnforcement>([
  'advisory',
  'blocking',
]);
const GUIDELINE_SCOPES = new Set(['global', 'inline']);
const SHA256 = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const expected = new Set(keys);
  return Object.keys(value).every((key) => expected.has(key));
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).length === keys.length && hasOnlyKeys(value, keys);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isIntegerBetween(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return (
    typeof value === 'number'
    && Number.isInteger(value)
    && value >= minimum
    && value <= maximum
  );
}

function isIntegerAtLeast(value: unknown, minimum: number): value is number {
  return (
    typeof value === 'number'
    && Number.isInteger(value)
    && value >= minimum
  );
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && SHA256.test(value);
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value)
    && value.every(isNonEmptyString)
    && new Set(value).size === value.length
  );
}

function hasUniqueStrings(values: readonly string[]): boolean {
  return new Set(values).size === values.length;
}

function isEntityType(value: unknown): value is PolicyEntityType {
  return (
    typeof value === 'string'
    && ENTITY_TYPES.has(value as PolicyEntityType)
  );
}

function isEnforcement(value: unknown): value is GuidelineEnforcement {
  return (
    typeof value === 'string'
    && ENFORCEMENTS.has(value as GuidelineEnforcement)
  );
}

function isMetricThresholdOverrides(
  value: unknown,
): value is GuidelineMetricThresholdOverrides {
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return (
    hasUniqueCaseInsensitiveStrings(entries.map(([code]) => code))
    && entries.every(
    ([code, threshold]) => (
      isValidCustomMetricCode(code)
      && isIntegerBetween(threshold, 0, 100)
    ),
    )
  );
}

function hasUniqueCaseInsensitiveStrings(values: readonly string[]): boolean {
  return new Set(values.map((value) => value.toLowerCase())).size
    === values.length;
}

export function isGuidelineImpactPageItem(
  value: unknown,
): value is GuidelineImpactPageItem {
  if (
    !isRecord(value)
    || !hasOnlyKeys(value, [
      'impact_item_id',
      'item_kind',
      'entity_type',
      'entity_id',
      'details_digest',
      'related_id',
      'entity_version',
    ])
  ) {
    return false;
  }
  if (
    !isNonEmptyString(value.impact_item_id)
    || !isNonEmptyString(value.entity_id)
    || !isSha256(value.details_digest)
    || typeof value.item_kind !== 'string'
    || !ITEM_KINDS.has(value.item_kind as GuidelineImpactItemKind)
  ) {
    return false;
  }
  if (
    value.related_id !== undefined
    && !isNonEmptyString(value.related_id)
  ) {
    return false;
  }
  if (
    value.entity_version !== undefined
    && !isIntegerAtLeast(value.entity_version, 0)
  ) {
    return false;
  }
  if (value.item_kind === 'waiver' && !isNonEmptyString(value.related_id)) {
    return false;
  }
  if (value.item_kind === 'binding') return value.entity_type === 'board';
  return isEntityType(value.entity_type);
}

export function isGuidelineImpactPreviewResponse(
  value: unknown,
  expected: {
    boardId: string;
    guidelineId: string;
    targetRevisionId: string;
    targetSemanticVersion: string;
    targetRevisionDigest: string;
    proposedPriority: number;
    proposedEnforcement: GuidelineEnforcement;
    proposedMinimumConfidence: number;
    proposedMetricThresholdOverrides: GuidelineMetricThresholdOverrides;
    bindingId: string | null;
    bindingRevision: number | null;
    fromRevisionId: string | null;
    fromSemanticVersion: string | null;
    fromRevisionDigest: string | null;
  },
): value is GuidelineImpactPreviewResponse {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['receipt'])
    || !isGuidelineImpactReceiptFor(value.receipt, expected)
  ) {
    return false;
  }
  return true;
}

const IMPACT_RECEIPT_KEYS = [
  'impact_receipt_id',
  'board_id',
  'guideline_id',
  'binding_id',
  'to_revision_id',
  'to_revision_number',
  'to_semantic_version',
  'to_revision_digest',
  'expected_head_revision',
  'expected_binding_revision',
  'expected_binding_state',
  'binding_digest',
  'binding_head_digest_before',
  'binding_head_digest_after',
  'policy_set_digest_before',
  'policy_set_digest_after',
  'artifact_snapshot_digest',
  'waiver_snapshot_digest',
  'proposed_priority',
  'proposed_enforcement',
  'proposed_minimum_confidence',
  'proposed_metric_threshold_overrides',
  'affected_entity_types',
  'items',
  'added_metric_ids',
  'changed_metric_ids',
  'removed_metric_ids',
  'requested_by',
  'created_at',
  'impact_digest',
  'from_revision_id',
  'from_semantic_version',
  'from_revision_digest',
  'requires_explicit_adoption',
] as const;

function isNullableText(value: unknown): value is string | null {
  return value === null || isNonEmptyString(value);
}

function isGuidelineImpactReceiptItem(
  value: unknown,
): value is GuidelineImpactItem {
  if (
    !isRecord(value)
    || !hasExactKeys(value, [
      'impact_item_id',
      'item_kind',
      'entity_type',
      'entity_id',
      'details_digest',
      'related_id',
      'entity_version',
    ])
    || !isNonEmptyString(value.impact_item_id)
    || !isNonEmptyString(value.entity_id)
    || !isSha256(value.details_digest)
    || typeof value.item_kind !== 'string'
    || !ITEM_KINDS.has(value.item_kind as GuidelineImpactItemKind)
    || !isNullableText(value.related_id)
    || (
      value.entity_version !== null
      && !isIntegerAtLeast(value.entity_version, 0)
    )
  ) {
    return false;
  }
  if (value.item_kind === 'waiver' && !isNonEmptyString(value.related_id)) {
    return false;
  }
  if (value.item_kind === 'binding') return value.entity_type === 'board';
  return isEntityType(value.entity_type);
}

function isGuidelineImpactReceiptFor(
  value: unknown,
  expected: Parameters<typeof isGuidelineImpactPreviewResponse>[1],
): value is GuidelineImpactReceipt {
  if (!isRecord(value) || !hasExactKeys(value, IMPACT_RECEIPT_KEYS)) {
    return false;
  }
  const digestFields = [
    value.to_revision_digest,
    value.binding_digest,
    value.binding_head_digest_before,
    value.binding_head_digest_after,
    value.policy_set_digest_before,
    value.policy_set_digest_after,
    value.artifact_snapshot_digest,
    value.waiver_snapshot_digest,
    value.impact_digest,
  ];
  if (
    !isNonEmptyString(value.impact_receipt_id)
    || !isNonEmptyString(value.binding_id)
    || value.board_id !== expected.boardId
    || value.guideline_id !== expected.guidelineId
    || value.to_revision_id !== expected.targetRevisionId
    || value.to_semantic_version !== expected.targetSemanticVersion
    || value.to_revision_digest !== expected.targetRevisionDigest
    || !isIntegerAtLeast(value.to_revision_number, 1)
    || !isIntegerAtLeast(value.expected_head_revision, 1)
    || value.expected_binding_revision !== expected.bindingRevision
    || value.expected_binding_state
      !== (expected.bindingRevision === null ? null : 'active')
    || digestFields.some((digest) => !isSha256(digest))
    || value.proposed_priority !== expected.proposedPriority
    || value.proposed_enforcement !== expected.proposedEnforcement
    || value.proposed_minimum_confidence
      !== expected.proposedMinimumConfidence
    || !isMetricThresholdOverrides(
      value.proposed_metric_threshold_overrides,
    )
    || JSON.stringify(value.proposed_metric_threshold_overrides)
      !== JSON.stringify(expected.proposedMetricThresholdOverrides)
    || !Array.isArray(value.affected_entity_types)
    || !value.affected_entity_types.every(isEntityType)
    || !hasUniqueStrings(value.affected_entity_types as string[])
    || !Array.isArray(value.items)
    || !value.items.every(isGuidelineImpactReceiptItem)
    || !hasUniqueStrings(
      (value.items as GuidelineImpactItem[])
        .map((item) => item.impact_item_id),
    )
    || !Array.isArray(value.added_metric_ids)
    || !value.added_metric_ids.every(isNonEmptyString)
    || !Array.isArray(value.changed_metric_ids)
    || !value.changed_metric_ids.every(isNonEmptyString)
    || !Array.isArray(value.removed_metric_ids)
    || !value.removed_metric_ids.every(isNonEmptyString)
    || !isNonEmptyString(value.requested_by)
    || !isNonEmptyString(value.created_at)
    || !isNullableText(value.from_revision_id)
    || !isNullableText(value.from_semantic_version)
    || (
      value.from_revision_digest !== null
      && !isSha256(value.from_revision_digest)
    )
    || value.from_revision_id !== expected.fromRevisionId
    || value.from_semantic_version !== expected.fromSemanticVersion
    || value.from_revision_digest !== expected.fromRevisionDigest
    || value.requires_explicit_adoption !== true
  ) {
    return false;
  }
  const metricSets = [
    value.added_metric_ids as string[],
    value.changed_metric_ids as string[],
    value.removed_metric_ids as string[],
  ];
  if (
    metricSets.some((set) => !hasUniqueStrings(set))
    || metricSets.some((set, index) => metricSets.some(
      (other, otherIndex) => (
        index !== otherIndex && set.some((metricId) => other.includes(metricId))
      ),
    ))
  ) {
    return false;
  }
  return expected.bindingId === null || value.binding_id === expected.bindingId;
}

function isMetricForRevision(value: unknown): boolean {
  if (
    !isRecord(value)
    || !hasOnlyKeys(value, [
      'metric_id',
      'code',
      'title',
      'description',
      'evaluation_rubric',
      'target_entity_types',
      'direction',
      'default_threshold',
    ])
  ) {
    return false;
  }
  return (
    isNonEmptyString(value.metric_id)
    && isNonEmptyString(value.code)
    && isValidCustomMetricCode(value.code)
    && isNonEmptyString(value.title)
    && isNonEmptyString(value.description)
    && isNonEmptyString(value.evaluation_rubric)
    && Array.isArray(value.target_entity_types)
    && value.target_entity_types.length > 0
    && value.target_entity_types.every(isEntityType)
    && hasUniqueStrings(value.target_entity_types)
    && (value.direction === 'minimum' || value.direction === 'maximum')
    && isIntegerBetween(value.default_threshold, 0, 100)
  );
}

function isGuidelineRootFor(
  value: unknown,
  guidelineId: string,
): value is GuidelineRevisionAuthorityResponse['guideline'] {
  return (
    isRecord(value)
    && hasOnlyKeys(value, [
      'guideline_id',
      'owner_id',
      'scope',
      'created_at',
      'board_id',
      'context_scope',
    ])
    && value.guideline_id === guidelineId
    && isNonEmptyString(value.owner_id)
    && typeof value.scope === 'string'
    && GUIDELINE_SCOPES.has(value.scope)
    && isNonEmptyString(value.created_at)
    && value.context_scope === 'all'
    && (
      value.board_id === undefined
      || isNonEmptyString(value.board_id)
    )
  );
}

function isGuidelineRevisionFor(
  value: unknown,
  expected: {
    guidelineId: string;
    revisionId: string;
    semanticVersion?: string;
  },
): value is GuidelineRevisionAuthorityResponse['revision'] {
  if (!isRecord(value)) return false;
  const valid = (
    hasOnlyKeys(value, [
      'revision_id',
      'guideline_id',
      'revision_number',
      'semantic_version',
      'title',
      'content',
      'revision_digest',
      'metrics',
      'created_by',
      'created_at',
      'parent_revision_id',
      'tags',
    ])
    && value.guideline_id === expected.guidelineId
    && value.revision_id === expected.revisionId
    && isIntegerAtLeast(value.revision_number, 1)
    && isNonEmptyString(value.semantic_version)
    && (
      expected.semanticVersion === undefined
      || value.semantic_version === expected.semanticVersion
    )
    && isNonEmptyString(value.title)
    && isNonEmptyString(value.content)
    && isSha256(value.revision_digest)
    && Array.isArray(value.metrics)
    && value.metrics.every(isMetricForRevision)
    && isNonEmptyString(value.created_by)
    && isNonEmptyString(value.created_at)
    && (
      value.parent_revision_id === undefined
      || isNonEmptyString(value.parent_revision_id)
    )
    && isStringArray(value.tags)
  );
  if (!valid) return false;
  const metrics = value.metrics as Array<Record<string, unknown>>;
  return (
    hasUniqueStrings(metrics.map((metric) => String(metric.metric_id)))
    && hasUniqueCaseInsensitiveStrings(
      metrics.map((metric) => String(metric.code)),
    )
  );
}

function isGuidelineHeadFor(
  value: unknown,
  guidelineId: string,
): value is GuidelineRevisionAuthorityResponse['head'] {
  return (
    isRecord(value)
    && hasOnlyKeys(value, [
      'guideline_id',
      'revision_id',
      'revision_number',
      'semantic_version',
      'head_revision',
      'updated_at',
    ])
    && value.guideline_id === guidelineId
    && isNonEmptyString(value.revision_id)
    && isIntegerAtLeast(value.revision_number, 1)
    && isNonEmptyString(value.semantic_version)
    && value.head_revision === value.revision_number
    && isNonEmptyString(value.updated_at)
  );
}

export function isGuidelineRevisionAuthorityForTarget(
  value: unknown,
  expected: {
    guidelineId: string;
    revisionId: string;
    semanticVersion: string;
  },
): value is GuidelineRevisionAuthorityResponse {
  if (!isRecord(value)) return false;
  const valid = (
    hasOnlyKeys(value, ['guideline', 'revision', 'head'])
    && isGuidelineRootFor(value.guideline, expected.guidelineId)
    && isGuidelineRevisionFor(value.revision, expected)
    && isGuidelineHeadFor(value.head, expected.guidelineId)
    && value.head.revision_id === expected.revisionId
    && value.head.semantic_version === expected.semanticVersion
    && value.head.revision_number === value.revision.revision_number
    && value.retirement === undefined
  );
  return valid;
}

export interface GuidelineLatestRevisionTarget {
  revisionId: string;
  semanticVersion: string;
}

export function latestGuidelineRevisionTargetFromAuthority(
  value: unknown,
  expected: {
    guidelineId: string;
    requestedRevisionId: string;
  },
): GuidelineLatestRevisionTarget | null {
  if (
    !isRecord(value)
    || !hasOnlyKeys(value, ['guideline', 'revision', 'head'])
  ) {
    return null;
  }
  if (!isGuidelineRootFor(value.guideline, expected.guidelineId)) return null;
  if (!isGuidelineRevisionFor(value.revision, {
    guidelineId: expected.guidelineId,
    revisionId: expected.requestedRevisionId,
  })) {
    return null;
  }
  if (!isGuidelineHeadFor(value.head, expected.guidelineId)) return null;
  if (value.head.revision_number < value.revision.revision_number) return null;
  return {
    revisionId: value.head.revision_id,
    semanticVersion: value.head.semantic_version,
  };
}

export function isCompleteBoardGuidelineBindingAuthority(
  entry: BoardGuidelineEntry,
): entry is BoardGuidelineEntry & {
  binding_id: string;
  binding_revision: number;
  binding_state: 'active';
  enforcement: GuidelineEnforcement;
  minimum_confidence: number;
  metric_threshold_overrides: GuidelineMetricThresholdOverrides;
  source_kind: 'native' | 'default_materialization';
  guideline: BoardGuidelineEntry['guideline'] & {
    revision_id: string;
    revision_digest: string;
    semantic_version: string;
  };
} {
  return (
    isRecord(entry)
    && hasOnlyKeys(entry, [
      'id',
      'guideline',
      'priority',
      'scope',
      'binding_id',
      'binding_revision',
      'enforcement',
      'minimum_confidence',
      'metric_threshold_overrides',
      'binding_state',
      'source_kind',
    ])
    && isRecord(entry.guideline)
    && hasOnlyKeys(
      entry.guideline,
      [
        'id',
        'title',
        'content',
        'tags',
        'scope',
        'board_id',
        'owner_id',
        'version',
        'semantic_version',
        'revision_id',
        'revision_digest',
        'context_scope',
        'created_at',
        'updated_at',
      ],
    )
    && isNonEmptyString(entry.binding_id)
    && isIntegerAtLeast(entry.binding_revision, 1)
    && entry.binding_state === 'active'
    && (
      entry.source_kind === 'native'
      || entry.source_kind === 'default_materialization'
    )
    && isEnforcement(entry.enforcement)
    && isIntegerBetween(entry.minimum_confidence, 0, 100)
    && isMetricThresholdOverrides(entry.metric_threshold_overrides)
    && isNonEmptyString(entry.guideline.revision_id)
    && isSha256(entry.guideline.revision_digest)
    && isNonEmptyString(entry.guideline.semantic_version)
  );
}

export function isGuidelineAdoptionResponseForPreview(
  value: unknown,
  preview: GuidelineImpactPreviewResponse,
  expectedBindingRevision: number,
): value is GuidelineAdoptionResponse {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['binding', 'receipt'])
    || !isRecord(value.binding)
    || !hasExactKeys(value.binding, [
      'binding_id',
      'board_id',
      'guideline_id',
      'revision_id',
      'semantic_version',
      'revision_digest',
      'priority',
      'binding_revision',
      'adopted_by',
      'adopted_at',
      'enforcement',
      'minimum_confidence',
      'metric_threshold_overrides',
      'configuration_digest',
      'state',
      'source_kind',
    ])
    || JSON.stringify(value.receipt) !== JSON.stringify(preview.receipt)
  ) {
    return false;
  }
  const binding = value.binding;
  const receipt = preview.receipt;
  return (
    binding.binding_id === receipt.binding_id
    && binding.board_id === receipt.board_id
    && binding.guideline_id === receipt.guideline_id
    && binding.revision_id === receipt.to_revision_id
    && binding.semantic_version === receipt.to_semantic_version
    && binding.revision_digest === receipt.to_revision_digest
    && binding.priority === receipt.proposed_priority
    && binding.binding_revision === expectedBindingRevision
    && isNonEmptyString(binding.adopted_by)
    && isNonEmptyString(binding.adopted_at)
    && binding.enforcement === receipt.proposed_enforcement
    && binding.minimum_confidence === receipt.proposed_minimum_confidence
    && isMetricThresholdOverrides(binding.metric_threshold_overrides)
    && JSON.stringify(binding.metric_threshold_overrides)
      === JSON.stringify(receipt.proposed_metric_threshold_overrides)
    && isSha256(binding.configuration_digest)
    && binding.state === 'active'
    && (
      binding.source_kind === 'native'
      || binding.source_kind === 'default_materialization'
    )
  );
}

export type GuidelineImpactCounts = Record<GuidelineImpactItemKind, number>;

export function countGuidelineImpactItems(
  items: readonly (GuidelineImpactItem | GuidelineImpactPageItem)[],
): GuidelineImpactCounts {
  const counts: GuidelineImpactCounts = {
    binding: 0,
    target: 0,
    artifact: 0,
    waiver: 0,
  };
  for (const item of items) counts[item.item_kind] += 1;
  return counts;
}

export const GUIDELINE_IMPACT_KIND_LABEL: Record<
  GuidelineImpactItemKind,
  string
> = {
  binding: 'Board configuration',
  target: 'Semantic target',
  artifact: 'Affected artifact',
  waiver: 'Governed waiver',
};

export function createGuidelinePolicyClientId(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `${prefix}-${uuid}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function guidelineImpactErrorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    const reasonCode = error.details.reason_code ?? error.code;
    if (reasonCode === 'guideline_impact_no_changes') {
      return 'This exact guideline revision and board configuration are already active. Change an enforcement or threshold setting before reviewing impact again.';
    }
    if (
      error.kind === 'not_found'
      || error.status === 404
      || reasonCode === 'guideline_not_found'
      || reasonCode === 'guideline_revision_not_found'
    ) {
      return 'This guideline or revision is no longer available. Refresh the catalog and verify that you selected the latest version.';
    }
    return error.nextAction
      ? `${error.message} Next: ${error.nextAction}.`
      : error.message;
  }
  return error instanceof Error
    ? error.message
    : 'Unexpected guideline policy error.';
}

export function isGuidelineImpactConflict(error: unknown): boolean {
  const staleReasonCodes = new Set([
    'guideline_impact_stale',
    'binding_head_conflict',
  ]);
  return (
    error instanceof PolicyGovernanceApiError
    && (
      error.kind === 'conflict'
      || error.status === 409
      || staleReasonCodes.has(error.code)
      || staleReasonCodes.has(error.details.reason_code ?? '')
    )
  );
}
