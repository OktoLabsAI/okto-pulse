import {
  describe,
  expect,
  it,
} from 'vitest';

import type {
  SemanticWaiverEvent,
  SemanticWaiverFull,
} from '@/types/policy-governance';

import {
  emptySemanticEvidenceDraft,
  parseRequestedSemanticWaiverResponse,
  parseReviewedSemanticWaiverResponse,
  parseRevokedSemanticWaiverResponse,
  parseRevalidatedSemanticWaiverResponse,
  parseSemanticEvidenceDrafts,
  parseSemanticWaiverHeadResponse,
  validatedSemanticWaiverEvents,
  validatedSemanticWaiverPage,
} from '../policyWaiverModel';

const digest = (character: string) => character.repeat(64);

function waiver(
  overrides: Partial<SemanticWaiverFull> = {},
): SemanticWaiverFull {
  return {
    projection: 'full',
    waiver_id: 'waiver-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    lifecycle_state: 'history_only',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'revision-1',
    binding_id: 'binding-1',
    metric_id: 'metric-1',
    metric_code: 'Architecture.Segregation:v2',
    status: 'requested',
    waiver_revision: 1,
    currentness: 'current',
    currentness_reasons: [],
    requested_at: '2026-07-30T09:00:00Z',
    expires_at: '2099-08-30T12:00:00Z',
    last_event_type: 'request',
    last_event_at: '2026-07-30T09:00:00Z',
    justification: 'Temporary semantic exception.',
    requested_by: 'requester-1',
    original_expires_at: '2099-08-30T12:00:00Z',
    reviewed_by: null,
    reviewed_at: null,
    review_reason: null,
    revoked_by: null,
    revoked_at: null,
    expire_reason: null,
    evidence_refs: [{
      source_type: 'spec',
      source_id: 'spec-1',
      source_version: 7,
      content_hash: digest('a'),
    }],
    metric_result_id: 'metric-result-1',
    metric_result_digest: digest('b'),
    finding_digest: digest('c'),
    receipt_digest: digest('d'),
    subject_content_digest: digest('e'),
    guideline_revision_digest: digest('f'),
    binding_revision: 3,
    binding_configuration_digest: digest('1'),
    scope_digest: digest('2'),
    head_digest: digest('3'),
    last_event_id: 'event-1',
    last_event_idempotency_key: 'request-key-1',
    assessment_assessor_id: 'assessment-agent-1',
    last_revalidation_status: null,
    last_revalidation_current: null,
    last_revalidation_reason_code: null,
    last_revalidation_evaluated_at: null,
    last_revalidation_currentness_reasons: [],
    last_revalidation_scheduled_expiry_observed: false,
    ...overrides,
  };
}

function waiverEvent(
  overrides: Partial<SemanticWaiverEvent> = {},
): SemanticWaiverEvent {
  return {
    event_id: 'event-1',
    predecessor_event_id: null,
    waiver_id: 'waiver-1',
    waiver_revision: 1,
    event_type: 'request',
    from_status: null,
    to_status: 'requested',
    actor_id: 'requester-1',
    occurred_at: '2026-07-30T09:00:00Z',
    reason: 'Temporary semantic exception.',
    evidence_refs: [{
      source_type: 'spec',
      source_id: 'spec-1',
      source_version: 7,
      content_hash: digest('a'),
    }],
    expires_at: '2099-08-30T12:00:00Z',
    scope_digest: digest('2'),
    waiver_digest: digest('3'),
    idempotency_key: 'request-key-1',
    request_digest: digest('4'),
    expire_reason: null,
    evaluated_at: null,
    revalidation_status: null,
    revalidation_current: null,
    revalidation_reason_code: null,
    currentness_reasons: [],
    scheduled_expiry_observed: false,
    ...overrides,
  };
}

