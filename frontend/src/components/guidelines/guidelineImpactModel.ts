import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type { BoardGuidelineEntry } from '@/types';
import type {
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
): value is GuidelineImpactPreviewResponse {
  if (
    !isRecord(value)
    || !hasOnlyKeys(value, ['preview_id', 'preview_digest', 'items_page'])
    || !isNonEmptyString(value.preview_id)
    || !isSha256(value.preview_digest)
    || !isRecord(value.items_page)
    || !hasOnlyKeys(value.items_page, ['items', 'next_cursor'])
    || !Array.isArray(value.items_page.items)
    || !value.items_page.items.every(isGuidelineImpactPageItem)
    || (
      value.items_page.next_cursor !== null
      && !isNonEmptyString(value.items_page.next_cursor)
    )
  ) {
    return false;
  }
  return hasUniqueStrings(
    value.items_page.items.map((item) => item.impact_item_id),
  );
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
  expectedBindingRevision: number,
): value is GuidelineAdoptionResponse {
  return (
    isRecord(value)
    && hasOnlyKeys(value, [
      'binding_id',
      'binding_revision',
      'configuration_digest',
      'replayed',
    ])
    && isNonEmptyString(value.binding_id)
    && value.binding_revision === expectedBindingRevision
    && isNonEmptyString(value.configuration_digest)
    && typeof value.replayed === 'boolean'
  );
}

export type GuidelineImpactCounts = Record<GuidelineImpactItemKind, number>;

export function countGuidelineImpactItems(
  items: readonly GuidelineImpactPageItem[],
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
