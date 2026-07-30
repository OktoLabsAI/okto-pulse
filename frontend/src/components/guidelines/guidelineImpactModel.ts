import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type { BoardGuidelineEntry } from '@/types';
import type {
  GuidelineAdoptionResponse,
  GuidelineEnforcement,
  GuidelineImpactItem,
  GuidelineImpactItemKind,
  GuidelineImpactPageItem,
  GuidelineImpactReceipt,
  GuidelineRevisionAuthorityResponse,
  PolicyCursorPage,
  PolicyEntityType,
} from '@/types/policy-governance';

export const MAX_GUIDELINE_PRIORITY = 2_147_483_647;

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
const RULE_OPERATORS = new Set(['all', 'any']);
const GUIDELINE_SCOPES = new Set(['global', 'inline']);
const SHA256 = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isNonEmptyString(value);
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

function isItemKind(value: unknown): value is GuidelineImpactItemKind {
  return (
    typeof value === 'string'
    && ITEM_KINDS.has(value as GuidelineImpactItemKind)
  );
}

function isEnforcement(value: unknown): value is GuidelineEnforcement {
  return (
    typeof value === 'string'
    && ENFORCEMENTS.has(value as GuidelineEnforcement)
  );
}

function hasImpactItemIdentity(value: Record<string, unknown>): boolean {
  return (
    isNonEmptyString(value.impact_item_id)
    && isNonEmptyString(value.entity_id)
    && isSha256(value.details_digest)
  );
}

export function isGuidelineImpactItem(
  value: unknown,
): value is GuidelineImpactItem {
  if (!isRecord(value) || !hasImpactItemIdentity(value)) return false;
  if (!isItemKind(value.item_kind)) return false;
  if (!isNullableString(value.related_id)) return false;
  if (
    value.entity_version !== null
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

export function isGuidelineImpactPageItem(
  value: unknown,
): value is GuidelineImpactPageItem {
  if (!isRecord(value) || !hasImpactItemIdentity(value)) return false;
  if (!isItemKind(value.item_kind)) return false;
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
  // A detail waiver row must retain its governed waiver identity. Treat an
  // omitted related_id as an audit gap, not as an empty display value.
  if (value.item_kind === 'waiver' && !isNonEmptyString(value.related_id)) {
    return false;
  }
  if (value.item_kind === 'binding') return value.entity_type === 'board';
  return isEntityType(value.entity_type);
}

export interface GuidelineImpactExpectation {
  boardId: string;
  guidelineId: string;
  revisionId: string;
  revisionNumber: number;
  revisionDigest: string;
  semanticVersion: string;
  priority: number;
  enforcement: GuidelineEnforcement;
  adoptedBinding: {
    bindingId: string;
    bindingRevision: number;
    bindingState: 'active' | 'unlinked';
    revisionId: string;
    semanticVersion: string;
    revisionDigest: string;
  } | null;
}

export function isGuidelineImpactReceiptForPreview(
  value: unknown,
  expected: GuidelineImpactExpectation,
): value is GuidelineImpactReceipt {
  if (!isRecord(value)) return false;
  const valid = (
    isNonEmptyString(value.impact_receipt_id)
    && value.board_id === expected.boardId
    && value.guideline_id === expected.guidelineId
    && isNonEmptyString(value.binding_id)
    && value.to_revision_id === expected.revisionId
    && value.to_revision_number === expected.revisionNumber
    && value.to_semantic_version === expected.semanticVersion
    && value.to_revision_digest === expected.revisionDigest
    && value.expected_head_revision === expected.revisionNumber
    && (
      value.expected_binding_revision === null
      || isIntegerAtLeast(value.expected_binding_revision, 1)
    )
    && (
      value.expected_binding_state === null
      || value.expected_binding_state === 'active'
      || value.expected_binding_state === 'unlinked'
    )
    && isSha256(value.binding_digest)
    && isSha256(value.binding_head_digest_before)
    && isSha256(value.binding_head_digest_after)
    && isSha256(value.policy_set_digest_before)
    && isSha256(value.policy_set_digest_after)
    && isSha256(value.artifact_snapshot_digest)
    && isSha256(value.waiver_snapshot_digest)
    && value.proposed_priority === expected.priority
    && value.proposed_default_enforcement === expected.enforcement
    && Array.isArray(value.affected_entity_types)
    && value.affected_entity_types.every(isEntityType)
    && Array.isArray(value.items)
    && value.items.every(isGuidelineImpactItem)
    && isStringArray(value.added_rule_ids)
    && isStringArray(value.changed_rule_ids)
    && isStringArray(value.removed_rule_ids)
    && isNonEmptyString(value.requested_by)
    && isNonEmptyString(value.created_at)
    && isSha256(value.impact_digest)
    && isNullableString(value.from_revision_id)
    && isNullableString(value.from_semantic_version)
    && (
      value.from_revision_digest === null
      || isSha256(value.from_revision_digest)
    )
    && value.requires_explicit_adoption === true
  );
  if (!valid) return false;
  const items = value.items as GuidelineImpactItem[];
  const affectedEntityTypes = value.affected_entity_types as string[];
  const addedRuleIds = value.added_rule_ids as string[];
  const changedRuleIds = value.changed_rule_ids as string[];
  const removedRuleIds = value.removed_rule_ids as string[];
  const ruleIds = [...addedRuleIds, ...changedRuleIds, ...removedRuleIds];
  const bindingItems = items.filter((item) => item.item_kind === 'binding');
  const bindingItem = bindingItems[0];
  const bindingMatches = expected.adoptedBinding === null
    ? (
        (
          value.expected_binding_revision === null
          && value.expected_binding_state === null
          && value.from_revision_id === null
          && value.from_semantic_version === null
          && value.from_revision_digest === null
        )
        || (
          isIntegerAtLeast(value.expected_binding_revision, 1)
          && value.expected_binding_state === 'unlinked'
          && isNonEmptyString(value.from_revision_id)
          && isNonEmptyString(value.from_semantic_version)
          && isSha256(value.from_revision_digest)
        )
      )
    : (
        value.binding_id === expected.adoptedBinding.bindingId
        && value.expected_binding_revision
          === expected.adoptedBinding.bindingRevision
        && value.expected_binding_state
          === expected.adoptedBinding.bindingState
        && value.from_revision_id === expected.adoptedBinding.revisionId
        && value.from_semantic_version
          === expected.adoptedBinding.semanticVersion
        && value.from_revision_digest
          === expected.adoptedBinding.revisionDigest
      );
  return (
    hasUniqueStrings(items.map((item) => item.impact_item_id))
    && hasUniqueStrings(affectedEntityTypes)
    && hasUniqueStrings(ruleIds)
    && bindingItems.length === 1
    && bindingItem?.entity_id === expected.boardId
    && bindingItem.related_id === value.binding_id
    && bindingMatches
  );
}

function isRuleForRevision(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.rule_id)
    && isNonEmptyString(value.code)
    && isNonEmptyString(value.title)
    && isNonEmptyString(value.description)
    && Array.isArray(value.target_entity_types)
    && value.target_entity_types.length > 0
    && value.target_entity_types.every(isEntityType)
    && hasUniqueStrings(value.target_entity_types)
    && Array.isArray(value.predicates)
    && value.predicates.length > 0
    && value.predicates.every(isPredicateForRevision)
    && isEnforcement(value.enforcement)
    && typeof value.operator === 'string'
    && RULE_OPERATORS.has(value.operator)
    && typeof value.waivable === 'boolean'
    && isNonEmptyString(value.policy_class)
  );
}

