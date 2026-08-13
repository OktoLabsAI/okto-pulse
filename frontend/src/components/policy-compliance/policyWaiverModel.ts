import type { CursorCollectionError } from '@/hooks/useOpaqueCursorCollection';
import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type {
  NonEmptyArray,
  PolicyEntityType,
  PolicyWaiverEventType,
  PolicyWaiverStatus,
  RequestedSemanticWaiverResponse,
  ReviewedSemanticWaiverResponse,
  RevokedSemanticWaiverResponse,
  RevalidatedSemanticWaiverResponse,
  SemanticAssessmentCurrentnessReason,
  SemanticEvidenceRef,
  SemanticMetricWaiverExpireReason,
  SemanticMetricWaiverRevalidationReason,
  SemanticMetricWaiverRevalidationStatus,
  SemanticWaiverEvent,
  SemanticWaiverFull,
} from '@/types/policy-governance';

import {
  formatPolicyToken,
  policyUiErrorMessage,
} from './policyUiModel';
import {
  parseSemanticWaiverDetail,
} from './semanticPolicyModel';

const ENTITY_TYPES = new Set<PolicyEntityType>([
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
]);
const WAIVER_STATUSES = new Set<PolicyWaiverStatus>([
  'requested',
  'approved',
  'rejected',
  'revoked',
  'expired',
]);
const EVENT_TYPES = new Set<PolicyWaiverEventType>([
  'request',
  'approve',
  'reject',
  'revoke',
  'expire',
  'revalidate',
]);
const EXPIRE_REASONS = new Set<SemanticMetricWaiverExpireReason>([
  'scheduled_expiry',
  'subject_scope_changed',
  'guideline_revision_changed',
  'binding_configuration_changed',
  'metric_result_changed',
]);
const REVALIDATION_STATUSES =
  new Set<SemanticMetricWaiverRevalidationStatus>([
    'approved',
    'expired',
    'anchor_stale',
    'revoked',
  ]);
const REVALIDATION_REASONS =
  new Set<SemanticMetricWaiverRevalidationReason>([
    'current',
    'scheduled_expiry',
    'anchor_missing',
    'subject_scope_changed',
    'guideline_revision_changed',
    'binding_configuration_changed',
    'metric_result_changed',
    'revoked',
  ]);
const CURRENTNESS_REASONS =
  new Set<SemanticAssessmentCurrentnessReason>([
    'current_snapshot_missing',
    'subject_version_changed',
    'subject_content_changed',
    'guideline_revision_changed',
    'guideline_revision_digest_changed',
    'binding_revision_changed',
    'binding_configuration_changed',
    'policy_set_changed',
    'binding_head_changed',
    'input_digest_changed',
  ]);

const WAIVER_DETAIL_FIELDS = [
  'projection',
  'waiver_id',
  'board_id',
  'entity_type',
  'subject_id',
  'subject_version',
  'validation_edition',
  'lifecycle_state',
  'finding_id',
  'receipt_id',
  'guideline_id',
  'guideline_revision_id',
  'binding_id',
  'metric_id',
  'metric_code',
  'status',
  'waiver_revision',
  'currentness',
  'currentness_reasons',
  'requested_at',
  'expires_at',
  'last_event_type',
  'last_event_at',
  'justification',
  'requested_by',
  'original_expires_at',
  'reviewed_by',
  'reviewed_at',
  'review_reason',
  'revoked_by',
  'revoked_at',
  'expire_reason',
  'evidence_refs',
] as const;
const WAIVER_FULL_FIELDS = [
  ...WAIVER_DETAIL_FIELDS,
  'metric_result_id',
  'metric_result_digest',
  'finding_digest',
  'receipt_digest',
  'subject_content_digest',
  'guideline_revision_digest',
  'binding_revision',
  'binding_configuration_digest',
  'scope_digest',
  'head_digest',
  'last_event_id',
  'last_event_idempotency_key',
  'assessment_assessor_id',
  'last_revalidation_status',
  'last_revalidation_current',
  'last_revalidation_reason_code',
  'last_revalidation_evaluated_at',
  'last_revalidation_currentness_reasons',
  'last_revalidation_scheduled_expiry_observed',
] as const;
const PAGE_FIELDS = [
  'items',
  'projection',
  'next_cursor',
  'has_more',
] as const;
const EVIDENCE_FIELDS = [
  'source_type',
  'source_id',
  'source_version',
  'content_hash',
] as const;
const EVENT_FIELDS = [
  'event_id',
  'predecessor_event_id',
  'waiver_id',
  'waiver_revision',
  'event_type',
  'from_status',
  'to_status',
  'actor_id',
  'occurred_at',
  'reason',
  'evidence_refs',
  'expires_at',
  'scope_digest',
  'waiver_digest',
  'idempotency_key',
  'request_digest',
  'expire_reason',
  'evaluated_at',
  'revalidation_status',
  'revalidation_current',
  'revalidation_reason_code',
  'currentness_reasons',
  'scheduled_expiry_observed',
] as const;

