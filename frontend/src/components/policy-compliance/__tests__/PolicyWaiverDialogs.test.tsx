import { useState } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import type {
  PolicyComplianceFindingDetail,
  PolicyWaiver,
  PolicyWaiverEvent,
  PolicyWaiverSummary,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  requestPolicyWaiver: vi.fn(),
  reviewPolicyWaiver: vi.fn(),
  revokePolicyWaiver: vi.fn(),
  revalidatePolicyWaiver: vi.fn(),
  getPolicyWaiver: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  isLoading: false,
  error: null as Error | null,
  ownerReviewRequired: false,
  allowed: new Set<string>(),
}));

vi.mock('@/services/policy-governance-api', async () => {
  const actual = await vi.importActual<
    typeof import('@/services/policy-governance-api')
  >('@/services/policy-governance-api');
  return {
    ...actual,
    usePolicyGovernanceApi: () => policyApiMock,
  };
});
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Custom',
    isLoading: permissionState.isLoading,
    error: permissionState.error,
    ownerReviewRequired: permissionState.ownerReviewRequired,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));

import {
  PolicyWaiverActionDialog,
  PolicyWaiverRequestDialog,
} from '../PolicyWaiverDialogs';

const digest = (character: string) => character.repeat(64);

function finding(): PolicyComplianceFindingDetail {
  return {
    projection: 'detail',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    guideline_id: 'guideline-1',
    revision_id: 'revision-1',
    rule_id: 'rule-1',
    subject: {
      board_id: 'board-1',
      entity_type: 'spec',
      subject_id: 'spec-1',
      subject_version: 7,
    },
    outcome: 'fail',
    enforcement: 'blocking',
    severity_rank: 50,
    blocking: true,
    created_at: '2026-07-30T09:00:00Z',
    message: 'A blocking policy finding.',
    evidence_refs: ['ticket://finding'],
  };
}

function requestedHead(
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
    subject: finding().subject,
    status: 'requested',
    justification: 'Temporary exception.',
    evidence_refs: ['ticket://finding'],
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    waiver_revision: 1,
    expires_at: '2099-08-30T12:00:00.000Z',
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

function requestedEvent(
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
    reason: 'Temporary exception.',
    evidence_refs: ['ticket://finding'],
    expires_at: '2099-08-30T12:00:00.000Z',
    scope_digest: digest('a'),
    expire_reason_code: null,
    ...overrides,
  };
}

function waiverSummary(
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
    subject: finding().subject,
    status: 'requested',
    source_current: true,
    effective: false,
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    expires_at: '2099-08-30T12:00:00.000Z',
    waiver_revision: 1,
    last_event_at: '2026-07-30T09:00:00Z',
    ...overrides,
  };
}

function approvedHead(
  revision = 2,
  eventType: 'approve' | 'revalidate' = 'approve',
): PolicyWaiver {
  return requestedHead({
    status: 'approved',
    waiver_revision: revision,
    last_event_id: `server-event-${revision}`,
    last_event_type: eventType,
    last_event_at: `2026-07-30T1${revision}:00:00Z`,
    reviewed_by: 'reviewer-1',
    reviewed_at: `2026-07-30T1${revision}:00:00Z`,
    review_reason: 'Independent approval.',
    evidence_refs: ['ticket://finding', 'review://one'],
  });
}

