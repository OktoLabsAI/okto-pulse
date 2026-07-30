import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import type {
  PolicyComplianceFindingDetail,
  PolicyComplianceReceiptSummary,
  PolicyComplianceState,
} from '@/types/policy-governance';
import { CONTEXTUAL_HELP_EVENT } from '@/components/help';

const policyApiMock = vi.hoisted(() => ({
  listPolicyComplianceReceipts: vi.fn(),
  listPolicyComplianceFindings: vi.fn(),
  evaluatePolicyCompliance: vi.fn(),
  requestPolicyWaiver: vi.fn(),
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

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import { PolicyCompliancePanel } from '../PolicyCompliancePanel';

function receipt({
  id = 'receipt-2',
  state = 'ready',
  currentness = 'current',
  subjectId = 'spec-1',
  evaluatedAt = '2026-07-30T01:00:00Z',
}: Partial<{
  id: string;
  state: PolicyComplianceState;
  currentness: 'current' | 'stale';
  subjectId: string;
  evaluatedAt: string;
}> = {}): PolicyComplianceReceiptSummary {
  return {
    projection: 'summary',
    receipt_id: id,
    subject: {
      board_id: 'board-1',
      entity_type: 'spec',
      subject_id: subjectId,
      subject_version: 7,
    },
    outcome: state === 'not_applicable' ? 'not_applicable' : (
      state === 'blocked' || state === 'ready_with_waivers'
        ? 'fail'
        : 'pass'
    ),
    state,
    currentness,
    currentness_reasons:
      currentness === 'stale' ? ['policy_set_changed'] : [],
    evaluator_version: 'policy-evaluator/v1',
    evaluated_by: 'agent-1',
    evaluated_at: evaluatedAt,
    finding_count: state === 'not_applicable' ? 0 : 3,
    rule_count: state === 'not_applicable' ? 0 : 8,
    failed_rule_count:
      state === 'blocked' || state === 'ready_with_waivers' ? 2 : 0,
    error_rule_count: 0,
    blocking_finding_count: state === 'blocked' ? 1 : 0,
    waived_finding_count: state === 'ready_with_waivers' ? 1 : 0,
    reason_codes:
      state === 'not_applicable' ? ['no_applicable_rules'] : [],
  };
}

function finding({
  id = 'finding-1',
  receiptId = 'receipt-2',
  subjectId = 'spec-1',
  ruleId = 'rule-1',
  blocking = true,
  waiverId,
}: Partial<{
  id: string;
  receiptId: string;
  subjectId: string;
  ruleId: string;
  blocking: boolean;
  waiverId: string;
}> = {}): PolicyComplianceFindingDetail {
  return {
    projection: 'detail',
    finding_id: id,
    receipt_id: receiptId,
    subject: {
      board_id: 'board-1',
      entity_type: 'spec',
      subject_id: subjectId,
      subject_version: 7,
    },
    guideline_id: 'guideline-1',
    revision_id: 'revision-1',
    rule_id: ruleId,
    outcome: 'fail',
    enforcement: blocking || waiverId ? 'blocking' : 'advisory',
    severity_rank: blocking ? 50 : waiverId ? 40 : 20,
    blocking,
    created_at: '2026-07-30T01:00:00Z',
    message: `Finding ${id}`,
    evidence_refs: [`evidence:${id}`],
    ...(waiverId ? { waiver_id: waiverId } : {}),
  };
}

function page<T>(items: T[], nextCursor?: string) {
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

function renderPanel({
  onRequestWaiver,
  onEvaluated,
  onRefreshed,
  subjectId = 'spec-1',
  refreshKey = 0,
}: {
  onRequestWaiver?: (item: PolicyComplianceFindingDetail) => void;
  onEvaluated?: (item: PolicyComplianceReceiptSummary | null) => void;
  onRefreshed?: (item: PolicyComplianceReceiptSummary | null) => void;
  subjectId?: string;
  refreshKey?: number;
} = {}) {
  return render(
    <PolicyCompliancePanel
      boardId="board-1"
      entityType="spec"
      subjectId={subjectId}
      onRequestWaiver={onRequestWaiver}
      onEvaluated={onEvaluated}
      onRefreshed={onRefreshed}
      refreshKey={refreshKey}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  grant('guidelines.compliance.read');
  policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
    page([receipt()]),
  );
  policyApiMock.listPolicyComplianceFindings.mockResolvedValue(
    page([finding()]),
  );
  policyApiMock.evaluatePolicyCompliance.mockResolvedValue({
    evaluation: {
      evaluation_id: 'evaluation-1',
      input_digest: 'a'.repeat(64),
      receipt: {},
    },
  });
  policyApiMock.requestPolicyWaiver.mockImplementation(
    async (
      _boardId: string,
      request: {
        finding_id: string;
        justification: string;
        evidence_refs: string[];
        expires_at: string;
      },
    ) => ({
      waiver: {
        waiver_id: 'server-waiver-1',
        board_id: 'board-1',
        finding_id: request.finding_id,
        receipt_id: 'receipt-2',
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
        justification: request.justification,
        evidence_refs: request.evidence_refs,
        requested_by: 'agent-1',
        requested_at: '2026-07-30T01:00:00Z',
        waiver_revision: 1,
        expires_at: request.expires_at,
        last_event_id: 'server-event-1',
        last_event_type: 'request',
        last_event_at: '2026-07-30T01:00:00Z',
        reviewed_by: null,
        reviewed_at: null,
        review_reason: null,
        revoked_by: null,
        revoked_at: null,
        expire_reason_code: null,
      },
      event: {
        event_id: 'server-event-1',
        waiver_id: 'server-waiver-1',
        board_id: 'board-1',
        waiver_revision: 1,
        event_type: 'request',
        from_status: null,
        to_status: 'requested',
        actor_id: 'agent-1',
        occurred_at: '2026-07-30T01:00:00Z',
        reason: request.justification,
        evidence_refs: request.evidence_refs,
        expires_at: request.expires_at,
        scope_digest: 'a'.repeat(64),
        expire_reason_code: null,
      },
    }),
  );
});

describe('PolicyCompliancePanel', () => {
  it.each([
    ['ready', 'Policy requirements are ready'],
    ['blocked', 'Policy requirements are blocking'],
    ['ready_with_waivers', 'Policy requirements are ready with waivers'],
    ['not_applicable', 'No adopted rule applies'],
  ] as const)(
    'renders the authoritative %s state without a numeric score',
    async (state, headline) => {
      policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
        page([receipt({ state })]),
      );

      renderPanel();

      expect(await screen.findByText(headline)).toBeInTheDocument();
      expect(screen.queryByText(/^Score$/i)).not.toBeInTheDocument();
      expect(screen.queryByText('Gate preview')).not.toBeInTheDocument();
      expect(screen.getByText('Evaluated rules')).toBeInTheDocument();
      expect(screen.getByTestId('policy-compliance-help'))
        .toHaveTextContent('How policy works');
    },
  );

  it('shows stale currentness as historical instead of presenting the prior state as ready', async () => {
    policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
      page([receipt({ currentness: 'stale' })]),
    );

    renderPanel();

    expect(
      await screen.findByText('Policy receipt is stale'),
    ).toBeInTheDocument();
    expect(screen.getByText('Stale evidence')).toBeInTheDocument();
    expect(screen.getByText(/Policy Set Changed/)).toBeInTheDocument();
    expect(
      screen.queryByText('Policy requirements are ready'),
    ).not.toBeInTheDocument();
  });

  it('distinguishes a never-evaluated subject from a transport failure', async () => {
    policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(page([]));
    const rendered = renderPanel();

    expect(
      await screen.findByTestId('policy-compliance-empty'),
    ).toHaveTextContent('has not been evaluated');

    policyApiMock.listPolicyComplianceReceipts.mockRejectedValueOnce(
      new Error('network unavailable'),
    );
    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-2"
      />,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'network unavailable',
    );
  });

  it.each([
    ['loading', true, null, false],
    ['permission error', false, new Error('denied lookup'), false],
    ['owner review', false, null, true],
  ] as const)(
    'fails closed while authority is in %s',
    (_label, isLoading, error, ownerReviewRequired) => {
      permissionState.isLoading = isLoading;
      permissionState.error = error;
      permissionState.ownerReviewRequired = ownerReviewRequired;

      renderPanel();

      expect(
        policyApiMock.listPolicyComplianceReceipts,
      ).not.toHaveBeenCalled();
      expect(
        policyApiMock.evaluatePolicyCompliance,
      ).not.toHaveBeenCalled();
      expect(
        screen.queryByRole('button', { name: 'Evaluate policies' }),
      ).not.toBeInTheDocument();
      expect(screen.getByTestId('policy-compliance-help'))
        .toHaveTextContent('How policy works');
    },
  );

  it('does not load evidence when compliance.read is denied', () => {
    grant();

    renderPanel();

    expect(screen.getByText(/compliance\.read/)).toBeInTheDocument();
    expect(
      policyApiMock.listPolicyComplianceReceipts,
    ).not.toHaveBeenCalled();
    expect(screen.getByTestId('policy-compliance-help'))
      .toHaveTextContent('How policy works');
  });

  it('opens canonical policy Help from the permitted compliance surface', async () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });
    renderPanel();

    await screen.findByText('Policy requirements are ready');
    fireEvent.click(screen.getByTestId('policy-compliance-help'));

    expect(helpListener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'policy-governance' },
      }),
    );
  });

  it('keeps evaluation visible but disabled for a read-only actor', async () => {
    renderPanel();

    const button = await screen.findByRole('button', {
      name: 'Evaluate policies',
    });
    expect(button).toBeDisabled();
    expect(screen.getByText(/compliance\.evaluate/)).toBeInTheDocument();
  });

  it('preserves an evaluation idempotency key across retry and rotates it after success', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.compliance.evaluate',
    );
    const onEvaluated = vi.fn();
    policyApiMock.evaluatePolicyCompliance
      .mockRejectedValueOnce(
        new PolicyGovernanceApiError({
          status: 503,
          kind: 'service_unavailable',
          code: 'service_unavailable',
          message: 'Evaluator temporarily unavailable.',
          retryable: true,
        }),
      )
      .mockResolvedValue({
        evaluation: {
          evaluation_id: 'evaluation-1',
          input_digest: 'a'.repeat(64),
          receipt: {},
        },
      });

    renderPanel({ onEvaluated });
    const button = await screen.findByRole('button', {
      name: 'Evaluate policies',
    });

    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Evaluator temporarily unavailable',
    );
    fireEvent.click(button);

    await waitFor(() =>
      expect(policyApiMock.evaluatePolicyCompliance).toHaveBeenCalledTimes(2),
    );
    const firstKey =
      policyApiMock.evaluatePolicyCompliance.mock.calls[0][1]
        .idempotency_key;
    const retryKey =
      policyApiMock.evaluatePolicyCompliance.mock.calls[1][1]
        .idempotency_key;
    expect(retryKey).toBe(firstKey);
    await waitFor(() => expect(onEvaluated).toHaveBeenCalled());

    fireEvent.click(button);
    await waitFor(() =>
      expect(policyApiMock.evaluatePolicyCompliance).toHaveBeenCalledTimes(3),
    );
    expect(
      policyApiMock.evaluatePolicyCompliance.mock.calls[2][1]
        .idempotency_key,
    ).not.toBe(firstKey);
    expect(
      policyApiMock.evaluatePolicyCompliance.mock.calls[0][1],
    ).toEqual({
      entity_type: 'spec',
      subject_id: 'spec-1',
      idempotency_key: firstKey,
    });
  });

  it('ignores an evaluation response from a subject that is no longer active', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.compliance.evaluate',
    );
    let resolveEvaluation: (() => void) | undefined;
    policyApiMock.evaluatePolicyCompliance.mockReturnValue(
      new Promise((resolve) => {
        resolveEvaluation = () => resolve({
          evaluation: {
            evaluation_id: 'evaluation-a',
            input_digest: 'a'.repeat(64),
            receipt: {},
          },
        });
      }),
    );
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      (_boardId, options: { subjectId: string }) =>
        Promise.resolve(page([
          receipt({
            id: options.subjectId === 'spec-a'
              ? 'receipt-a'
              : 'receipt-b',
            subjectId: options.subjectId,
          }),
        ])),
    );
    const onEvaluated = vi.fn();
    const rendered = renderPanel({
      subjectId: 'spec-a',
      onEvaluated,
    });
    await screen.findByText(/Receipt receipt-a/);

    fireEvent.click(
      screen.getByRole('button', { name: 'Evaluate policies' }),
    );
    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-b"
        onEvaluated={onEvaluated}
      />,
    );
    expect(await screen.findByText(/Receipt receipt-b/)).toBeInTheDocument();

    await act(async () => {
      resolveEvaluation?.();
      await Promise.resolve();
    });

    expect(screen.getByText(/Receipt receipt-b/)).toBeInTheDocument();
    expect(screen.queryByText(/Receipt receipt-a/)).not.toBeInTheDocument();
    expect(onEvaluated).not.toHaveBeenCalled();
  });

  it('does not expose receipt or finding evidence from a previous subject scope', async () => {
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (
        _boardId: string,
        options: { limit: number; subjectId: string },
      ) => page([
        receipt({
          id: options.limit === 1
            ? `current-${options.subjectId}`
            : `history-${options.subjectId}`,
          subjectId: options.subjectId,
        }),
      ]),
    );
    policyApiMock.listPolicyComplianceFindings.mockImplementation(
      async (
        _boardId: string,
        options: { receiptId: string; subjectId: string },
      ) => page([
        finding({
          id: `finding-${options.subjectId}`,
          receiptId: options.receiptId,
          subjectId: options.subjectId,
        }),
      ]),
    );
    const rendered = renderPanel({ subjectId: 'spec-a' });
    await screen.findByText(/Receipt current-spec-a/);
    fireEvent.click(
      screen.getByTestId('policy-compliance-history-toggle'),
    );
    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );
    await screen.findByText(/Receipt history-spec-a/);
    await screen.findByText('Finding finding-spec-a');

    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-b"
      />,
    );

    expect(screen.queryByText(/current-spec-a/)).not.toBeInTheDocument();
    expect(screen.queryByText(/history-spec-a/)).not.toBeInTheDocument();
    expect(screen.queryByText(/finding-spec-a/)).not.toBeInTheDocument();
    expect(await screen.findByText(/Receipt current-spec-b/))
      .toBeInTheDocument();
  });

  it('lazy-loads summary receipt history and traverses opaque cursor pages without duplicates', async () => {
    const latest = receipt();
    const older = receipt({
      id: 'receipt-1',
      evaluatedAt: '2026-07-29T01:00:00Z',
    });
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (_boardId: string, options: { limit: number; cursor?: string }) => {
        if (options.limit === 1) return page([latest]);
        return options.cursor
          ? page([older])
          : page([latest], 'receipt-cursor-1');
      },
    );

    renderPanel();
    await screen.findByText('Policy requirements are ready');
    expect(
      policyApiMock.listPolicyComplianceReceipts,
    ).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('policy-compliance-history-toggle'));
    expect(
      await screen.findByTestId('policy-compliance-receipt-history'),
    ).toHaveTextContent('receipt-2');
    const firstHistoryCall =
      policyApiMock.listPolicyComplianceReceipts.mock.calls.find(
        (call) => call[1].limit === 25,
      );
    expect(firstHistoryCall?.[1]).toMatchObject({
      projection: 'summary',
      entityType: 'spec',
      subjectId: 'spec-1',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(
        screen.getByTestId('policy-compliance-receipt-history'),
      ).toHaveTextContent('receipt-1'),
    );
    expect(
      screen.getAllByText(/Receipt receipt-2/),
    ).toHaveLength(2);
    expect(
      screen.getByTestId('policy-compliance-history-cursor'),
    ).toHaveTextContent('2 items loaded; all available items loaded.');
  });

  it('invalidates expanded collections when an external refresh key changes', async () => {
    let evidenceVersion = 1;
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (_boardId: string, options: { limit: number }) =>
        options.limit === 1
          ? page([receipt({ id: `overview-${evidenceVersion}` })])
          : page([receipt({ id: `history-${evidenceVersion}` })]),
    );
    const rendered = render(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-1"
        refreshKey={0}
      />,
    );
    await screen.findByText(/Receipt overview-1/);
    fireEvent.click(
      screen.getByTestId('policy-compliance-history-toggle'),
    );
    expect(await screen.findByText(/Receipt history-1/)).toBeInTheDocument();

    evidenceVersion = 2;
    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-1"
        refreshKey={1}
      />,
    );

    expect(screen.queryByText(/Receipt overview-1/))
      .not.toBeInTheDocument();
    expect(await screen.findByText(/Receipt overview-2/))
      .toBeInTheDocument();
    expect(await screen.findByText(/Receipt history-2/)).toBeInTheDocument();
    expect(screen.queryByText(/Receipt history-1/)).not.toBeInTheDocument();
  });

  it('notifies the host only after the visible evidence refresh completes', async () => {
    const onRefreshed = vi.fn();
    policyApiMock.listPolicyComplianceReceipts
      .mockResolvedValueOnce(page([receipt({ id: 'receipt-before' })]))
      .mockResolvedValueOnce(page([receipt({ id: 'receipt-after' })]));

    renderPanel({ onRefreshed });

    expect(await screen.findByText(/Receipt receipt-before/))
      .toBeInTheDocument();
    expect(onRefreshed).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByText(/Receipt receipt-after/))
      .toBeInTheDocument();
    expect(onRefreshed).toHaveBeenCalledTimes(1);
    expect(onRefreshed).toHaveBeenCalledWith(
      expect.objectContaining({ receipt_id: 'receipt-after' }),
    );
  });

  it('invalidates an in-flight evaluation when the subject version changes', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.compliance.evaluate',
    );
    let evidenceVersion = 0;
    let resolveEvaluation: (() => void) | undefined;
    policyApiMock.evaluatePolicyCompliance.mockReturnValue(
      new Promise((resolve) => {
        resolveEvaluation = () => resolve({
          evaluation: {
            evaluation_id: 'evaluation-old-version',
            input_digest: 'a'.repeat(64),
            receipt: {},
          },
        });
      }),
    );
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async () => page([
        receipt({ id: `receipt-version-${evidenceVersion}` }),
      ]),
    );
    const onEvaluated = vi.fn();
    const rendered = renderPanel({ onEvaluated, refreshKey: 0 });
    await screen.findByText(/Receipt receipt-version-0/);

    fireEvent.click(
      screen.getByRole('button', { name: 'Evaluate policies' }),
    );
    evidenceVersion = 1;
    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-1"
        refreshKey={1}
        onEvaluated={onEvaluated}
      />,
    );
    expect(await screen.findByText(/Receipt receipt-version-1/))
      .toBeInTheDocument();

    await act(async () => {
      resolveEvaluation?.();
      await Promise.resolve();
    });

    expect(screen.getByText(/Receipt receipt-version-1/))
      .toBeInTheDocument();
    expect(screen.queryByText(/Receipt receipt-version-0/))
      .not.toBeInTheDocument();
    expect(onEvaluated).not.toHaveBeenCalled();
  });

  it('fails closed on a repeated receipt cursor and offers an explicit restart', async () => {
    const latest = receipt();
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (_boardId: string, options: { limit: number; cursor?: string }) => {
        if (options.limit === 1) return page([latest]);
        return options.cursor
          ? page([receipt({ id: 'receipt-1' })], 'same-cursor')
          : page([latest], 'same-cursor');
      },
    );

    renderPanel();
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(screen.getByTestId('policy-compliance-history-toggle'));
    await screen.findByTestId('policy-compliance-receipt-history');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    expect(
      await screen.findByRole('button', { name: 'Restart from newest' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'receipt cursor repeated',
    );
  });

  it('recovers invalid receipt cursors without reusing them', async () => {
    const latest = receipt();
    let firstPageCalls = 0;
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (_boardId: string, options: { limit: number; cursor?: string }) => {
        if (options.limit === 1) return page([latest]);
        if (!options.cursor) {
          firstPageCalls += 1;
          return page([latest], 'expiring-cursor');
        }
        throw new PolicyGovernanceApiError({
          status: 400,
          kind: 'invalid_cursor',
          code: 'invalid_cursor',
          message: 'Cursor invalid.',
          nextAction: 'restart_pagination',
        });
      },
    );

    renderPanel();
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(screen.getByTestId('policy-compliance-history-toggle'));
    await screen.findByTestId('policy-compliance-receipt-history');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    const restart = await screen.findByRole('button', {
      name: 'Restart from newest',
    });
    fireEvent.click(restart);
    await waitFor(() => expect(firstPageCalls).toBe(2));
    const historyCalls =
      policyApiMock.listPolicyComplianceReceipts.mock.calls.filter(
        (call) => call[1].limit === 25,
      );
    expect(historyCalls.at(-1)?.[1].cursor).toBeUndefined();
  });

  it('rejects duplicate receipt identities instead of hiding them', async () => {
    const latest = receipt();
    policyApiMock.listPolicyComplianceReceipts.mockImplementation(
      async (_boardId: string, options: { limit: number; cursor?: string }) => {
        if (options.limit === 1) return page([latest]);
        return options.cursor
          ? page([latest])
          : page([latest], 'receipt-cursor');
      },
    );

    renderPanel();
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(screen.getByTestId('policy-compliance-history-toggle'));
    await screen.findByTestId('policy-compliance-receipt-history');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'receipt identity was repeated',
    );
  });

  it('lazy-loads detailed findings scoped to the latest receipt and preserves stable IDs', async () => {
    renderPanel();
    await screen.findByText('Policy requirements are ready');
    expect(
      policyApiMock.listPolicyComplianceFindings,
    ).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );

    expect(
      await screen.findByTestId('policy-compliance-findings'),
    ).toHaveTextContent('Receipt receipt-2 · Rule rule-1');
    expect(
      policyApiMock.listPolicyComplianceFindings,
    ).toHaveBeenCalledWith(
      'board-1',
      expect.objectContaining({
        limit: 25,
        projection: 'detail',
        receiptId: 'receipt-2',
        subjectId: 'spec-1',
      }),
    );
    expect(screen.getByText('evidence:finding-1')).toBeInTheDocument();
  });

  it('renders Request waiver only with its exact capability and a handler', async () => {
    const onRequestWaiver = vi.fn();
    const rendered = renderPanel({ onRequestWaiver });
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );
    await screen.findByTestId('policy-compliance-findings');
    expect(
      screen.queryByRole('button', { name: 'Request waiver' }),
    ).not.toBeInTheDocument();

    grant(
      'guidelines.compliance.read',
      'guidelines.waiver.request',
    );
    rendered.rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-1"
        onRequestWaiver={onRequestWaiver}
      />,
    );

    const button = await screen.findByRole('button', {
      name: 'Request waiver',
    });
    fireEvent.click(button);
    expect(onRequestWaiver).toHaveBeenCalledWith(
      expect.objectContaining({
        finding_id: 'finding-1',
        receipt_id: 'receipt-2',
        rule_id: 'rule-1',
      }),
    );
  });

  it('opens the shared governed request dialog and refreshes evidence after success', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.waiver.request',
    );
    renderPanel();
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );
    fireEvent.click(await screen.findByRole('button', {
      name: 'Request waiver',
    }));

    expect(
      screen.getByRole('dialog', { name: 'Request governed waiver' }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Justification'), {
      target: { value: 'Temporary controlled exception.' },
    });
    fireEvent.change(screen.getByLabelText('Requested expiry'), {
      target: { value: '2099-08-30T09:00' },
    });
    fireEvent.click(within(screen.getByRole('dialog', {
      name: 'Request governed waiver',
    })).getByRole('button', {
      name: 'Request waiver',
    }));

    await waitFor(() =>
      expect(policyApiMock.requestPolicyWaiver).toHaveBeenCalledTimes(1),
    );
    expect(policyApiMock.requestPolicyWaiver.mock.calls[0][1])
      .not.toHaveProperty('waiver_id');
    expect(policyApiMock.requestPolicyWaiver.mock.calls[0][1])
      .not.toHaveProperty('event_id');
    expect(
      await screen.findByText(/server-waiver-1 was requested/i),
    ).toBeInTheDocument();
    expect(policyApiMock.listPolicyComplianceReceipts.mock.calls.length)
      .toBeGreaterThanOrEqual(2);
  });

  it('does not offer a second waiver for an already waived finding', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.waiver.request',
    );
    policyApiMock.listPolicyComplianceFindings.mockResolvedValue(
      page([finding({
        blocking: false,
        waiverId: 'waiver-1',
      })]),
    );

    renderPanel({ onRequestWaiver: vi.fn() });
    await screen.findByText('Policy requirements are ready');
    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );

    expect(
      await screen.findByText('Governed waiver waiver-1'),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Request waiver' }),
    ).not.toBeInTheDocument();
  });

  it('rejects mismatched runtime projections and disables evaluation until evidence is trustworthy', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.compliance.evaluate',
    );
    policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
      page([receipt({ subjectId: 'other-spec' })]),
    );

    renderPanel();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'malformed or mismatched receipt',
    );
    expect(
      screen.getByRole('button', { name: 'Evaluate policies' }),
    ).toBeDisabled();
    expect(
      policyApiMock.evaluatePolicyCompliance,
    ).not.toHaveBeenCalled();
  });

  it('rejects malformed reason arrays instead of crashing the receipt renderer', async () => {
    const malformed = {
      ...receipt(),
      currentness_reasons: [null],
      reason_codes: [{}],
    };
    policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
      page([malformed]),
    );

    renderPanel();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'malformed or mismatched receipt',
    );
    expect(
      screen.queryByTestId('policy-compliance-current-receipt'),
    ).not.toBeInTheDocument();
  });

  it('rejects an overview that violates the single-latest-receipt contract', async () => {
    grant(
      'guidelines.compliance.read',
      'guidelines.compliance.evaluate',
    );
    policyApiMock.listPolicyComplianceReceipts.mockResolvedValue(
      page([
        receipt({ id: 'receipt-2' }),
        receipt({ id: 'receipt-1' }),
      ]),
    );

    renderPanel();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'more than one latest receipt',
    );
    expect(
      screen.getByRole('button', { name: 'Evaluate policies' }),
    ).toBeDisabled();
    expect(
      policyApiMock.evaluatePolicyCompliance,
    ).not.toHaveBeenCalled();
  });
});