function isPolicyScalar(value: unknown): boolean {
  return (
    value === null
    || typeof value === 'string'
    || typeof value === 'number'
    || typeof value === 'boolean'
  );
}

function isPredicateForRevision(value: unknown): boolean {
  if (!isRecord(value) || !isNonEmptyString(value.predicate_code)) {
    return false;
  }
  if (!Array.isArray(value.parameters)) return false;
  const names: string[] = [];
  for (const parameter of value.parameters) {
    if (
      !Array.isArray(parameter)
      || parameter.length !== 2
      || !isNonEmptyString(parameter[0])
      || !(
        isPolicyScalar(parameter[1])
        || (
          Array.isArray(parameter[1])
          && parameter[1].every(isPolicyScalar)
        )
      )
    ) {
      return false;
    }
    names.push(parameter[0]);
  }
  return hasUniqueStrings(names);
}

function isGuidelineRootFor(
  value: unknown,
  guidelineId: string,
): value is GuidelineRevisionAuthorityResponse['guideline'] {
  return (
    isRecord(value)
    && value.guideline_id === guidelineId
    && isNonEmptyString(value.owner_id)
    && typeof value.scope === 'string'
    && GUIDELINE_SCOPES.has(value.scope)
    && isNonEmptyString(value.created_at)
    && value.context_scope === 'all'
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
    value.guideline_id === expected.guidelineId
    && value.revision_id === expected.revisionId
    && isIntegerAtLeast(value.revision_number, 1)
    && isNonEmptyString(value.semantic_version)
    && (
      expected.semanticVersion === undefined
      || value.semantic_version === expected.semanticVersion
    )
    && isNonEmptyString(value.title)
    && isNonEmptyString(value.content)
    && isSha256(value.content_digest)
    && Array.isArray(value.rules)
    && value.rules.every(isRuleForRevision)
    && isNonEmptyString(value.created_by)
    && isNonEmptyString(value.created_at)
    && (
      value.parent_revision_id === undefined
      || isNonEmptyString(value.parent_revision_id)
    )
    && isStringArray(value.tags)
  );
  if (!valid) return false;
  const rules = value.rules as Array<Record<string, unknown>>;
  return (
    hasUniqueStrings(rules.map((rule) => String(rule.rule_id)))
    && hasUniqueStrings(rules.map((rule) => String(rule.code)))
  );
}

