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

import { CONTEXTUAL_HELP_EVENT } from '@/components/help';
import type {
  SemanticWaiverEvent,
  SemanticWaiverFull,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  listSemanticMetricWaivers: vi.fn(),
  getSemanticMetricWaiver: vi.fn(),
  listSemanticMetricWaiverEvents: vi.fn(),
  reviewSemanticMetricWaiver: vi.fn(),
  revokeSemanticMetricWaiver: vi.fn(),
  revalidateSemanticMetricWaiver: vi.fn(),
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
    expires_at: null,
    last_event_type: 'request',
    last_event_at: '2026-07-30T09:00:00Z',
    justification: 'Temporary semantic exception.',
    requested_by: 'requester-1',
    original_expires_at: null,
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
    expires_at: null,
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

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  grant('guidelines.waiver.read');
  policyApiMock.listSemanticMetricWaivers.mockResolvedValue(page([waiver()]));
  policyApiMock.getSemanticMetricWaiver.mockResolvedValue({
    waiver: waiver(),
  });
  policyApiMock.listSemanticMetricWaiverEvents.mockResolvedValue({
    events: [waiverEvent()],
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
    ({ isLoading, error, ownerReviewRequired }) => {
      permissionState.isLoading = isLoading;
      permissionState.error = error;
      permissionState.ownerReviewRequired = ownerReviewRequired;
      render(<PolicyWaiverPanel boardId="board-1" />);
      expect(policyApiMock.listSemanticMetricWaivers).not.toHaveBeenCalled();
      expect(screen.getByTestId('policy-waiver-help'))
        .toHaveTextContent('How waivers work');
      expect(screen.queryByRole('button', {
        name: 'Refresh newest',
      })).not.toBeInTheDocument();
    },
  );

  it('fails closed without the exact read capability', () => {
    grant();
    render(<PolicyWaiverPanel boardId="board-1" />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'guidelines.waiver.read is not granted',
    );
    expect(policyApiMock.listSemanticMetricWaivers).not.toHaveBeenCalled();
  });

  it('opens canonical policy Help and presents semantic scope identities', async () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });
    render(<PolicyWaiverPanel boardId="board-1" />);
    const row = await screen.findByTestId('policy-waiver-waiver-1');
    expect(row).toHaveTextContent('Architecture.Segregation:v2');
    expect(row).toHaveTextContent('metric-result-1');
    expect(row).toHaveTextContent('finding-1');
    expect(row).toHaveTextContent('receipt-1');
    expect(row).toHaveTextContent('current');
    expect(row).not.toHaveTextContent('rule-1');
    fireEvent.click(screen.getByTestId('policy-waiver-help'));
    expect(helpListener).toHaveBeenCalledWith(expect.objectContaining({
      detail: { sectionId: 'policy-governance' },
    }));
  });

  it('keeps evaluated_at and exact filters fixed across opaque pages', async () => {
    policyApiMock.listSemanticMetricWaivers.mockImplementation(
      async (_boardId: string, options: { cursor?: string }) => (
        options.cursor
          ? page([waiver({
              waiver_id: 'waiver-0',
              requested_at: '2026-07-30T08:00:00Z',
            })])
          : page([waiver()], 'opaque/do-not-parse')
      ),
    );
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.change(screen.getByLabelText('Metric result ID'), {
      target: { value: 'metric-result-1' },
    });
    fireEvent.change(screen.getByLabelText('Finding ID'), {
      target: { value: 'finding-1' },
    });
    fireEvent.change(screen.getByLabelText('Receipt ID'), {
      target: { value: 'receipt-1' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Apply exact filters',
    }));
    await waitFor(() =>
      expect(policyApiMock.listSemanticMetricWaivers)
        .toHaveBeenCalledTimes(2),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTestId('policy-waiver-waiver-0');

    const filtered =
      policyApiMock.listSemanticMetricWaivers.mock.calls[1][1];
    const continued =
      policyApiMock.listSemanticMetricWaivers.mock.calls[2][1];
    expect(filtered).toEqual(expect.objectContaining({
      limit: 25,
      projection: 'full',
      evaluatedAt: expect.any(String),
      metricResultId: 'metric-result-1',
      findingId: 'finding-1',
      receiptId: 'receipt-1',
    }));
    expect(continued.evaluatedAt).toBe(filtered.evaluatedAt);
    expect(continued.cursor).toBe('opaque/do-not-parse');
    expect(screen.queryByText('opaque/do-not-parse'))
      .not.toBeInTheDocument();
  });

  it('creates a strictly newer snapshot on refresh', async () => {
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    const first =
      policyApiMock.listSemanticMetricWaivers.mock.calls[0][1].evaluatedAt;
    fireEvent.click(screen.getByRole('button', {
      name: 'Refresh newest',
    }));
    await waitFor(() =>
      expect(policyApiMock.listSemanticMetricWaivers)
        .toHaveBeenCalledTimes(2),
    );
    const second =
      policyApiMock.listSemanticMetricWaivers.mock.calls[1][1].evaluatedAt;
    expect(Date.parse(second)).toBeGreaterThan(Date.parse(first));
    expect(
      policyApiMock.listSemanticMetricWaivers.mock.calls[1][1].cursor,
    ).toBeUndefined();
  });

  it('shows lifecycle actions only for exact granted capabilities', async () => {
    grant('guidelines.waiver.read', 'guidelines.waiver.review');
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    expect(screen.getByRole('button', { name: 'Approve' }))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' }))
      .toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Revoke' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Revalidate' }))
      .not.toBeInTheDocument();
  });

  it('loads and verifies full semantic head/history lazily', async () => {
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    expect(policyApiMock.getSemanticMetricWaiver).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {
      name: 'Expand waiver waiver-1',
    }));
    expect(screen.getByText('Temporary semantic exception.'))
      .toBeInTheDocument();
    expect(screen.getByText('SHA-256', { exact: false }))
      .toBeInTheDocument();
    fireEvent.click(screen.getByTestId(
      'policy-waiver-history-waiver-1-toggle',
    ));
    expect(await screen.findByText('Verified head revision 1', {
      exact: false,
    })).toBeInTheDocument();
    expect(policyApiMock.getSemanticMetricWaiver).toHaveBeenCalledWith(
      'board-1',
      'waiver-1',
      {
        evaluatedAt:
          policyApiMock.listSemanticMetricWaivers.mock.calls[0][1]
            .evaluatedAt,
        projection: 'full',
        signal: expect.any(AbortSignal),
      },
    );
    expect(policyApiMock.listSemanticMetricWaiverEvents)
      .toHaveBeenCalledWith(
        'board-1',
        'waiver-1',
        expect.any(AbortSignal),
      );
  });

  it('reloads singular evidence when evaluated_at snapshot changes', async () => {
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.click(screen.getByRole('button', {
      name: 'Expand waiver waiver-1',
    }));
    fireEvent.click(screen.getByTestId(
      'policy-waiver-history-waiver-1-toggle',
    ));
    await waitFor(() =>
      expect(policyApiMock.getSemanticMetricWaiver)
        .toHaveBeenCalledTimes(1),
    );
    const firstSnapshot =
      policyApiMock.getSemanticMetricWaiver.mock.calls[0][2].evaluatedAt;

    fireEvent.click(screen.getByRole('button', {
      name: 'Refresh newest',
    }));
    await waitFor(() =>
      expect(policyApiMock.listSemanticMetricWaivers)
        .toHaveBeenCalledTimes(2),
    );
    const waiverToggle = screen.getByRole('button', {
      name: /waiver waiver-1/u,
    });
    if (waiverToggle.getAttribute('aria-label')?.startsWith('Expand')) {
      fireEvent.click(waiverToggle);
    }
    fireEvent.click(screen.getByTestId(
      'policy-waiver-history-waiver-1-toggle',
    ));
    await waitFor(() =>
      expect(policyApiMock.getSemanticMetricWaiver)
        .toHaveBeenCalledTimes(2),
    );
    const secondSnapshot =
      policyApiMock.getSemanticMetricWaiver.mock.calls[1][2].evaluatedAt;
    expect(Date.parse(secondSnapshot))
      .toBeGreaterThan(Date.parse(firstSnapshot));
  });

  it('rejects cross-filter server evidence fail closed', async () => {
    policyApiMock.listSemanticMetricWaivers.mockResolvedValue(page([
      waiver({ metric_result_id: 'different-result' }),
    ]));
    render(<PolicyWaiverPanel boardId="board-1" />);
    await screen.findByTestId('policy-waiver-waiver-1');
    fireEvent.change(screen.getByLabelText('Metric result ID'), {
      target: { value: 'metric-result-1' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Apply exact filters',
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /cross-filter evidence/i,
    );
    expect(screen.queryByTestId('policy-waiver-waiver-1'))
      .not.toBeInTheDocument();
  });
});