function page(
  items: SemanticWaiverFull[],
  nextCursor: string | null = null,
) {
  return {
    items,
    projection: 'full' as const,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

describe('semantic metric waiver validation', () => {
  it('parses structured evidence and rejects incomplete or duplicate refs', () => {
    expect(parseSemanticEvidenceDrafts([{
      sourceType: 'spec',
      sourceId: 'spec-1',
      sourceVersion: '7',
      contentHash: digest('A'),
    }])).toEqual([{
      source_type: 'spec',
      source_id: 'spec-1',
      source_version: 7,
      content_hash: digest('a'),
    }]);
    expect(parseSemanticEvidenceDrafts([
      emptySemanticEvidenceDraft(),
    ])).toBeNull();
    const duplicate = {
      sourceType: 'spec',
      sourceId: 'spec-1',
      sourceVersion: '7',
      contentHash: digest('a'),
    };
    expect(parseSemanticEvidenceDrafts([
      duplicate,
      duplicate,
    ])).toBeNull();
  });

  it('accepts an exact full page and keeps its opaque continuation', () => {
    expect(validatedSemanticWaiverPage(
      page([waiver()], 'opaque/do-not-parse'),
      'board-1',
      {
        status: 'requested',
        entityType: 'spec',
        subjectId: 'spec-1',
        metricResultId: 'metric-result-1',
        findingId: 'finding-1',
        receiptId: 'receipt-1',
      },
    )).toMatchObject({
      items: [expect.objectContaining({
        metric_code: 'Architecture.Segregation:v2',
        metric_result_id: 'metric-result-1',
        currentness: 'current',
      })],
      has_more: true,
      next_cursor: 'opaque/do-not-parse',
    });
  });

  it('rejects unknown response fields, filter drift and keyset reordering', () => {
    expect(() => validatedSemanticWaiverPage({
      ...page([waiver()]),
      legacy_rules: [],
    }, 'board-1')).toThrow(/unknown or missing field/i);
    expect(() => validatedSemanticWaiverPage(
      page([waiver()]),
      'board-1',
      { metricResultId: 'other-result' },
    )).toThrow(/cross-filter evidence/i);
    expect(() => validatedSemanticWaiverPage(page([
      waiver({
        waiver_id: 'waiver-older',
        requested_at: '2026-07-30T08:00:00Z',
      }),
      waiver({
        waiver_id: 'waiver-newer',
        requested_at: '2026-07-30T10:00:00Z',
        last_event_at: '2026-07-30T10:00:00Z',
      }),
    ]), 'board-1')).toThrow(/newest-first/i);
  });

  it('parses the exact head wrapper and rejects partial revalidation state', () => {
    expect(parseSemanticWaiverHeadResponse(
      { waiver: waiver() },
      {
        boardId: 'board-1',
        evaluatedAt: '2026-07-30T12:00:00Z',
        waiverId: 'waiver-1',
        metricResultId: 'metric-result-1',
      },
    ).metric_code).toBe('Architecture.Segregation:v2');
    expect(() => parseSemanticWaiverHeadResponse(
      {
        waiver: waiver({
          last_revalidation_status: 'approved',
          last_revalidation_current: null,
        }),
      },
      {
        boardId: 'board-1',
        evaluatedAt: '2026-07-30T12:00:00Z',
        waiverId: 'waiver-1',
      },
    )).toThrow(/revalidation evidence is invalid/i);
    expect(() => parseSemanticWaiverHeadResponse(
      { waiver: waiver(), ignored: true },
      {
        boardId: 'board-1',
        evaluatedAt: '2026-07-30T12:00:00Z',
        waiverId: 'waiver-1',
      },
    )).toThrow(/malformed response/i);
  });

  it('fails closed when detail expiry disagrees with evaluated_at', () => {
    const approved = waiver({
      status: 'approved',
      waiver_revision: 2,
      expires_at: '2026-07-30T11:00:00Z',
      original_expires_at: '2026-07-30T11:00:00Z',
      last_event_type: 'approve',
      last_event_at: '2026-07-30T10:00:00Z',
      reviewed_by: 'reviewer-1',
      reviewed_at: '2026-07-30T10:00:00Z',
      review_reason: 'Approved as a bounded exception.',
      last_event_id: 'event-2',
      last_event_idempotency_key: 'approve-key-1',
    });
    const expected = {
      boardId: 'board-1',
      evaluatedAt: '2026-07-30T12:00:00Z',
      waiverId: 'waiver-1',
    };
    expect(() => parseSemanticWaiverHeadResponse(
      { waiver: approved },
      expected,
    )).toThrow(/expiry does not match its evaluation snapshot/i);
    expect(parseSemanticWaiverHeadResponse(
      {
        waiver: {
          ...approved,
          status: 'expired',
          expire_reason: 'scheduled_expiry',
        },
      },
      expected,
    ).status).toBe('expired');
  });

  it('verifies a contiguous append-only event chain against the head', () => {
    const approvedHead = waiver({
      status: 'approved',
      waiver_revision: 2,
      last_event_id: 'event-2',
      last_event_type: 'approve',
      last_event_at: '2026-07-30T10:00:00Z',
      head_digest: digest('5'),
      reviewed_by: 'reviewer-1',
      reviewed_at: '2026-07-30T10:00:00Z',
      review_reason: 'Approved independently.',
    });
    const approvedEvent = waiverEvent({
      event_id: 'event-2',
      predecessor_event_id: 'event-1',
      waiver_revision: 2,
      event_type: 'approve',
      from_status: 'requested',
      to_status: 'approved',
      actor_id: 'reviewer-1',
      occurred_at: '2026-07-30T10:00:00Z',
      reason: 'Approved independently.',
      waiver_digest: digest('5'),
      idempotency_key: 'review-key-1',
      request_digest: digest('6'),
    });
    expect(validatedSemanticWaiverEvents({
      events: [waiverEvent(), approvedEvent],
    }, approvedHead)).toHaveLength(2);
    expect(validatedSemanticWaiverEvents({
      events: [waiverEvent(), approvedEvent],
    }, {
      ...approvedHead,
      status: 'expired',
      expires_at: '2026-07-30T11:00:00Z',
      original_expires_at: '2026-07-30T11:00:00Z',
      expire_reason: 'scheduled_expiry',
    })).toHaveLength(2);
    expect(() => validatedSemanticWaiverEvents({
      events: [
        waiverEvent(),
        { ...approvedEvent, predecessor_event_id: 'wrong' },
      ],
    }, approvedHead)).toThrow(/contiguous append-only chain/i);
  });

  it('accepts only exact flat mutation responses with monotonic revisions', () => {
    expect(parseRequestedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      status: 'requested',
      scope_digest: digest('2'),
    })).toMatchObject({ action: 'request', waiverRevision: 1 });
    expect(parseReviewedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      waiver_revision: 2,
      status: 'approved',
      reviewer_id: 'reviewer-1',
      replayed: false,
    }, {
      waiverId: 'waiver-1',
      previousRevision: 1,
      action: 'approve',
    })).toMatchObject({ status: 'approved', waiverRevision: 2 });
    expect(parseRevokedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      waiver_revision: 3,
      status: 'revoked',
      replayed: false,
    }, {
      waiverId: 'waiver-1',
      previousRevision: 2,
    })).toMatchObject({ status: 'revoked', waiverRevision: 3 });
    expect(() => parseReviewedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      waiver_revision: 2,
      status: 'approved',
      reviewer_id: 'reviewer-1',
      replayed: false,
      legacy_event: {},
    }, {
      waiverId: 'waiver-1',
      previousRevision: 1,
      action: 'approve',
    })).toThrow(/unknown or missing field/i);
  });

  it('validates revalidation status/current/reason as one closed decision', () => {
    expect(parseRevalidatedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      waiver_revision: 3,
      status: 'anchor_stale',
      current: false,
      reason_code: 'metric_result_changed',
      replayed: false,
    }, {
      waiverId: 'waiver-1',
      previousRevision: 2,
    })).toMatchObject({
      status: 'anchor_stale',
      current: false,
      reasonCode: 'metric_result_changed',
    });
    expect(() => parseRevalidatedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      waiver_revision: 3,
      status: 'approved',
      current: false,
      reason_code: 'current',
      replayed: false,
    }, {
      waiverId: 'waiver-1',
      previousRevision: 2,
    })).toThrow(/inconsistent/i);
  });
});