function isGuidelineHeadFor(
  value: unknown,
  guidelineId: string,
): value is GuidelineRevisionAuthorityResponse['head'] {
  return (
    isRecord(value)
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
  const guideline = value.guideline;
  const revision = value.revision;
  const head = value.head;
  const valid = (
    isGuidelineRootFor(guideline, expected.guidelineId)
    && isGuidelineRevisionFor(revision, expected)
    && isGuidelineHeadFor(head, expected.guidelineId)
    && head.revision_id === expected.revisionId
    && head.semantic_version === expected.semanticVersion
    && head.revision_number === revision.revision_number
    && value.retirement === undefined
  );
  return valid;
}

export interface GuidelineLatestRevisionTarget {
  revisionId: string;
  semanticVersion: string;
}

/**
 * Validate a response fetched for an adopted revision and return its current
 * head. The caller must fetch that head and run the exact target validator
 * before opening a mutation surface.
 */
export function latestGuidelineRevisionTargetFromAuthority(
  value: unknown,
  expected: {
    guidelineId: string;
    requestedRevisionId: string;
  },
): GuidelineLatestRevisionTarget | null {
  if (!isRecord(value) || value.retirement !== undefined) return null;
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
  default_enforcement: GuidelineEnforcement;
  source_kind: 'native' | 'default_materialization';
  guideline: BoardGuidelineEntry['guideline'] & {
    revision_id: string;
    revision_digest: string;
    semantic_version: string;
  };
} {
  return (
    isNonEmptyString(entry.binding_id)
    && isIntegerAtLeast(entry.binding_revision, 1)
    && entry.binding_state === 'active'
    && (
      entry.source_kind === 'native'
      || entry.source_kind === 'default_materialization'
    )
    && isEnforcement(entry.default_enforcement)
    && isNonEmptyString(entry.guideline.revision_id)
    && isSha256(entry.guideline.revision_digest)
    && isNonEmptyString(entry.guideline.semantic_version)
    && isIntegerAtLeast(entry.priority, 0)
    && entry.priority <= MAX_GUIDELINE_PRIORITY
  );
}

function sameStringArray(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length
    && left.every((value, index) => value === right[index])
  );
}

function sameGuidelineImpactItems(
  left: readonly GuidelineImpactItem[],
  right: readonly GuidelineImpactItem[],
): boolean {
  return (
    left.length === right.length
    && left.every((item, index) => {
      const expected = right[index];
      return (
        expected !== undefined
        && item.impact_item_id === expected.impact_item_id
        && item.item_kind === expected.item_kind
        && item.entity_type === expected.entity_type
        && item.entity_id === expected.entity_id
        && item.related_id === expected.related_id
        && item.entity_version === expected.entity_version
        && item.details_digest === expected.details_digest
      );
    })
  );
}

function sameGuidelineImpactReceipt(
  left: GuidelineImpactReceipt,
  right: GuidelineImpactReceipt,
): boolean {
  return (
    left.impact_receipt_id === right.impact_receipt_id
    && left.board_id === right.board_id
    && left.guideline_id === right.guideline_id
    && left.binding_id === right.binding_id
    && left.to_revision_id === right.to_revision_id
    && left.to_revision_number === right.to_revision_number
    && left.to_semantic_version === right.to_semantic_version
    && left.to_revision_digest === right.to_revision_digest
    && left.expected_head_revision === right.expected_head_revision
    && left.expected_binding_revision === right.expected_binding_revision
    && left.expected_binding_state === right.expected_binding_state
    && left.binding_digest === right.binding_digest
    && left.binding_head_digest_before === right.binding_head_digest_before
    && left.binding_head_digest_after === right.binding_head_digest_after
    && left.policy_set_digest_before === right.policy_set_digest_before
    && left.policy_set_digest_after === right.policy_set_digest_after
    && left.artifact_snapshot_digest === right.artifact_snapshot_digest
    && left.waiver_snapshot_digest === right.waiver_snapshot_digest
    && left.proposed_priority === right.proposed_priority
    && left.proposed_default_enforcement
      === right.proposed_default_enforcement
    && sameStringArray(
      left.affected_entity_types,
      right.affected_entity_types,
    )
    && sameGuidelineImpactItems(left.items, right.items)
    && sameStringArray(left.added_rule_ids, right.added_rule_ids)
    && sameStringArray(left.changed_rule_ids, right.changed_rule_ids)
    && sameStringArray(left.removed_rule_ids, right.removed_rule_ids)
    && left.requested_by === right.requested_by
    && left.created_at === right.created_at
    && left.impact_digest === right.impact_digest
    && left.from_revision_id === right.from_revision_id
    && left.from_semantic_version === right.from_semantic_version
    && left.from_revision_digest === right.from_revision_digest
    && left.requires_explicit_adoption
      === right.requires_explicit_adoption
  );
}