function transitionEvent({
  revision,
  eventType,
  fromStatus,
  expiresAt = '2099-08-30T12:00:00.000Z',
}: {
  revision: number;
  eventType: 'approve' | 'revalidate';
  fromStatus: 'requested' | 'approved';
  expiresAt?: string;
}): PolicyWaiverEvent {
  return requestedEvent({
    event_id: `server-event-${revision}`,
    waiver_revision: revision,
    event_type: eventType,
    from_status: fromStatus,
    to_status: 'approved',
    actor_id: 'reviewer-1',
    occurred_at: `2026-07-30T1${revision}:00:00Z`,
    reason: 'Independent approval.',
    evidence_refs: ['review://one'],
    expires_at: expiresAt,
  });
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function fillRequiredRequestFields() {
  fireEvent.change(screen.getByLabelText('Justification'), {
    target: { value: ' Temporary exception. ' },
  });
  fireEvent.change(screen.getByLabelText('Requested expiry'), {
    target: { value: '2099-08-30T09:00' },
  });
}

function fillActionFields(expiry?: string) {
  fireEvent.change(screen.getByLabelText(/reason/i), {
    target: { value: ' Independent decision. ' },
  });
  fireEvent.change(screen.getByRole('textbox', {
    name: /Evidence references/i,
  }), {
    target: { value: ' review://one \nreview://one' },
  });
  if (expiry) {
    fireEvent.change(screen.getByLabelText('New later expiry'), {
      target: { value: expiry },
    });
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  grant();
  policyApiMock.requestPolicyWaiver.mockResolvedValue({
    waiver: requestedHead(),
    event: requestedEvent(),
  });
});

describe('PolicyWaiverDialogs', () => {
  it('submits a normalized request without fabricating server-owned IDs', async () => {
    grant('guidelines.waiver.request');
    const onCompleted = vi.fn();
    render(
      <PolicyWaiverRequestDialog
        boardId="board-1"
        finding={finding()}
        onClose={vi.fn()}
        onCompleted={onCompleted}
      />,
    );

    expect(screen.getByTestId('policy-waiver-exact-scope')).toHaveTextContent(
      'spec-1 · v7',
    );
    expect(
      screen.getByRole('button', { name: 'Request waiver' }),
    ).toBeDisabled();
    fillRequiredRequestFields();
    fireEvent.click(screen.getByRole('button', { name: 'Request waiver' }));

    await waitFor(() =>
      expect(policyApiMock.requestPolicyWaiver).toHaveBeenCalledTimes(1),
    );
    const request = policyApiMock.requestPolicyWaiver.mock.calls[0][1];
    expect(request).toEqual({
      finding_id: 'finding-1',
      justification: 'Temporary exception.',
      evidence_refs: ['ticket://finding'],
      expires_at: expect.any(String),
      idempotency_key: expect.any(String),
    });
    expect(request).not.toHaveProperty('waiver_id');
    expect(request).not.toHaveProperty('event_id');
    await waitFor(() =>
      expect(onCompleted).toHaveBeenCalledWith(
        expect.objectContaining({
          waiver: expect.objectContaining({
            waiver_id: 'server-waiver-1',
          }),
        }),
      ),
    );
  });

  it('keeps one idempotency key for an unchanged network retry', async () => {
    grant('guidelines.waiver.request');
    policyApiMock.requestPolicyWaiver
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce({
        waiver: requestedHead(),
        event: requestedEvent(),
      });
    render(
      <PolicyWaiverRequestDialog
        boardId="board-1"
        finding={finding()}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    fillRequiredRequestFields();
    fireEvent.click(screen.getByRole('button', { name: 'Request waiver' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'network unavailable',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Request waiver' }));
    await waitFor(() =>
      expect(policyApiMock.requestPolicyWaiver).toHaveBeenCalledTimes(2),
    );
    expect(
      policyApiMock.requestPolicyWaiver.mock.calls[0][1].idempotency_key,
    ).toBe(
      policyApiMock.requestPolicyWaiver.mock.calls[1][1].idempotency_key,
    );
  });

  it('fails closed when the exact request capability is unavailable', () => {
    render(
      <PolicyWaiverRequestDialog
        boardId="board-1"
        finding={finding()}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    fillRequiredRequestFields();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'guidelines.waiver.request is not granted',
    );
    expect(
      screen.getByRole('button', { name: 'Request waiver' }),
    ).toBeDisabled();
    expect(policyApiMock.requestPolicyWaiver).not.toHaveBeenCalled();
  });

  it('uses reviewer capability, CAS revision and no client event ID on approval', async () => {
    grant('guidelines.waiver.review');
    const approved = approvedHead();
    policyApiMock.reviewPolicyWaiver.mockImplementation(
      async (
        _boardId: string,
        _waiverId: string,
        request: { reason: string; evidence_refs: string[] },
      ) => ({
        waiver: {
          ...approved,
          review_reason: request.reason,
        },
        event: {
          ...transitionEvent({
            revision: 2,
            eventType: 'approve',
            fromStatus: 'requested',
          }),
          reason: request.reason,
          evidence_refs: request.evidence_refs,
        },
      }),
    );
    const onCompleted = vi.fn();
    render(
      <PolicyWaiverActionDialog
        boardId="board-1"
        waiver={waiverSummary()}
        action="approve"
        onClose={vi.fn()}
        onCompleted={onCompleted}
      />,
    );
    expect(screen.getByText(/requester cannot perform this action/i))
      .toBeInTheDocument();
    fillActionFields();
    fireEvent.click(screen.getByRole('button', { name: 'Approve waiver' }));
    await waitFor(() =>
      expect(policyApiMock.reviewPolicyWaiver).toHaveBeenCalledTimes(1),
    );
    const request = policyApiMock.reviewPolicyWaiver.mock.calls[0][2];
    expect(request).toEqual({
      decision: 'approve',
      reason: 'Independent decision.',
      evidence_refs: ['review://one'],
      expected_waiver_revision: 1,
      idempotency_key: expect.any(String),
    });
    expect(request).not.toHaveProperty('event_id');
    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
  });

  it('refreshes authority after CAS conflict and never auto-replays stale input', async () => {
    grant('guidelines.waiver.revalidate');
    const initial = waiverSummary({
      status: 'approved',
      effective: true,
      waiver_revision: 2,
      last_event_at: '2026-07-30T12:00:00Z',
    });
    const refreshed = approvedHead(3, 'revalidate');
    policyApiMock.revalidatePolicyWaiver
      .mockRejectedValueOnce(
        new PolicyGovernanceApiError({
          message: 'CAS conflict',
          status: 409,
          kind: 'conflict',
          code: 'conflict',
          nextAction: 'refresh_and_retry',
        }),
      )
      .mockImplementationOnce(
        async (
          _boardId: string,
          _waiverId: string,
          request: {
            reason: string;
            evidence_refs: string[];
            new_expires_at: string;
          },
        ) => ({
          waiver: {
            ...approvedHead(4, 'revalidate'),
            review_reason: request.reason,
            expires_at: request.new_expires_at,
          },
          event: {
            ...transitionEvent({
              revision: 4,
              eventType: 'revalidate',
              fromStatus: 'approved',
              expiresAt: request.new_expires_at,
            }),
            reason: request.reason,
            evidence_refs: request.evidence_refs,
          },
        }),
      );
    policyApiMock.getPolicyWaiver.mockResolvedValue({
      waiver: refreshed,
    });
    render(
      <PolicyWaiverActionDialog
        boardId="board-1"
        waiver={initial}
        action="revalidate"
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    fillActionFields('2100-08-30T09:00');
    fireEvent.click(
      screen.getByRole('button', { name: 'Revalidate waiver' }),
    );
    expect(
      await screen.findByText(/authority was refreshed to revision 3/i),
    ).toBeInTheDocument();
    expect(policyApiMock.getPolicyWaiver).toHaveBeenCalledWith(
      'board-1',
      'server-waiver-1',
      expect.any(AbortSignal),
    );
    expect(policyApiMock.revalidatePolicyWaiver).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('New later expiry'), {
      target: { value: '2101-08-30T09:00' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Revalidate waiver' }),
    );
    await waitFor(() =>
      expect(policyApiMock.revalidatePolicyWaiver).toHaveBeenCalledTimes(2),
    );
    const first = policyApiMock.revalidatePolicyWaiver.mock.calls[0][2];
    const second = policyApiMock.revalidatePolicyWaiver.mock.calls[1][2];
    expect(first.expected_waiver_revision).toBe(2);
    expect(second.expected_waiver_revision).toBe(3);
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
  });

  it('masks and restores the parent dialog while Escape closes only the child', async () => {
    grant('guidelines.waiver.request');
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Parent policy modal"
        >
          <button type="button" onClick={() => setOpen(true)}>
            Open waiver request
          </button>
          {open && (
            <PolicyWaiverRequestDialog
              boardId="board-1"
              finding={finding()}
              onClose={() => setOpen(false)}
              onCompleted={vi.fn()}
            />
          )}
        </div>
      );
    }
    render(<Harness />);
    const opener = screen.getByRole('button', {
      name: 'Open waiver request',
    });
    opener.focus();
    fireEvent.click(opener);
    const parent = opener.closest('[role="dialog"]') as HTMLElement;
    await waitFor(() => {
      expect(parent).toHaveAttribute('aria-hidden', 'true');
      expect(parent).not.toHaveAttribute('aria-modal');
    });
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(
        screen.queryByRole('dialog', { name: 'Request governed waiver' }),
      ).not.toBeInTheDocument(),
    );
    expect(parent).toHaveAttribute('aria-modal', 'true');
    expect(parent).not.toHaveAttribute('aria-hidden');
    expect(opener).toHaveFocus();
  });
});
