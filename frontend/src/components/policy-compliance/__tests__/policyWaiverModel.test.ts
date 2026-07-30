import { describe, expect, it } from 'vitest';

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import type {
  PolicyWaiver,
  PolicyWaiverEvent,
  PolicyWaiverSummary,
} from '@/types/policy-governance';

import {
  classifyPolicyWaiverCursorError,
  isPolicyWaiverForExpectedScope,
  isPolicyWaiverListItemForBoard,
  parsePolicyEvidenceRefs,
  policyWaiverErrorMessage,
  validatedPolicyWaiverEvents,
  validatedPolicyWaiverMutation,
  validatedPolicyWaiverPage,
} from '../policyWaiverModel';

const digest = (character: string) => character.repeat(64);

function summary(
  overrides: Partial<PolicyWaiverSummary> = {},
): PolicyWaiverSummary {
  return {
    projection: 'summary',
    waiver_id: 'server-waiver-1',
    board_id: 'board-1',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    guideline_id: 'guideline-1',
    revision_id: 'revision-1',
    rule_id: 'rule-1',
    subject: {
      board_id: 'board-1',
      entity_type: 'spec',
      subject_id: 'spec-1',
      subject_version: 4,
    },
    status: 'requested',
    source_current: true,
    effective: false,
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    expires_at: '2026-08-30T09:00:00Z',
    waiver_revision: 1,
    last_event_at: '2026-07-30T09:00:00Z',
    ...overrides,
  };
}

function head(
  overrides: Partial<PolicyWaiver> = {},
): PolicyWaiver {
  return {
    waiver_id: 'server-waiver-1',
    board_id: 'board-1',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    guideline_id: 'guideline-1',
    revision_id: 'revision-1',
    rule_id: 'rule-1',
    subject: {
      board_id: 'board-1',
      entity_type: 'spec',
      subject_id: 'spec-1',
      subject_version: 4,
    },
    status: 'requested',
    justification: 'Temporary exception requested.',
    evidence_refs: ['ticket://one'],
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    waiver_revision: 1,
    expires_at: '2026-08-30T09:00:00Z',
    last_event_id: 'server-event-1',
    last_event_type: 'request',
    last_event_at: '2026-07-30T09:00:00Z',
    reviewed_by: null,
    reviewed_at: null,
    review_reason: null,
    revoked_by: null,
    revoked_at: null,
    expire_reason_code: null,
    ...overrides,
  };
}

function event(
  overrides: Partial<PolicyWaiverEvent> = {},
): PolicyWaiverEvent {
  return {
    event_id: 'server-event-1',
    waiver_id: 'server-waiver-1',
    board_id: 'board-1',
    waiver_revision: 1,
    event_type: 'request',
    from_status: null,
    to_status: 'requested',
    actor_id: 'requester-1',
    occurred_at: '2026-07-30T09:00:00Z',
    reason: 'Temporary exception requested.',
    evidence_refs: ['ticket://one'],
    expires_at: '2026-08-30T09:00:00Z',
    scope_digest: digest('a'),
    expire_reason_code: null,
    ...overrides,
  };
}