export function isGuidelineAdoptionResponseForReceipt(
  value: unknown,
  expected: GuidelineImpactReceipt,
): value is GuidelineAdoptionResponse {
  if (!isRecord(value)) return false;
  const binding = isRecord(value.binding) ? value.binding : null;
  const receipt = isRecord(value.receipt) ? value.receipt : null;
  const expectedBindingRevision =
    (expected.expected_binding_revision ?? 0) + 1;
  return (
    binding !== null
    && binding.binding_id === expected.binding_id
    && binding.board_id === expected.board_id
    && binding.guideline_id === expected.guideline_id
    && binding.revision_id === expected.to_revision_id
    && binding.semantic_version === expected.to_semantic_version
    && binding.revision_digest === expected.to_revision_digest
    && binding.priority === expected.proposed_priority
    && binding.default_enforcement
      === expected.proposed_default_enforcement
    && binding.state === 'active'
    && binding.binding_revision === expectedBindingRevision
    && isNonEmptyString(binding.adopted_by)
    && isNonEmptyString(binding.adopted_at)
    && (
      binding.source_kind === 'native'
      || binding.source_kind === 'default_materialization'
    )
    && receipt !== null
    && isGuidelineImpactReceiptForPreview(receipt, {
      boardId: expected.board_id,
      guidelineId: expected.guideline_id,
      revisionId: expected.to_revision_id,
      revisionNumber: expected.to_revision_number,
      revisionDigest: expected.to_revision_digest,
      semanticVersion: expected.to_semantic_version,
      priority: expected.proposed_priority,
      enforcement: expected.proposed_default_enforcement,
      adoptedBinding: expected.expected_binding_state === null
        ? null
        : {
            bindingId: expected.binding_id,
            bindingRevision: expected.expected_binding_revision ?? 0,
            bindingState: expected.expected_binding_state,
            revisionId: expected.from_revision_id ?? '',
            semanticVersion: expected.from_semantic_version ?? '',
            revisionDigest: expected.from_revision_digest ?? '',
          },
    })
    && sameGuidelineImpactReceipt(receipt, expected)
  );
}

export function validatedGuidelineImpactPage(
  value: unknown,
): PolicyCursorPage<GuidelineImpactPageItem> {
  if (!isRecord(value)) {
    throw new Error('Impact items returned a malformed cursor page.');
  }
  const items = value.items;
  const limit = value.limit;
  const hasMore = value.has_more;
  const nextCursor = value.next_cursor;
  if (
    !Array.isArray(items)
    || !items.every(isGuidelineImpactPageItem)
    || !isIntegerAtLeast(limit, 1)
    || limit > 200
    || items.length > limit
    || typeof hasMore !== 'boolean'
    || (
      hasMore
      && (!isNonEmptyString(nextCursor) || items.length === 0)
    )
    || (
      !hasMore
      && nextCursor !== undefined
      && nextCursor !== null
    )
  ) {
    throw new Error('Impact items returned a malformed cursor page.');
  }
  if (hasMore) {
    return {
      items,
      limit,
      has_more: true,
      next_cursor: nextCursor as string,
    };
  }
  return {
    items,
    limit,
    has_more: false,
  };
}

export type GuidelineImpactCounts = Record<GuidelineImpactItemKind, number>;

export function countGuidelineImpactItems(
  items: readonly GuidelineImpactItem[],
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
  binding: 'Board binding',
  target: 'Executable target',
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
    'guideline_impact_binding_stale',
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

export function classifyGuidelineImpactCursorError(error: unknown): {
  message: string;
  restartRequired: boolean;
} {
  if (
    error instanceof PolicyGovernanceApiError
    && error.kind === 'invalid_cursor'
  ) {
    return {
      message: 'This cursor no longer matches the impact receipt projection.',
      restartRequired: true,
    };
  }
  return {
    message: guidelineImpactErrorMessage(error),
    restartRequired: false,
  };
}
