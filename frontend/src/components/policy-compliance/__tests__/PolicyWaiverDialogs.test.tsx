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
  SemanticFindingDetail,
  SemanticWaiverFull,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  requestSemanticMetricWaiver: vi.fn(),
  reviewSemanticMetricWaiver: vi.fn(),
  revokeSemanticMetricWaiver: vi.fn(),
  revalidateSemanticMetricWaiver: vi.fn(),
  getSemanticMetricWaiver: vi.fn(),
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

function finding(): SemanticFindingDetail {
  return {
    projection: 'detail',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    lifecycle_state: 'history_only',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'revision-1',
    binding_id: 'binding-1',
    metric_id: 'metric-1',
    metric_code: 'Architecture.Segregation:v2',
    currentness: 'current',
    currentness_reasons: [],
    created_at: '2026-07-30T09:00:00Z',
    metric_result_id: 'metric-result-1',
    binding_revision: 3,
    rationale: 'Ports depend on infrastructure.',
    evidence_refs: [{
      source_type: 'spec',
      source_id: 'spec-1',
      source_version: 7,
      content_hash: digest('a'),
    }],
    pinpoints: [{
      anchor_type: 'field',
      anchor_ref: 'architecture',
      excerpt_hash: digest('b'),
      input_digest: digest('c'),
    }],
  };
}

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
    evidence_refs: finding().evidence_refs,
    metric_result_id: 'metric-result-1',
    metric_result_digest: digest('d'),
    finding_digest: digest('e'),
    receipt_digest: digest('f'),
    subject_content_digest: digest('1'),
    guideline_revision_digest: digest('2'),
    binding_revision: 3,
    binding_configuration_digest: digest('3'),
    scope_digest: digest('4'),
    head_digest: digest('5'),
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

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function fillActionEvidence() {
  fireEvent.change(screen.getByLabelText('Review reason'), {
    target: { value: 'Approved by an independent reviewer.' },
  });
  fireEvent.change(screen.getByLabelText('Source type'), {
    target: { value: 'review' },
  });
  fireEvent.change(screen.getByLabelText('Source ID'), {
    target: { value: 'review-1' },
  });
  fireEvent.change(screen.getByLabelText('SHA-256 content hash'), {
    target: { value: digest('9') },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  grant();
  policyApiMock.requestSemanticMetricWaiver.mockResolvedValue({
    waiver_id: 'waiver-1',
    status: 'requested',
    scope_digest: digest('4'),
  });
  policyApiMock.reviewSemanticMetricWaiver.mockResolvedValue({
    waiver_id: 'waiver-1',
    waiver_revision: 2,
    status: 'approved',
    reviewer_id: 'reviewer-1',
    replayed: false,
  });
  policyApiMock.revalidateSemanticMetricWaiver.mockResolvedValue({
    waiver_id: 'waiver-1',
    waiver_revision: 3,
    status: 'approved',
    current: true,
    reason_code: 'current',
    replayed: false,
  });
});

describe('PolicyWaiverRequestDialog', () => {
  it('submits the exact semantic anchor and structured evidence', async () => {
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
    fireEvent.change(screen.getByLabelText('Justification'), {
      target: { value: 'Temporary exception while architecture migrates.' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Request waiver',
    }));
    await waitFor(() =>
      expect(policyApiMock.requestSemanticMetricWaiver).toHaveBeenCalledTimes(1),
    );
    expect(policyApiMock.requestSemanticMetricWaiver)
      .toHaveBeenCalledWith(
        'board-1',
        expect.objectContaining({
          metric_result_id: 'metric-result-1',
          finding_id: 'finding-1',
          receipt_id: 'receipt-1',
          justification:
            'Temporary exception while architecture migrates.',
          evidence_refs: [{
            source_type: 'spec',
            source_id: 'spec-1',
            source_version: 7,
            content_hash: digest('a'),
          }],
          expires_at: null,
          idempotency_key: expect.any(String),
        }),
        expect.any(AbortSignal),
      );
    expect(onCompleted).toHaveBeenCalledWith(expect.objectContaining({
      action: 'request',
      waiverId: 'waiver-1',
      status: 'requested',
    }));
    expect(screen.getByTestId('policy-waiver-exact-scope'))
      .toHaveTextContent('Architecture.Segregation:v2');
    expect(screen.getByTestId('policy-waiver-exact-scope'))
      .not.toHaveTextContent('Rule');
  });

  it('fails closed without the exact request capability', () => {
    render(
      <PolicyWaiverRequestDialog
        boardId="board-1"
        finding={finding()}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'guidelines.waiver.request is not granted',
    );
    expect(screen.getByRole('button', {
      name: 'Request waiver',
    })).toBeDisabled();
  });

  it('rejects a mutation response with an unknown legacy wrapper', async () => {
    grant('guidelines.waiver.request');
    policyApiMock.requestSemanticMetricWaiver.mockResolvedValue({
      waiver_id: 'waiver-1',
      status: 'requested',
      scope_digest: digest('4'),
      event: {},
    });
    render(
      <PolicyWaiverRequestDialog
        boardId="board-1"
        finding={finding()}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText('Justification'), {
      target: { value: 'Temporary exception.' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Request waiver',
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /unknown or missing field/i,
    );
  });
});

describe('PolicyWaiverActionDialog', () => {
  it('reviews by metric waiver revision with structured evidence', async () => {
    grant('guidelines.waiver.review');
    const onCompleted = vi.fn();
    render(
      <PolicyWaiverActionDialog
        boardId="board-1"
        evaluatedAt="2026-07-30T12:00:00Z"
        waiver={waiver()}
        action="approve"
        onClose={vi.fn()}
        onCompleted={onCompleted}
      />,
    );
    fillActionEvidence();
    fireEvent.click(screen.getByRole('button', {
      name: 'Approve waiver',
    }));
    await waitFor(() =>
      expect(policyApiMock.reviewSemanticMetricWaiver)
        .toHaveBeenCalledTimes(1),
    );
    expect(policyApiMock.reviewSemanticMetricWaiver).toHaveBeenCalledWith(
      'board-1',
      'waiver-1',
      expect.objectContaining({
        decision: 'approve',
        expected_waiver_revision: 1,
        evidence_refs: [{
          source_type: 'review',
          source_id: 'review-1',
          source_version: 1,
          content_hash: digest('9'),
        }],
        idempotency_key: expect.any(String),
      }),
      expect.any(AbortSignal),
    );
    expect(onCompleted).toHaveBeenCalledWith(expect.objectContaining({
      action: 'approve',
      status: 'approved',
      waiverRevision: 2,
    }));
  });

  it('revalidates without inventing review reason or mutable expiry', async () => {
    grant('guidelines.waiver.revalidate');
    const onCompleted = vi.fn();
    render(
      <PolicyWaiverActionDialog
        boardId="board-1"
        evaluatedAt="2026-07-30T12:00:00Z"
        waiver={waiver({
          status: 'approved',
          waiver_revision: 2,
          last_event_type: 'approve',
          reviewed_by: 'reviewer-1',
          reviewed_at: '2026-07-30T10:00:00Z',
          review_reason: 'Approved.',
        })}
        action="revalidate"
        onClose={vi.fn()}
        onCompleted={onCompleted}
      />,
    );
    expect(screen.queryByLabelText('Review reason')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('New later expiry')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {
      name: 'Revalidate waiver',
    }));
    await waitFor(() =>
      expect(policyApiMock.revalidateSemanticMetricWaiver)
        .toHaveBeenCalledTimes(1),
    );
    const request =
      policyApiMock.revalidateSemanticMetricWaiver.mock.calls[0][2];
    expect(request).toEqual({
      expected_waiver_revision: 2,
      evaluated_at: expect.any(String),
      idempotency_key: expect.any(String),
    });
    expect(onCompleted).toHaveBeenCalledWith(expect.objectContaining({
      action: 'revalidate',
      current: true,
      reasonCode: 'current',
    }));
  });

  it('refreshes the exact semantic head after a revision conflict', async () => {
    grant('guidelines.waiver.review');
    const { PolicyGovernanceApiError } = await import(
      '@/services/policy-governance-api'
    );
    policyApiMock.reviewSemanticMetricWaiver.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'conflict',
        status: 409,
        kind: 'conflict',
        code: 'semantic_waiver_revision_conflict',
        details: {},
      }),
    );
    const refreshed = waiver({
      waiver_revision: 2,
      last_event_id: 'event-2',
      last_event_at: '2026-07-30T10:00:00Z',
      head_digest: digest('6'),
    });
    policyApiMock.getSemanticMetricWaiver.mockResolvedValue({
      waiver: refreshed,
    });
    render(
      <PolicyWaiverActionDialog
        boardId="board-1"
        evaluatedAt="2026-07-30T12:00:00Z"
        waiver={waiver()}
        action="approve"
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );
    fillActionEvidence();
    fireEvent.click(screen.getByRole('button', {
      name: 'Approve waiver',
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /refreshed to revision 2/i,
    );
    expect(policyApiMock.getSemanticMetricWaiver).toHaveBeenCalledWith(
      'board-1',
      'waiver-1',
      {
        evaluatedAt: '2026-07-30T12:00:00Z',
        projection: 'full',
        signal: expect.any(AbortSignal),
      },
    );
  });
});