describe('policyWaiverModel', () => {
  it('normalizes evidence references and requires a non-empty result', () => {
    expect(
      parsePolicyEvidenceRefs(
        ' ticket://one \n\nreceipt://two\nticket://one ',
      ),
    ).toEqual(['ticket://one', 'receipt://two']);
    expect(parsePolicyEvidenceRefs(' \n ')).toBeNull();
  });

  it('keeps summary and detail projections closed and enforces status invariants', () => {
    expect(isPolicyWaiverListItemForBoard(summary(), 'board-1')).toBe(true);
    expect(
      isPolicyWaiverListItemForBoard(
        { ...summary(), justification: 'leaked detail' },
        'board-1',
      ),
    ).toBe(false);
    expect(
      isPolicyWaiverListItemForBoard(
        summary({ effective: true }),
        'board-1',
      ),
    ).toBe(false);
    expect(
      isPolicyWaiverListItemForBoard(
        summary({
          status: 'expired',
          source_current: true,
          expire_reason_code: 'scheduled_expiry',
        }),
        'board-1',
      ),
    ).toBe(true);
    expect(
      isPolicyWaiverListItemForBoard(
        summary({ status: 'expired' }),
        'board-1',
      ),
    ).toBe(false);
    expect(
      isPolicyWaiverListItemForBoard(
        summary({ expire_reason_code: 'scheduled_expiry' }),
        'board-1',
      ),
    ).toBe(false);
  });

  it('validates active filters, board scope, newest-first order and terminal cursor shape', () => {
    const second = summary({
      waiver_id: 'server-waiver-0',
      requested_at: '2026-07-30T08:00:00Z',
    });
    expect(
      validatedPolicyWaiverPage(
        {
          items: [summary(), second],
          limit: 25,
          has_more: false,
        },
        'board-1',
        {
          status: 'requested',
          entityType: 'spec',
          subjectId: 'spec-1',
        },
      ).items,
    ).toHaveLength(2);
    expect(() =>
      validatedPolicyWaiverPage(
        {
          items: [
            summary({
              subject: {
                board_id: 'board-1',
                entity_type: 'card',
                subject_id: 'spec-1',
                subject_version: 4,
              },
            }),
          ],
          limit: 25,
          has_more: false,
        },
        'board-1',
        { entityType: 'spec' },
      ),
    ).toThrow(/malformed or cross-board/);
    expect(() =>
      validatedPolicyWaiverPage(
        {
          items: [second, summary()],
          limit: 25,
          has_more: false,
        },
        'board-1',
      ),
    ).toThrow(/newest-first/);
    expect(() =>
      validatedPolicyWaiverPage(
        {
          items: [summary()],
          limit: 25,
          has_more: false,
          next_cursor: 'opaque-must-not-exist',
        },
        'board-1',
      ),
    ).toThrow(/malformed/);
    expect(() =>
      validatedPolicyWaiverPage(
        {
          items: [{
            ...summary(),
            projection: 'detail',
            justification: 'Unexpected expanded content.',
            evidence_refs: ['ticket://one'],
          }],
          limit: 25,
          has_more: false,
        },
        'board-1',
        { projection: 'summary' },
      ),
    ).toThrow(/malformed or cross-board/);
  });

  it('validates full status semantics and every append-only event edge', () => {
    const approved = head({
      status: 'approved',
      waiver_revision: 2,
      last_event_id: 'server-event-2',
      last_event_type: 'approve',
      last_event_at: '2026-07-30T10:00:00Z',
      reviewed_by: 'reviewer-1',
      reviewed_at: '2026-07-30T10:00:00Z',
      review_reason: 'Independently accepted.',
    });
    expect(
      isPolicyWaiverForExpectedScope(approved, {
        boardId: 'board-1',
        findingId: 'finding-1',
      }),
    ).toBe(true);
    expect(
      isPolicyWaiverForExpectedScope(
        { ...approved, reviewed_by: ' ' },
        {
          boardId: 'board-1',
          waiverId: 'server-waiver-1',
        },
      ),
    ).toBe(false);

    const approve = event({
      event_id: 'server-event-2',
      waiver_revision: 2,
      event_type: 'approve',
      from_status: 'requested',
      to_status: 'approved',
      actor_id: 'reviewer-1',
      occurred_at: '2026-07-30T10:00:00Z',
      reason: 'Independently accepted.',
    });
    expect(
      validatedPolicyWaiverEvents(
        { events: [event(), approve] },
        {
          boardId: 'board-1',
          waiverId: 'server-waiver-1',
          headRevision: 2,
          headEventId: 'server-event-2',
        },
      ),
    ).toHaveLength(2);
    expect(() =>
      validatedPolicyWaiverEvents(
        {
          events: [
            event(),
            { ...approve, from_status: 'approved' },
          ],
        },
        {
          boardId: 'board-1',
          waiverId: 'server-waiver-1',
          headRevision: 2,
          headEventId: 'server-event-2',
        },
      ),
    ).toThrow(/malformed|append-only/);
  });

  it('accepts server-owned IDs in request mutations and binds the exact finding', () => {
    const result = validatedPolicyWaiverMutation(
      {
        waiver: head(),
        event: event(),
      },
      {
        boardId: 'board-1',
        findingId: 'finding-1',
        previousRevision: 0,
        eventType: 'request',
      },
    );
    expect(result.waiver.waiver_id).toBe('server-waiver-1');
    expect(result.event.event_id).toBe('server-event-1');

    expect(() =>
      validatedPolicyWaiverMutation(
        {
          waiver: head({ finding_id: 'other-finding' }),
          event: event(),
        },
        {
          boardId: 'board-1',
          findingId: 'finding-1',
          previousRevision: 0,
          eventType: 'request',
        },
      ),
    ).toThrow(/inconsistent scope/);
  });

  it('projects independent-review and conflict errors into actionable UI messages', () => {
    const separation = new PolicyGovernanceApiError({
      message: 'validation failed',
      status: 400,
      code: 'validation_failed',
      details: {
        reason_code: 'policy_waiver_independent_reviewer_required',
      },
    });
    expect(policyWaiverErrorMessage(separation)).toMatch(
      /requester must use a different authorized reviewer/i,
    );

    const conflict = new PolicyGovernanceApiError({
      message: 'conflict',
      status: 409,
      kind: 'conflict',
      code: 'conflict',
      nextAction: 'refresh_and_retry',
    });
    expect(policyWaiverErrorMessage(conflict)).toMatch(
      /changed or its scope is no longer current/i,
    );
  });

  it('classifies invalid opaque cursors as restart-only failures', () => {
    const invalidCursor = new PolicyGovernanceApiError({
      message: 'invalid cursor',
      status: 400,
      kind: 'invalid_cursor',
      code: 'invalid_cursor',
    });
    expect(classifyPolicyWaiverCursorError(invalidCursor)).toEqual({
      message:
        'This cursor expired or no longer matches the waiver snapshot.',
      restartRequired: true,
    });
  });
});
