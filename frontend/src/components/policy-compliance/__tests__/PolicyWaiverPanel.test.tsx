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

import type {
  PolicyWaiver,
  PolicyWaiverEvent,
  PolicyWaiverSummary,
} from '@/types/policy-governance';
import { CONTEXTUAL_HELP_EVENT } from '@/components/help';

const policyApiMock = vi.hoisted(() => ({
  listPolicyWaivers: vi.fn(),
  getPolicyWaiver: vi.fn(),
  listPolicyWaiverEvents: vi.fn(),
  reviewPolicyWaiver: vi.fn(),
  revokePolicyWaiver: vi.fn(),
  revalidatePolicyWaiver: vi.fn(),
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

import { PolicyWaiverPanel } from '../PolicyWaiverPanel';

const digest = (character: string) => character.repeat(64);

function summary(
  overrides: Partial<PolicyWaiverSummary> = {},
): PolicyWaiverSummary {
  return {
    projection: 'summary',
    waiver_id: 'waiver-1',
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
      subject_version: 7,
    },
    status: 'requested',
    source_current: true,
    effective: false,
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    expires_at: '2099-08-30T12:00:00Z',
    waiver_revision: 1,
    last_event_at: '2026-07-30T09:00:00Z',
    ...overrides,
  };
}

function head(
  overrides: Partial<PolicyWaiver> = {},
): PolicyWaiver {
  return {
    waiver_id: 'waiver-1',
    board_id: 'board-1',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    guideline_id: 'guideline-1',
    revision_id: 'revision-1',
    rule_id: 'rule-1',
    subject: summary().subject,
    status: 'requested',
    justification: 'Temporary exception.',
    evidence_refs: ['ticket://one'],
    requested_by: 'requester-1',
    requested_at: '2026-07-30T09:00:00Z',
    waiver_revision: 1,
    expires_at: '2099-08-30T12:00:00Z',
    last_event_id: 'event-1',
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
    event_id: 'event-1',
    waiver_id: 'waiver-1',
    board_id: 'board-1',
    waiver_revision: 1,
    event_type: 'request',
    from_status: null,
    to_status: 'requested',
    actor_id: 'requester-1',
    occurred_at: '2026-07-30T09:00:00Z',
    reason: 'Temporary exception.',
    evidence_refs: ['ticket://one'],
    expires_at: '2099-08-30T12:00:00Z',
    scope_digest: digest('a'),
    expire_reason_code: null,
    ...overrides,
  };
}

function page(
  items: PolicyWaiverSummary[],
  nextCursor?: string,
) {
  return nextCursor
    ? {
        items,
        limit: 25,
        has_more: true as const,
        next_cursor: nextCursor,
      }
    : {
        items,
        limit: 25,
        has_more: false as const,
      };
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  grant('guidelines.waiver.read');
  policyApiMock.listPolicyWaivers.mockResolvedValue(page([summary()]));
  policyApiMock.getPolicyWaiver.mockResolvedValue({ waiver: head() });
  policyApiMock.listPolicyWaiverEvents.mockResolvedValue({
    events: [event()],
  });
});

describe('PolicyWaiverPanel', () => {
  it.each([
    {
      label: 'loading',
      isLoading: true,
      error: null,
      ownerReviewRequired: false,
    },
    {
      label: 'permission error',
      isLoading: false,
      error: new Error('permission service unavailable'),
      ownerReviewRequired: false,
    },
    {
      label: 'owner review',
      isLoading: false,
      error: null,
      ownerReviewRequired: true,
    },
  ])(
    'fails closed while waiver authority is $label and keeps Help visible',
    ({
      isLoading,
      error,
      ownerReviewRequired,
    }) => {
      permissionState.isLoading = isLoading;
      permissionState.error = error;
      permissionState.ownerReviewRequired = ownerReviewRequired;

      render(<PolicyWaiverPanel boardId="board-1" />);

      expect(policyApiMock.listPolicyWaivers).not.toHaveBeenCalled();
      expect(screen.getByTestId('policy-waiver-help'))
        .toHaveTextContent('How waivers work');
      expect(
        screen.queryByRole('button', { name: 'Refresh newest' }),
      ).not.toBeInTheDocument();
    },
  );

  it('fails closed before loading evidence without the exact read capability', async () => {
    grant();
    render(<PolicyWaiverPanel boardId="board-1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'guidelines.waiver.read is not granted',
    );
    expect(policyApiMock.listPolicyWaivers).not.toHaveBeenCalled();
    expect(screen.getByTestId('policy-waiver-help'))
      .toHaveTextContent('How waivers work');
  });

  it('opens canonical policy Help from the permitted waiver surface', async () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });
    render(<PolicyWaiverPanel boardId="board-1" />);

    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.click(screen.getByTestId('policy-waiver-help'));

    expect(helpListener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'policy-governance' },
      }),
    );
  });

  it('keeps evaluated_at and filters fixed while forwarding an opaque cursor verbatim', async () => {
    policyApiMock.listPolicyWaivers.mockImplementation(
      async (_boardId: string, options: { cursor?: string }) => (
        options.cursor
          ? page([
              summary({
                waiver_id: 'waiver-0',
                requested_at: '2026-07-30T08:00:00Z',
              }),
            ])
          : page([summary()], 'opaque/do-not-parse')
      ),
    );
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTestId('policy-waiver-waiver-0');

    const first = policyApiMock.listPolicyWaivers.mock.calls[0][1];
    const second = policyApiMock.listPolicyWaivers.mock.calls[1][1];
    expect(first).toEqual(expect.objectContaining({
      limit: 25,
      projection: 'summary',
      evaluatedAt: expect.any(String),
    }));
    expect(second.evaluatedAt).toBe(first.evaluatedAt);
    expect(second.cursor).toBe('opaque/do-not-parse');
    expect(screen.queryByText('opaque/do-not-parse')).not.toBeInTheDocument();
  });

  it('creates a strictly newer snapshot when refreshing from newest', async () => {
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    const first = policyApiMock.listPolicyWaivers.mock.calls[0][1]
      .evaluatedAt as string;
    fireEvent.click(screen.getByRole('button', {
      name: 'Refresh newest',
    }));
    await waitFor(() =>
      expect(policyApiMock.listPolicyWaivers).toHaveBeenCalledTimes(2),
    );
    const second = policyApiMock.listPolicyWaivers.mock.calls[1][1]
      .evaluatedAt as string;
    expect(new Date(second).getTime()).toBeGreaterThan(
      new Date(first).getTime(),
    );
    expect(policyApiMock.listPolicyWaivers.mock.calls[1][1].cursor)
      .toBeUndefined();
  });

  it('rejects rows outside active status, entity and subject filters', async () => {
    policyApiMock.listPolicyWaivers.mockResolvedValue(
      page([summary({ status: 'requested' })]),
    );
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.change(screen.getByLabelText('Status'), {
      target: { value: 'approved' },
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /malformed or cross-board evidence/i,
    );
    expect(
      screen.queryByTestId('policy-waiver-waiver-1'),
    ).not.toBeInTheDocument();
  });

  it('shows only lifecycle actions granted by exact independent capabilities', async () => {
    grant('guidelines.waiver.read', 'guidelines.waiver.review');
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    expect(
      screen.getByRole('button', { name: 'Approve' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Reject' }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Revoke' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Revalidate' }),
    ).not.toBeInTheDocument();
  });

  it('loads detail/history lazily and invalidates it when the same waiver head changes', async () => {
    let revision = 1;
    policyApiMock.listPolicyWaivers.mockImplementation(async () => (
      revision === 1
        ? page([summary()])
        : page([
            summary({
              status: 'approved',
              effective: true,
              waiver_revision: 2,
              last_event_at: '2026-07-30T10:00:00Z',
            }),
          ])
    ));
    policyApiMock.getPolicyWaiver.mockImplementation(async () => (
      revision === 1
        ? { waiver: head() }
        : {
            waiver: head({
              status: 'approved',
              waiver_revision: 2,
              last_event_id: 'event-2',
              last_event_type: 'approve',
              last_event_at: '2026-07-30T10:00:00Z',
              reviewed_by: 'reviewer-1',
              reviewed_at: '2026-07-30T10:00:00Z',
              review_reason: 'Approved independently.',
            }),
          }
    ));
    policyApiMock.listPolicyWaiverEvents.mockImplementation(async () => ({
      events: revision === 1
        ? [event()]
        : [
            event(),
            event({
              event_id: 'event-2',
              waiver_revision: 2,
              event_type: 'approve',
              from_status: 'requested',
              to_status: 'approved',
              actor_id: 'reviewer-1',
              occurred_at: '2026-07-30T10:00:00Z',
              reason: 'Approved independently.',
              evidence_refs: ['review://one'],
            }),
          ],
    }));
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    expect(policyApiMock.getPolicyWaiver).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', {
      name: 'Expand waiver waiver-1',
    }));
    fireEvent.click(screen.getByTestId(
      'policy-waiver-history-waiver-1-toggle',
    ));
    expect(await screen.findByText('Verified head revision 1', {
      exact: false,
    })).toBeInTheDocument();

    revision = 2;
    fireEvent.click(screen.getByRole('button', {
      name: 'Refresh newest',
    }));
    await waitFor(() =>
      expect(policyApiMock.listPolicyWaivers).toHaveBeenCalledTimes(2),
    );
    expect(
      screen.queryByText('Verified head revision 1', { exact: false }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', {
      name: 'Expand waiver waiver-1',
    }));
    fireEvent.click(screen.getByTestId(
      'policy-waiver-history-waiver-1-toggle',
    ));
    expect(await screen.findByText('Verified head revision 2', {
      exact: false,
    })).toBeInTheDocument();
    expect(policyApiMock.getPolicyWaiver).toHaveBeenCalledTimes(2);
  });
});
