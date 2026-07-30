import type { CursorCollectionError } from '@/hooks/useOpaqueCursorCollection';
import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';
import type {
  NonEmptyArray,
  PolicyEntityType,
  PolicyWaiver,
  PolicyWaiverEvent,
  PolicyWaiverEventType,
  PolicyWaiverExpireReasonCode,
  PolicyWaiverListItem,
  PolicyWaiverStatus,
} from '@/types/policy-governance';

import {
  formatPolicyToken,
  policyUiErrorMessage,
} from './policyComplianceModel';

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
const EXPIRE_REASONS = new Set<PolicyWaiverExpireReasonCode>([
  'scheduled_expiry',
  'subject_scope_changed',
  'guideline_revision_changed',
  'guideline_rule_changed',
]);

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

export function policyWaiverExpireReasonLabel(
  value: PolicyWaiverExpireReasonCode,
): string {
  return formatPolicyToken(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

function isTimestamp(value: unknown): value is string {
  return (
    isNonEmptyString(value)
    && !Number.isNaN(new Date(value).getTime())
  );
}

function isOptionalTimestamp(value: unknown): value is string | undefined {
  return value === undefined || isTimestamp(value);
}

function isOptionalString(value: unknown): value is string | undefined {
  return value === undefined || isNonEmptyString(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isNonEmptyString(value);
}

function isNullableTimestamp(value: unknown): value is string | null {
  return value === null || isTimestamp(value);
}

function isEvidenceRefs(value: unknown): value is string[] {
  return (
    Array.isArray(value)
    && value.every(isNonEmptyString)
    && new Set(value).size === value.length
  );
}

function isSubject(value: unknown, boardId: string): boolean {
  if (!isRecord(value)) return false;
  return (
    value.board_id === boardId
    && ENTITY_TYPES.has(value.entity_type as PolicyEntityType)
    && isNonEmptyString(value.subject_id)
    && isPositiveInteger(value.subject_version)
  );
}

function hasValidExpireReason(
  value: unknown,
): value is PolicyWaiverExpireReasonCode | undefined {
  return (
    value === undefined
    || EXPIRE_REASONS.has(value as PolicyWaiverExpireReasonCode)
  );
}

function eventTransitionIsValid(
  event: Pick<
    PolicyWaiverEvent,
    'event_type' | 'from_status' | 'to_status' | 'waiver_revision'
  >,
): boolean {
  switch (event.event_type) {
    case 'request':
      return (
        event.waiver_revision === 1
        && event.from_status === null
        && event.to_status === 'requested'
      );
    case 'approve':
      return (
        event.from_status === 'requested'
        && event.to_status === 'approved'
      );
    case 'reject':
      return (
        event.from_status === 'requested'
        && event.to_status === 'rejected'
      );
    case 'revoke':
      return (
        event.from_status === 'approved'
        && event.to_status === 'revoked'
      );
    case 'expire':
      return (
        event.from_status === 'approved'
        && event.to_status === 'expired'
      );
    case 'revalidate':
      return (
        (
          event.from_status === 'approved'
          || event.from_status === 'expired'
        )
        && event.to_status === 'approved'
      );
  }
}

export function isPolicyWaiverListItemForBoard(
  value: PolicyWaiverListItem | unknown,
  boardId: string,
): value is PolicyWaiverListItem {
  if (!isRecord(value)) return false;
  const projection = value.projection;
  const baseValid = (
    (projection === 'summary' || projection === 'detail')
    && isNonEmptyString(value.waiver_id)
    && value.board_id === boardId
    && isNonEmptyString(value.finding_id)
    && isNonEmptyString(value.receipt_id)
    && isNonEmptyString(value.guideline_id)
    && isNonEmptyString(value.revision_id)
    && isNonEmptyString(value.rule_id)
    && isSubject(value.subject, boardId)
    && WAIVER_STATUSES.has(value.status as PolicyWaiverStatus)
    && typeof value.source_current === 'boolean'
    && typeof value.effective === 'boolean'
    && isNonEmptyString(value.requested_by)
    && isTimestamp(value.requested_at)
    && isTimestamp(value.expires_at)
    && isPositiveInteger(value.waiver_revision)
    && isTimestamp(value.last_event_at)
    && hasValidExpireReason(value.expire_reason_code)
  );
  if (!baseValid) return false;
  if (
    (value.status === 'expired')
    !== (value.expire_reason_code !== undefined)
  ) {
    return false;
  }
  if (
    value.effective === true
    && (
      value.status !== 'approved'
      || value.source_current !== true
    )
  ) {
    return false;
  }
  if (projection === 'summary') {
    return [
      'justification',
      'evidence_refs',
      'reviewed_by',
      'reviewed_at',
      'review_reason',
      'revoked_by',
      'revoked_at',
    ].every((field) => !(field in value));
  }
  return (
    typeof value.justification === 'string'
    && value.justification.trim().length > 0
    && isEvidenceRefs(value.evidence_refs)
    && isOptionalString(value.reviewed_by)
    && isOptionalTimestamp(value.reviewed_at)
    && isOptionalString(value.review_reason)
    && isOptionalString(value.revoked_by)
    && isOptionalTimestamp(value.revoked_at)
  );
}

export function validatedPolicyWaiverPage(
  value: unknown,
  boardId: string,
  expected: {
    projection?: 'summary' | 'detail';
    status?: PolicyWaiverStatus;
    entityType?: PolicyEntityType;
    subjectId?: string;
  } = {},
): {
  items: PolicyWaiverListItem[];
  limit: number;
  has_more: boolean;
  next_cursor?: string;
} {
  if (!isRecord(value)) {
    throw new Error('Policy waiver list returned a malformed cursor page.');
  }
  const items = value.items;
  const limit = value.limit;
  const hasMore = value.has_more;
  const nextCursor = value.next_cursor;
  if (
    !Array.isArray(items)
    || !Number.isInteger(limit)
    || Number(limit) < 1
    || Number(limit) > 200
    || items.length > Number(limit)
    || typeof hasMore !== 'boolean'
    || (
      hasMore
      && (
        items.length === 0
        || !isNonEmptyString(nextCursor)
      )
    )
    || (!hasMore && nextCursor !== undefined)
    || !items.every((item) =>
      isPolicyWaiverListItemForBoard(item, boardId),
    )
    || !items.every((item) => {
      const waiver = item as PolicyWaiverListItem;
      return (
        (
          expected.projection === undefined
          || waiver.projection === expected.projection
        )
        && (
          expected.status === undefined
          || waiver.status === expected.status
        )
        && (
          expected.entityType === undefined
          || waiver.subject.entity_type === expected.entityType
        )
        && (
          expected.subjectId === undefined
          || waiver.subject.subject_id === expected.subjectId
        )
      );
    })
  ) {
    throw new Error(
      'Policy waiver list returned malformed or cross-board evidence.',
    );
  }
  for (let index = 1; index < items.length; index += 1) {
    const previous = items[index - 1] as PolicyWaiverListItem;
    const current = items[index] as PolicyWaiverListItem;
    const previousTime = new Date(previous.requested_at).getTime();
    const currentTime = new Date(current.requested_at).getTime();
    if (
      currentTime > previousTime
      || (
        currentTime === previousTime
        && current.waiver_id.localeCompare(previous.waiver_id) > 0
      )
    ) {
      throw new Error(
        'Policy waiver list violated its newest-first keyset order.',
      );
    }
  }
  return {
    items: items as PolicyWaiverListItem[],
    limit: Number(limit),
    has_more: hasMore,
    ...(hasMore ? { next_cursor: nextCursor as string } : {}),
  };
}

export function isPolicyWaiverForExpectedScope(
  value: PolicyWaiver | unknown,
  expected: {
    boardId: string;
    waiverId?: string;
    findingId?: string;
  },
): value is PolicyWaiver {
  if (!isRecord(value)) return false;
  const baseValid = (
    isNonEmptyString(value.waiver_id)
    && (
      expected.waiverId === undefined
      || value.waiver_id === expected.waiverId
    )
    && value.board_id === expected.boardId
    && (
      expected.findingId === undefined
      || value.finding_id === expected.findingId
    )
    && isNonEmptyString(value.finding_id)
    && isNonEmptyString(value.receipt_id)
    && isNonEmptyString(value.guideline_id)
    && isNonEmptyString(value.revision_id)
    && isNonEmptyString(value.rule_id)
    && isSubject(value.subject, expected.boardId)
    && WAIVER_STATUSES.has(value.status as PolicyWaiverStatus)
    && typeof value.justification === 'string'
    && value.justification.trim().length > 0
    && isEvidenceRefs(value.evidence_refs)
    && isNonEmptyString(value.requested_by)
    && isTimestamp(value.requested_at)
    && isPositiveInteger(value.waiver_revision)
    && isTimestamp(value.expires_at)
    && isNonEmptyString(value.last_event_id)
    && EVENT_TYPES.has(value.last_event_type as PolicyWaiverEventType)
    && isTimestamp(value.last_event_at)
    && isNullableString(value.reviewed_by)
    && isNullableTimestamp(value.reviewed_at)
    && (
      value.review_reason === null
      || typeof value.review_reason === 'string'
    )
    && isNullableString(value.revoked_by)
    && isNullableTimestamp(value.revoked_at)
    && (
      value.expire_reason_code === null
      || EXPIRE_REASONS.has(
        value.expire_reason_code as PolicyWaiverExpireReasonCode,
      )
    )
  );
  if (!baseValid) return false;
  const status = value.status as PolicyWaiverStatus;
  const eventType = value.last_event_type as PolicyWaiverEventType;
  const hasReview = (
    isNonEmptyString(value.reviewed_by)
    && isTimestamp(value.reviewed_at)
    && typeof value.review_reason === 'string'
    && value.review_reason.trim().length > 0
  );
  const hasRevocation = (
    isNonEmptyString(value.revoked_by)
    && isTimestamp(value.revoked_at)
  );
  if (status === 'requested') {
    return (
      eventType === 'request'
      && !hasReview
      && !hasRevocation
      && value.expire_reason_code === null
    );
  }
  if (status === 'approved') {
    return (
      (eventType === 'approve' || eventType === 'revalidate')
      && hasReview
      && !hasRevocation
      && value.expire_reason_code === null
    );
  }
  if (status === 'rejected') {
    return (
      eventType === 'reject'
      && hasReview
      && !hasRevocation
      && value.expire_reason_code === null
    );
  }
  if (status === 'revoked') {
    return (
      eventType === 'revoke'
      && hasReview
      && hasRevocation
      && value.expire_reason_code === null
    );
  }
  return (
    eventType === 'expire'
    && hasReview
    && !hasRevocation
    && value.expire_reason_code !== null
  );
}

function isPolicyWaiverEventForScope(
  value: PolicyWaiverEvent | unknown,
  expected: {
    boardId: string;
    waiverId: string;
  },
): value is PolicyWaiverEvent {
  if (!isRecord(value)) return false;
  const baseValid = (
    isNonEmptyString(value.event_id)
    && value.waiver_id === expected.waiverId
    && value.board_id === expected.boardId
    && isPositiveInteger(value.waiver_revision)
    && EVENT_TYPES.has(value.event_type as PolicyWaiverEventType)
    && (
      value.from_status === null
      || WAIVER_STATUSES.has(value.from_status as PolicyWaiverStatus)
    )
    && WAIVER_STATUSES.has(value.to_status as PolicyWaiverStatus)
    && isNonEmptyString(value.actor_id)
    && isTimestamp(value.occurred_at)
    && typeof value.reason === 'string'
    && value.reason.trim().length > 0
    && isEvidenceRefs(value.evidence_refs)
    && isTimestamp(value.expires_at)
    && typeof value.scope_digest === 'string'
    && /^[0-9a-f]{64}$/.test(value.scope_digest)
    && (
      value.expire_reason_code === null
      || EXPIRE_REASONS.has(
        value.expire_reason_code as PolicyWaiverExpireReasonCode,
      )
    )
  );
  if (!baseValid) return false;
  const event = value as unknown as PolicyWaiverEvent;
  return (
    eventTransitionIsValid(event)
    && (
      (event.event_type === 'expire')
      === (event.expire_reason_code !== null)
    )
  );
}

export function validatedPolicyWaiverEvents(
  value: unknown,
  expected: {
    boardId: string;
    waiverId: string;
    headRevision: number;
    headEventId: string;
  },
): PolicyWaiverEvent[] {
  if (
    !isRecord(value)
    || !Array.isArray(value.events)
    || !value.events.every((event) =>
      isPolicyWaiverEventForScope(event, expected),
    )
  ) {
    throw new Error(
      'Policy waiver history returned malformed or cross-scope evidence.',
    );
  }
  const events = value.events as PolicyWaiverEvent[];
  if (
    events.length !== expected.headRevision
    || events.some(
      (event, index) => event.waiver_revision !== index + 1,
    )
    || events.some(
      (event, index) => (
        index > 0
        && event.from_status !== events[index - 1]?.to_status
      ),
    )
    || events.at(-1)?.event_id !== expected.headEventId
  ) {
    throw new Error(
      'Policy waiver history does not match the append-only head.',
    );
  }
  return events;
}

export function validatedPolicyWaiverMutation(
  value: unknown,
  expected: {
    boardId: string;
    waiverId?: string;
    findingId: string;
    previousRevision: number;
    eventType: PolicyWaiverEventType;
    reason?: string;
    evidenceRefs?: readonly string[];
    expiresAt?: string;
  },
): {
  waiver: PolicyWaiver;
  event: PolicyWaiverEvent;
} {
  if (!isRecord(value)) {
    throw new Error('Policy waiver mutation returned a malformed response.');
  }
  const waiver = value.waiver;
  const event = value.event;
  if (
    !isPolicyWaiverForExpectedScope(waiver, expected)
    || !isPolicyWaiverEventForScope(event, {
      boardId: expected.boardId,
      waiverId: waiver.waiver_id,
    })
    || waiver.waiver_revision !== expected.previousRevision + 1
    || event.waiver_revision !== waiver.waiver_revision
    || event.event_type !== expected.eventType
    || event.event_id !== waiver.last_event_id
    || event.to_status !== waiver.status
    || new Date(event.expires_at).getTime()
      !== new Date(waiver.expires_at).getTime()
    || (
      expected.reason !== undefined
      && event.reason !== expected.reason
    )
    || (
      expected.evidenceRefs !== undefined
      && (
        event.evidence_refs.length !== expected.evidenceRefs.length
        || event.evidence_refs.some(
          (reference, index) =>
            reference !== expected.evidenceRefs?.[index],
        )
      )
    )
    || (
      expected.expiresAt !== undefined
      && new Date(waiver.expires_at).getTime()
        !== new Date(expected.expiresAt).getTime()
    )
  ) {
    throw new Error(
      'Policy waiver mutation returned inconsistent scope or event evidence.',
    );
  }
  if (
    event.event_type === 'request'
    && (
      event.actor_id !== waiver.requested_by
      || event.reason !== waiver.justification
    )
  ) {
    throw new Error(
      'Policy waiver request evidence does not match its immutable head.',
    );
  }
  if (
    (
      event.event_type === 'approve'
      || event.event_type === 'reject'
      || event.event_type === 'revalidate'
    )
    && (
      event.actor_id === waiver.requested_by
      || event.actor_id !== waiver.reviewed_by
      || event.reason !== waiver.review_reason
    )
  ) {
    throw new Error(
      'Policy waiver mutation violated reviewer separation evidence.',
    );
  }
  if (
    event.event_type === 'revoke'
    && (
      event.actor_id !== waiver.revoked_by
      || new Date(event.occurred_at).getTime()
        !== new Date(waiver.revoked_at ?? '').getTime()
    )
  ) {
    throw new Error(
      'Policy waiver revocation evidence does not match its immutable head.',
    );
  }
  return { waiver, event };
}

export function parsePolicyEvidenceRefs(
  value: string,
): NonEmptyArray<string> | null {
  const refs = Array.from(
    new Set(
      value
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
  return refs.length > 0
    ? refs as NonEmptyArray<string>
    : null;
}

export function policyWaiverErrorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    if (
      error.details.reason_code
      === 'policy_waiver_independent_reviewer_required'
    ) {
      return (
        'Reviewer separation blocked this action. '
        + 'The requester must use a different authorized reviewer.'
      );
    }
    if (error.status === 409 || error.kind === 'conflict') {
      const next = error.nextAction
        ? ` ${error.nextAction}.`
        : ' Refresh the waiver and retry with the current revision.';
      return `The waiver changed or its scope is no longer current.${next}`;
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