export const POLICY_WAIVER_STATUS_LABEL: Record<
  PolicyWaiverStatus,
  string
> = {
  requested: 'Requested',
  approved: 'Approved',
  rejected: 'Rejected',
  revoked: 'Revoked',
  expired: 'Expired',
};

export const POLICY_WAIVER_EVENT_LABEL: Record<
  PolicyWaiverEventType,
  string
> = {
  request: 'Requested',
  approve: 'Approved',
  reject: 'Rejected',
  revoke: 'Revoked',
  expire: 'Expired',
  revalidate: 'Revalidated',
};

export interface SemanticEvidenceDraft {
  sourceType: string;
  sourceId: string;
  sourceVersion: string;
  contentHash: string;
}

export interface SemanticWaiverPageExpectation {
  evaluatedAt?: string;
  status?: PolicyWaiverStatus;
  entityType?: PolicyEntityType;
  subjectId?: string;
  findingId?: string;
  metricResultId?: string;
  receiptId?: string;
}

export type SemanticWaiverMutationResult =
  | {
      action: 'request';
      waiverId: string;
      waiverRevision: 1;
      status: 'requested';
      replayed: false;
      scopeDigest: string;
    }
  | {
      action: 'approve' | 'reject';
      waiverId: string;
      waiverRevision: number;
      status: 'approved' | 'rejected';
      replayed: boolean;
      actorId: string;
    }
  | {
      action: 'revoke';
      waiverId: string;
      waiverRevision: number;
      status: 'revoked';
      replayed: boolean;
    }
  | {
      action: 'revalidate';
      waiverId: string;
      waiverRevision: number;
      status: SemanticMetricWaiverRevalidationStatus;
      replayed: boolean;
      current: boolean;
      reasonCode: SemanticMetricWaiverRevalidationReason;
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactFields(
  value: Record<string, unknown>,
  fields: readonly string[],
  label: string,
): void {
  const expected = new Set(fields);
  const keys = Object.keys(value);
  if (
    keys.length !== fields.length
    || fields.some(
      (field) => !Object.prototype.hasOwnProperty.call(value, field),
    )
    || keys.some((field) => !expected.has(field))
  ) {
    throw new Error(
      `Semantic metric waiver ${label} has an unknown or missing field.`,
    );
  }
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Semantic metric waiver ${label} is invalid.`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < 1
  ) {
    throw new Error(`Semantic metric waiver ${label} is invalid.`);
  }
  return value;
}

function timestamp(value: unknown, label: string): string {
  const parsed = requiredText(value, label);
  if (Number.isNaN(Date.parse(parsed))) {
    throw new Error(`Semantic metric waiver ${label} is invalid.`);
  }
  return parsed;
}

function nullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : timestamp(value, label);
}

function sha256(value: unknown, label: string): string {
  const parsed = requiredText(value, label);
  if (!/^[0-9a-f]{64}$/u.test(parsed)) {
    throw new Error(`Semantic metric waiver ${label} is invalid.`);
  }
  return parsed;
}

function closedUniqueCurrentnessReasons(
  value: unknown,
  label: string,
): SemanticAssessmentCurrentnessReason[] {
  if (!Array.isArray(value)) {
    throw new Error(`Semantic metric waiver ${label} is invalid.`);
  }
  const reasons = value.map((item) => {
    if (
      typeof item !== 'string'
      || !CURRENTNESS_REASONS.has(
        item as SemanticAssessmentCurrentnessReason,
      )
    ) {
      throw new Error(`Semantic metric waiver ${label} is invalid.`);
    }
    return item as SemanticAssessmentCurrentnessReason;
  });
  if (new Set(reasons).size !== reasons.length) {
    throw new Error(`Semantic metric waiver ${label} contains duplicates.`);
  }
  return reasons;
}

function parseEvidenceRef(value: unknown): SemanticEvidenceRef {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver evidence is invalid.');
  }
  exactFields(value, EVIDENCE_FIELDS, 'evidence');
  return {
    source_type: requiredText(value.source_type, 'evidence source type'),
    source_id: requiredText(value.source_id, 'evidence source identity'),
    source_version: positiveInteger(
      value.source_version,
      'evidence source version',
    ),
    content_hash: sha256(value.content_hash, 'evidence content hash'),
  };
}

function parseEvidenceRefs(
  value: unknown,
): NonEmptyArray<SemanticEvidenceRef> {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error('Semantic metric waiver evidence is required.');
  }
  const references = value.map(parseEvidenceRef);
  const identities = references.map((item) =>
    [
      item.source_type,
      item.source_id,
      item.source_version,
      item.content_hash,
    ].join(':'),
  );
  if (new Set(identities).size !== identities.length) {
    throw new Error('Semantic metric waiver evidence contains duplicates.');
  }
  return references as NonEmptyArray<SemanticEvidenceRef>;
}

export function parseSemanticEvidenceDrafts(
  drafts: readonly SemanticEvidenceDraft[],
): NonEmptyArray<SemanticEvidenceRef> | null {
  try {
    return parseEvidenceRefs(drafts.map((draft) => ({
      source_type: draft.sourceType.trim(),
      source_id: draft.sourceId.trim(),
      source_version: Number(draft.sourceVersion),
      content_hash: draft.contentHash.trim().toLowerCase(),
    })));
  } catch {
    return null;
  }
}

export function emptySemanticEvidenceDraft(): SemanticEvidenceDraft {
  return {
    sourceType: '',
    sourceId: '',
    sourceVersion: '1',
    contentHash: '',
  };
}

function detailProjectionOf(
  value: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    WAIVER_DETAIL_FIELDS.map((field) => [
      field,
      field === 'projection' ? 'detail' : value[field],
    ]),
  );
}

function validateRevalidationHead(
  value: Record<string, unknown>,
): {
  status: SemanticMetricWaiverRevalidationStatus | null;
  current: boolean | null;
  reason: SemanticMetricWaiverRevalidationReason | null;
  evaluatedAt: string | null;
  reasons: SemanticAssessmentCurrentnessReason[];
  scheduledExpiryObserved: boolean;
} {
  const status = value.last_revalidation_status;
  const current = value.last_revalidation_current;
  const reason = value.last_revalidation_reason_code;
  const evaluatedAt = value.last_revalidation_evaluated_at;
  const reasons = closedUniqueCurrentnessReasons(
    value.last_revalidation_currentness_reasons,
    'last revalidation currentness reasons',
  );
  const observed = value.last_revalidation_scheduled_expiry_observed;
  if (typeof observed !== 'boolean') {
    throw new Error(
      'Semantic metric waiver last revalidation expiry evidence is invalid.',
    );
  }
  if (status === null) {
    if (
      current !== null
      || reason !== null
      || evaluatedAt !== null
      || reasons.length > 0
      || observed
    ) {
      throw new Error(
        'Semantic metric waiver last revalidation evidence is partial.',
      );
    }
    return {
      status: null,
      current: null,
      reason: null,
      evaluatedAt: null,
      reasons,
      scheduledExpiryObserved: false,
    };
  }
  if (
    typeof status !== 'string'
    || !REVALIDATION_STATUSES.has(
      status as SemanticMetricWaiverRevalidationStatus,
    )
    || typeof current !== 'boolean'
    || typeof reason !== 'string'
    || !REVALIDATION_REASONS.has(
      reason as SemanticMetricWaiverRevalidationReason,
    )
    || typeof evaluatedAt !== 'string'
  ) {
    throw new Error(
      'Semantic metric waiver last revalidation evidence is invalid.',
    );
  }
  const parsedAt = timestamp(evaluatedAt, 'last revalidation timestamp');
  if (
    current !== (status === 'approved')
    || (status === 'approved' && reason !== 'current')
    || (status === 'expired' && reason !== 'scheduled_expiry')
    || (status === 'revoked' && reason !== 'revoked')
    || (status === 'anchor_stale' && (
      reason === 'current'
      || reason === 'scheduled_expiry'
      || reason === 'revoked'
    ))
    || (status === 'anchor_stale' && reasons.length === 0)
    || (status !== 'anchor_stale' && reasons.length > 0)
    || observed !== (status === 'expired')
  ) {
    throw new Error(
      'Semantic metric waiver last revalidation decision is inconsistent.',
    );
  }
  return {
    status: status as SemanticMetricWaiverRevalidationStatus,
    current,
    reason: reason as SemanticMetricWaiverRevalidationReason,
    evaluatedAt: parsedAt,
    reasons,
    scheduledExpiryObserved: observed,
  };
}

export function parseSemanticWaiverFull(
  value: unknown,
  expected: {
    boardId: string;
    evaluatedAt?: string;
    waiverId?: string;
    findingId?: string;
    metricResultId?: string;
  },
): SemanticWaiverFull {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver is invalid.');
  }
  exactFields(value, WAIVER_FULL_FIELDS, 'full projection');
  if (value.projection !== 'full') {
    throw new Error('Semantic metric waiver projection is not full.');
  }
  if (
    value.board_id !== expected.boardId
    || (
      expected.waiverId !== undefined
      && value.waiver_id !== expected.waiverId
    )
    || (
      expected.findingId !== undefined
      && value.finding_id !== expected.findingId
    )
    || (
      expected.metricResultId !== undefined
      && value.metric_result_id !== expected.metricResultId
    )
    || !ENTITY_TYPES.has(value.entity_type as PolicyEntityType)
  ) {
    throw new Error(
      'Semantic metric waiver evidence does not match its expected scope.',
    );
  }
  const detail = parseSemanticWaiverDetail(
    detailProjectionOf(value),
    {
      boardId: expected.boardId,
      entityType: value.entity_type as PolicyEntityType,
      subjectId: requiredText(value.subject_id, 'subject identity'),
    },
  );
  const lastRevalidation = validateRevalidationHead(value);
  const result: SemanticWaiverFull = {
    ...detail,
    projection: 'full',
    metric_result_id: requiredText(
      value.metric_result_id,
      'metric result identity',
    ),
    metric_result_digest: sha256(
      value.metric_result_digest,
      'metric result digest',
    ),
    finding_digest: sha256(value.finding_digest, 'finding digest'),
    receipt_digest: sha256(value.receipt_digest, 'receipt digest'),
    subject_content_digest: sha256(
      value.subject_content_digest,
      'subject content digest',
    ),
    guideline_revision_digest: sha256(
      value.guideline_revision_digest,
      'guideline revision digest',
    ),
    binding_revision: positiveInteger(
      value.binding_revision,
      'binding revision',
    ),
    binding_configuration_digest: sha256(
      value.binding_configuration_digest,
      'binding configuration digest',
    ),
    scope_digest: sha256(value.scope_digest, 'scope digest'),
    head_digest: sha256(value.head_digest, 'head digest'),
    last_event_id: requiredText(value.last_event_id, 'last event identity'),
    last_event_idempotency_key: requiredText(
      value.last_event_idempotency_key,
      'last event idempotency key',
    ),
    assessment_assessor_id: requiredText(
      value.assessment_assessor_id,
      'assessment assessor identity',
    ),
    last_revalidation_status: lastRevalidation.status,
    last_revalidation_current: lastRevalidation.current,
    last_revalidation_reason_code: lastRevalidation.reason,
    last_revalidation_evaluated_at: lastRevalidation.evaluatedAt,
    last_revalidation_currentness_reasons: lastRevalidation.reasons,
    last_revalidation_scheduled_expiry_observed:
      lastRevalidation.scheduledExpiryObserved,
  };
  if (
    result.last_event_type === 'revalidate'
    !== (result.last_revalidation_status !== null)
  ) {
    throw new Error(
      'Semantic metric waiver head contradicts its revalidation evidence.',
    );
  }
  if (expected.evaluatedAt !== undefined) {
    const evaluatedAt = Date.parse(
      timestamp(expected.evaluatedAt, 'snapshot evaluation timestamp'),
    );
    const expiresAt = result.expires_at === null
      ? null
      : Date.parse(result.expires_at);
    if (
      (
        result.status === 'approved'
        && expiresAt !== null
        && expiresAt <= evaluatedAt
      )
      || (
        result.status === 'expired'
        && result.expire_reason === 'scheduled_expiry'
        && (expiresAt === null || expiresAt > evaluatedAt)
      )
    ) {
      throw new Error(
        'Semantic metric waiver expiry does not match its evaluation snapshot.',
      );
    }
  }
  return result;
}

export function parseSemanticWaiverHeadResponse(
  value: unknown,
  expected: {
    boardId: string;
    evaluatedAt: string;
    waiverId: string;
    findingId?: string;
    metricResultId?: string;
  },
): SemanticWaiverFull {
  if (
    !isRecord(value)
    || Object.keys(value).length !== 1
    || !Object.prototype.hasOwnProperty.call(value, 'waiver')
  ) {
    throw new Error(
      'Semantic metric waiver head returned a malformed response.',
    );
  }
  return parseSemanticWaiverFull(value.waiver, expected);
}

export function validatedSemanticWaiverPage(
  value: unknown,
  boardId: string,
  expected: SemanticWaiverPageExpectation = {},
  pageLimit = 25,
): {
  items: SemanticWaiverFull[];
  limit: number;
  has_more: boolean;
  next_cursor?: string;
} {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver list returned a malformed page.');
  }
  exactFields(value, PAGE_FIELDS, 'cursor page');
  if (
    value.projection !== 'full'
    || !Array.isArray(value.items)
    || value.items.length > pageLimit
    || typeof value.has_more !== 'boolean'
    || (
      value.has_more
      && (
        typeof value.next_cursor !== 'string'
        || value.next_cursor.length === 0
        || value.items.length === 0
      )
    )
    || (!value.has_more && value.next_cursor !== null)
  ) {
    throw new Error('Semantic metric waiver list returned a malformed page.');
  }
  const items = value.items.map((item) =>
    parseSemanticWaiverFull(item, {
      boardId,
      ...(expected.evaluatedAt === undefined
        ? {}
        : { evaluatedAt: expected.evaluatedAt }),
    }),
  );
  if (items.some((item) => (
    (expected.status !== undefined && item.status !== expected.status)
    || (
      expected.entityType !== undefined
      && item.entity_type !== expected.entityType
    )
    || (
      expected.subjectId !== undefined
      && item.subject_id !== expected.subjectId
    )
    || (
      expected.findingId !== undefined
      && item.finding_id !== expected.findingId
    )
    || (
      expected.metricResultId !== undefined
      && item.metric_result_id !== expected.metricResultId
    )
    || (
      expected.receiptId !== undefined
      && item.receipt_id !== expected.receiptId
    )
  ))) {
    throw new Error(
      'Semantic metric waiver list returned cross-filter evidence.',
    );
  }
  for (let index = 1; index < items.length; index += 1) {
    const previous = items[index - 1] as SemanticWaiverFull;
    const current = items[index] as SemanticWaiverFull;
    const previousTime = Date.parse(previous.requested_at);
    const currentTime = Date.parse(current.requested_at);
    if (
      currentTime > previousTime
      || (
        currentTime === previousTime
        && current.waiver_id.localeCompare(previous.waiver_id) > 0
      )
    ) {
      throw new Error(
        'Semantic metric waiver list violated newest-first keyset order.',
      );
    }
  }
  return value.has_more
    ? {
        items,
        limit: pageLimit,
        has_more: true,
        next_cursor: value.next_cursor as string,
      }
    : {
        items,
        limit: pageLimit,
        has_more: false,
      };
}

function parseSemanticWaiverEvent(
  value: unknown,
  waiverId: string,
): SemanticWaiverEvent {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver event is invalid.');
  }
  exactFields(value, EVENT_FIELDS, 'event');
  if (
    value.waiver_id !== waiverId
    || !EVENT_TYPES.has(value.event_type as PolicyWaiverEventType)
    || (
      value.from_status !== null
      && !WAIVER_STATUSES.has(value.from_status as PolicyWaiverStatus)
    )
    || !WAIVER_STATUSES.has(value.to_status as PolicyWaiverStatus)
    || typeof value.scheduled_expiry_observed !== 'boolean'
  ) {
    throw new Error('Semantic metric waiver event lifecycle is invalid.');
  }
  const eventType = value.event_type as PolicyWaiverEventType;
  const fromStatus = value.from_status as PolicyWaiverStatus | null;
  const toStatus = value.to_status as PolicyWaiverStatus;
  const revision = positiveInteger(value.waiver_revision, 'event revision');
  const transitions: Record<
    Exclude<PolicyWaiverEventType, 'revalidate'>,
    [PolicyWaiverStatus | null, PolicyWaiverStatus]
  > = {
    request: [null, 'requested'],
    approve: ['requested', 'approved'],
    reject: ['requested', 'rejected'],
    revoke: ['approved', 'revoked'],
    expire: ['approved', 'expired'],
  };
  if (
    eventType === 'revalidate'
      ? !(
          fromStatus === 'approved'
          || fromStatus === 'expired'
          || fromStatus === 'revoked'
        )
      : (
          fromStatus !== transitions[eventType][0]
          || toStatus !== transitions[eventType][1]
        )
  ) {
    throw new Error('Semantic metric waiver event transition is invalid.');
  }
  const evidenceRefs = parseEvidenceRefs(value.evidence_refs);
  const expireReason = value.expire_reason;
  if (
    expireReason !== null
    && !EXPIRE_REASONS.has(
      expireReason as SemanticMetricWaiverExpireReason,
    )
  ) {
    throw new Error('Semantic metric waiver event expiry reason is invalid.');
  }
  const currentnessReasons = closedUniqueCurrentnessReasons(
    value.currentness_reasons,
    'event currentness reasons',
  );
  const revalidationStatus = value.revalidation_status;
  const revalidationCurrent = value.revalidation_current;
  const revalidationReason = value.revalidation_reason_code;
  const evaluatedAt = value.evaluated_at;
  if (eventType === 'revalidate') {
    if (
      typeof revalidationStatus !== 'string'
      || !REVALIDATION_STATUSES.has(
        revalidationStatus as SemanticMetricWaiverRevalidationStatus,
      )
      || typeof revalidationCurrent !== 'boolean'
      || typeof revalidationReason !== 'string'
      || !REVALIDATION_REASONS.has(
        revalidationReason as SemanticMetricWaiverRevalidationReason,
      )
      || evaluatedAt === null
      || revalidationCurrent !== (revalidationStatus === 'approved')
    ) {
      throw new Error(
        'Semantic metric waiver revalidation event is invalid.',
      );
    }
  } else if (
    revalidationStatus !== null
    || revalidationCurrent !== null
    || revalidationReason !== null
    || evaluatedAt !== null
    || currentnessReasons.length > 0
    || value.scheduled_expiry_observed
  ) {
    throw new Error(
      'Semantic metric waiver non-revalidation event has extra evidence.',
    );
  }
  return {
    event_id: requiredText(value.event_id, 'event identity'),
    predecessor_event_id: value.predecessor_event_id === null
      ? null
      : requiredText(
          value.predecessor_event_id,
          'predecessor event identity',
        ),
    waiver_id: waiverId,
    waiver_revision: revision,
    event_type: eventType,
    from_status: fromStatus,
    to_status: toStatus,
    actor_id: requiredText(value.actor_id, 'event actor'),
    occurred_at: timestamp(value.occurred_at, 'event timestamp'),
    reason: requiredText(value.reason, 'event reason'),
    evidence_refs: evidenceRefs,
    expires_at: nullableTimestamp(value.expires_at, 'event expiry'),
    scope_digest: sha256(value.scope_digest, 'event scope digest'),
    waiver_digest: sha256(value.waiver_digest, 'event waiver digest'),
    idempotency_key: requiredText(
      value.idempotency_key,
      'event idempotency key',
    ),
    request_digest: sha256(value.request_digest, 'event request digest'),
    expire_reason:
      expireReason as SemanticMetricWaiverExpireReason | null,
    evaluated_at: evaluatedAt === null
      ? null
      : timestamp(evaluatedAt, 'event evaluation timestamp'),
    revalidation_status:
      revalidationStatus as SemanticMetricWaiverRevalidationStatus | null,
    revalidation_current: revalidationCurrent as boolean | null,
    revalidation_reason_code:
      revalidationReason as SemanticMetricWaiverRevalidationReason | null,
    currentness_reasons: currentnessReasons,
    scheduled_expiry_observed: value.scheduled_expiry_observed,
  };
}

export function validatedSemanticWaiverEvents(
  value: unknown,
  head: SemanticWaiverFull,
): SemanticWaiverEvent[] {
  if (
    !isRecord(value)
    || Object.keys(value).length !== 1
    || !Object.prototype.hasOwnProperty.call(value, 'events')
    || !Array.isArray(value.events)
  ) {
    throw new Error(
      'Semantic metric waiver history returned malformed evidence.',
    );
  }
  const events = value.events.map((item) =>
    parseSemanticWaiverEvent(item, head.waiver_id),
  );
  if (
    events.length !== head.waiver_revision
    || events.some((event, index) => (
      event.waiver_revision !== index + 1
      || (
        index === 0
          ? event.predecessor_event_id !== null
          : event.predecessor_event_id !== events[index - 1]?.event_id
      )
      || (
        index > 0
        && event.from_status !== events[index - 1]?.to_status
      )
    ))
  ) {
    throw new Error(
      'Semantic metric waiver history is not a contiguous append-only chain.',
    );
  }
  const last = events.at(-1);
  const statusMatchesHead = (
    last?.to_status === head.status
    || (
      head.status === 'expired'
      && head.expire_reason === 'scheduled_expiry'
      && last?.to_status === 'approved'
    )
  );
  if (
    last?.event_id !== head.last_event_id
    || last.event_type !== head.last_event_type
    || !statusMatchesHead
    || last.waiver_digest !== head.head_digest
    || last.scope_digest !== head.scope_digest
    || Date.parse(last.occurred_at) !== Date.parse(head.last_event_at)
  ) {
    throw new Error(
      'Semantic metric waiver history does not match its authoritative head.',
    );
  }
  return events;
}

export function parseRequestedSemanticWaiverResponse(
  value: RequestedSemanticWaiverResponse | unknown,
): SemanticWaiverMutationResult {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver request response is invalid.');
  }
  exactFields(
    value,
    ['waiver_id', 'status', 'scope_digest'],
    'request response',
  );
  if (value.status !== 'requested') {
    throw new Error('Semantic metric waiver request status is invalid.');
  }
  return {
    action: 'request',
    waiverId: requiredText(value.waiver_id, 'request identity'),
    waiverRevision: 1,
    status: 'requested',
    replayed: false,
    scopeDigest: sha256(value.scope_digest, 'request scope digest'),
  };
}

export function parseReviewedSemanticWaiverResponse(
  value: ReviewedSemanticWaiverResponse | unknown,
  expected: {
    waiverId: string;
    previousRevision: number;
    action: 'approve' | 'reject';
  },
): SemanticWaiverMutationResult {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver review response is invalid.');
  }
  const expectedStatus = expected.action === 'approve'
    ? 'approved'
    : 'rejected';
  exactFields(
    value,
    [
      'waiver_id',
      'waiver_revision',
      'status',
      'reviewer_id',
      'replayed',
    ],
    'review response',
  );
  if (
    value.waiver_id !== expected.waiverId
    || value.waiver_revision !== expected.previousRevision + 1
    || value.status !== expectedStatus
    || typeof value.replayed !== 'boolean'
  ) {
    throw new Error('Semantic metric waiver review response is inconsistent.');
  }
  return {
    action: expected.action,
    waiverId: expected.waiverId,
    waiverRevision: value.waiver_revision,
    status: expectedStatus,
    replayed: value.replayed,
    actorId: requiredText(value.reviewer_id, 'reviewer identity'),
  };
}

export function parseRevokedSemanticWaiverResponse(
  value: RevokedSemanticWaiverResponse | unknown,
  expected: {
    waiverId: string;
    previousRevision: number;
  },
): SemanticWaiverMutationResult {
  if (!isRecord(value)) {
    throw new Error('Semantic metric waiver revoke response is invalid.');
  }
  exactFields(
    value,
    ['waiver_id', 'waiver_revision', 'status', 'replayed'],
    'revoke response',
  );
  if (
    value.waiver_id !== expected.waiverId
    || value.waiver_revision !== expected.previousRevision + 1
    || value.status !== 'revoked'
    || typeof value.replayed !== 'boolean'
  ) {
    throw new Error('Semantic metric waiver revoke response is inconsistent.');
  }
  return {
    action: 'revoke',
    waiverId: expected.waiverId,
    waiverRevision: value.waiver_revision,
    status: 'revoked',
    replayed: value.replayed,
  };
}

export function parseRevalidatedSemanticWaiverResponse(
  value: RevalidatedSemanticWaiverResponse | unknown,
  expected: {
    waiverId: string;
    previousRevision: number;
  },
): SemanticWaiverMutationResult {
  if (!isRecord(value)) {
    throw new Error(
      'Semantic metric waiver revalidation response is invalid.',
    );
  }
  exactFields(
    value,
    [
      'waiver_id',
      'waiver_revision',
      'status',
      'current',
      'reason_code',
      'replayed',
    ],
    'revalidation response',
  );
  if (
    value.waiver_id !== expected.waiverId
    || value.waiver_revision !== expected.previousRevision + 1
    || typeof value.status !== 'string'
    || !REVALIDATION_STATUSES.has(
      value.status as SemanticMetricWaiverRevalidationStatus,
    )
    || typeof value.current !== 'boolean'
    || typeof value.reason_code !== 'string'
    || !REVALIDATION_REASONS.has(
      value.reason_code as SemanticMetricWaiverRevalidationReason,
    )
    || typeof value.replayed !== 'boolean'
    || value.current !== (value.status === 'approved')
    || (value.status === 'approved' && value.reason_code !== 'current')
    || (
      value.status === 'expired'
      && value.reason_code !== 'scheduled_expiry'
    )
    || (value.status === 'revoked' && value.reason_code !== 'revoked')
    || (value.status === 'anchor_stale' && (
      value.reason_code === 'current'
      || value.reason_code === 'scheduled_expiry'
      || value.reason_code === 'revoked'
    ))
  ) {
    throw new Error(
      'Semantic metric waiver revalidation response is inconsistent.',
    );
  }
  return {
    action: 'revalidate',
    waiverId: expected.waiverId,
    waiverRevision: value.waiver_revision,
    status: value.status as SemanticMetricWaiverRevalidationStatus,
    current: value.current,
    reasonCode:
      value.reason_code as SemanticMetricWaiverRevalidationReason,
    replayed: value.replayed,
  };
}

export function policyWaiverExpireReasonLabel(
  value: SemanticMetricWaiverExpireReason,
): string {
  return formatPolicyToken(value);
}

export function policyWaiverErrorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    if (
      error.details.reason_code
      === 'semantic_waiver_independent_review_required'
      || error.details.reason_code
      === 'policy_waiver_independent_reviewer_required'
    ) {
      return (
        'Reviewer separation blocked this action. The requester and the '
        + 'assessment author must use a different authorized reviewer.'
      );
    }
    if (error.status === 409 || error.kind === 'conflict') {
      const next = error.nextAction
        ? ` ${error.nextAction}.`
        : ' Refresh the waiver head before retrying.';
      return `The waiver revision or semantic anchor changed.${next}`;
    }
  }
  return policyUiErrorMessage(error);
}

export function classifyPolicyWaiverCursorError(
  error: unknown,
): CursorCollectionError {
  if (
    error instanceof PolicyGovernanceApiError
    && error.kind === 'invalid_cursor'
  ) {
    return {
      message:
        'This cursor expired or no longer matches the waiver snapshot.',
      restartRequired: true,
    };
  }
  return {
    message: policyWaiverErrorMessage(error),
    restartRequired: false,
  };
}
