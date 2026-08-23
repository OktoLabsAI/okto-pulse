import {
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
  AllowedTransition,
  PolicyComplianceTransitionDecision,
  PolicyComplianceLifecycleBinding,
  PolicyComplianceLifecycleDetails,
} from '@/types';
import type {
  SemanticAssessmentCurrentnessReason,
  SemanticAssessmentDetail,
  SemanticCursorPage,
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticMetricResultDetail,
  SemanticSkipDetail,
  SemanticWaiverDetail,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  listSemanticGuidelineAssessments: vi.fn(),
  getCurrentSemanticGuidelineAssessment: vi.fn(),
  listSemanticGuidelineFindings: vi.fn(),
  listSemanticMetricWaivers: vi.fn(),
  requestSemanticMetricWaiver: vi.fn(),
  listSemanticPolicySkips: vi.fn(),
  createSemanticPolicySkip: vi.fn(),
  revokeSemanticPolicySkip: vi.fn(),
  getGuidelineRevision: vi.fn(),
}));

const dashboardApiMock = vi.hoisted(() => ({
  getBoardGuidelines: vi.fn(),
}));

vi.mock('@/services/api', async () => {
  const actual = await vi.importActual<
    typeof import('@/services/api')
  >('@/services/api');
  return {
    ...actual,
    useDashboardApi: () => dashboardApiMock,
  };
});

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

import { PolicyCompliancePanel } from '../PolicyCompliancePanel';
import { PolicyGovernanceApiError } from '@/services/policy-governance-api';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function evidence(
  sourceId = 'spec-1',
  hash = HASH_A,
): SemanticEvidenceRef {
  return {
    source_type: 'spec',
    source_id: sourceId,
    source_version: 7,
    content_hash: hash,
  };
}

function metric({
  id = 'metric-1',
  resultId = 'metric-result-1',
  code = 'architecture.segregation',
  score = 84,
  direction = 'minimum',
  defaultThreshold = 70,
  effectiveThreshold = defaultThreshold,
  thresholdSource = 'default',
}: Partial<{
  id: string;
  resultId: string;
  code: string;
  score: number;
  direction: 'minimum' | 'maximum';
  defaultThreshold: number;
  effectiveThreshold: number;
  thresholdSource: 'default' | 'override';
}> = {}): SemanticMetricResultDetail {
  const outcome = direction === 'minimum'
    ? score >= effectiveThreshold ? 'pass' : 'fail'
    : score <= effectiveThreshold ? 'pass' : 'fail';
  return {
    metric_result_id: resultId,
    metric_id: id,
    metric_code: code,
    score,
    direction,
    default_threshold: defaultThreshold,
    effective_threshold: effectiveThreshold,
    threshold_source: thresholdSource,
    outcome,
    rationale: `${code} was assessed against the immutable subject.`,
    evidence_refs: [evidence()],
    pinpoints: [{
      anchor_type: 'field',
      anchor_ref: 'description',
      excerpt_hash: HASH_B,
      input_digest: HASH_A,
    }],
  };
}

function assessment({
  receiptId = 'receipt-1',
  bindingId = 'binding-1',
  guidelineId = 'guideline-1',
  confidence = 92,
  minimumConfidence = 80,
  assessorIndependent = true,
  currentness = 'current',
  currentnessReasons = [],
  validationEdition = null,
  metricResults = [metric()],
}: Partial<{
  receiptId: string;
  bindingId: string;
  guidelineId: string;
  confidence: number;
  minimumConfidence: number;
  assessorIndependent: boolean;
  currentness: 'current' | 'stale';
  currentnessReasons: SemanticAssessmentCurrentnessReason[];
  validationEdition: number | null;
  metricResults: SemanticMetricResultDetail[];
}> = {}): SemanticAssessmentDetail {
  const failedMetricCount = metricResults.filter(
    (item) => item.outcome === 'fail',
  ).length;
  return {
    projection: 'detail',
    receipt_id: receiptId,
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: validationEdition,
    lifecycle_state: validationEdition === null
      ? 'history_only'
      : currentness === 'current' ? 'current' : 'previous',
    binding_id: bindingId,
    guideline_id: guidelineId,
    guideline_revision_id: `${guidelineId}-revision-3`,
    enforcement: 'blocking',
    state:
      failedMetricCount === 0 ? 'passed' : 'metric_threshold_failed',
    currentness,
    currentness_reasons: currentnessReasons,
    confidence,
    minimum_confidence: minimumConfidence,
    metric_count: metricResults.length,
    failed_metric_count: failedMetricCount,
    recorded_at: '2026-07-30T01:00:00Z',
    binding_revision: 3,
    assessor_agent_id: 'assessor-agent',
    assessor_model_id: 'semantic-model-v1',
    assessor_independent: assessorIndependent,
    confidence_admissible: confidence >= minimumConfidence,
    metric_results: metricResults,
  };
}

function currentV2Assessment(
  entityType: 'ideation' | 'refinement' | 'spec' | 'card' | 'sprint',
  subjectId: string,
) {
  return {
    contract_version: 'v2' as const,
    assessment: {
      receipt_id: `receipt-v2-${entityType}`,
      receipt_digest: HASH_A,
      currentness: 'current' as const,
      board_id: 'board-1',
      subject_type: entityType,
      subject_id: subjectId,
      subject_version: 7,
      validation_edition: null,
      lifecycle_state: 'current' as const,
      binding_id: 'binding-1',
      guideline_id: 'guideline-1',
      guideline_revision_id: 'guideline-1-revision-3',
      confidence: 94,
      recorded_at: '2026-08-08T12:00:00Z',
      metrics: [{
        metric_result_id: `result-v2-${entityType}`,
        metric_result_digest: HASH_B,
        metric_id: 'metric-1',
        metric_code: 'architecture.segregation',
        score: 86,
        direction: 'minimum' as const,
        default_threshold: 75,
        effective_threshold: 75,
        threshold_source: 'default' as const,
        outcome: 'pass' as const,
        blocking: false,
        pinpoints: [{
          contract_version: 'v2' as const,
          pinpoint_key: `pinpoint-v2-${entityType}`,
          kind: 'evidence' as const,
          title: 'Domain boundary is explicit',
          detail: 'The business responsibility is isolated from runtime details.',
          severity: null,
          remediation: null,
          anchor: {
            anchor_type: 'field' as const,
            anchor_ref: 'technical_requirements',
            excerpt_hash: HASH_B,
          },
          anchor_snapshot: {
            label: 'Technical requirements',
            excerpt: 'Runtime adapters remain outside the domain boundary.',
            source_version: '7',
            availability_at_seal: 'available' as const,
          },
          blocking: false,
        }],
      }],
    },
  };
}

function adoptedGuideline() {
  return {
    id: 'guideline-1',
    guideline: {
      id: 'guideline-1',
      title: 'Hexagonal architecture',
      content: 'Core declares WHAT; community provides HOW.',
      tags: [],
      scope: 'global',
      board_id: null,
      owner_id: 'owner-1',
      revision_id: 'guideline-1-revision-3',
      created_at: '2026-07-27T00:00:00Z',
      updated_at: '2026-07-27T00:00:00Z',
    },
    priority: 10,
    scope: 'global',
    binding_id: 'binding-1',
    binding_revision: 3,
    enforcement: 'advisory',
    minimum_confidence: 80,
    metric_threshold_overrides: {},
    binding_state: 'active',
    source_kind: 'native',
  };
}

function guidelineRevisionFor(
  entityType: 'ideation' | 'refinement' | 'spec' | 'card' | 'sprint',
) {
  return {
    revision: {
      metrics: [{
        metric_id: 'metric-1',
        code: 'architecture.segregation',
        title: 'Segregation',
        description: 'Business vs technical separation.',
        evaluation_rubric: 'Rubric.',
        target_entity_types: [entityType],
        direction: 'minimum',
        default_threshold: 75,
      }],
    },
  };
}

function frozenBinding({
  bindingId = 'binding-1',
  guidelineId = 'guideline-1',
  revisionId = 'guideline-1-revision-3',
  title = 'Hexagonal architecture',
  enforcement = 'advisory',
  status = 'passed',
  metricId = 'metric-1',
  metricCode = 'architecture.segregation',
  metricTitle = 'Segregation',
  threshold = 75,
  descriptionTruncated = false,
  rubricTruncated = false,
  assessmentOutcome,
  failedMetricCount,
  waivedMetricCount,
  unwaivedFailedMetricCount,
}: Partial<{
  bindingId: string;
  guidelineId: string;
  revisionId: string;
  title: string;
  enforcement: 'advisory' | 'blocking';
  status: PolicyComplianceLifecycleBinding['status'];
  metricId: string;
  metricCode: string;
  metricTitle: string;
  threshold: number;
  descriptionTruncated: boolean;
  rubricTruncated: boolean;
  assessmentOutcome: PolicyComplianceLifecycleBinding['metrics'][number]['assessment_outcome'];
  failedMetricCount: number;
  waivedMetricCount: number;
  unwaivedFailedMetricCount: number;
}> = {}): PolicyComplianceLifecycleBinding {
  const resolvedOutcome = assessmentOutcome
    ?? (status === 'passed'
      ? 'passed'
      : status === 'failed'
        ? 'failed'
        : status === 'waived'
          ? 'waived'
          : 'pending');
  const resolvedWaivedMetricCount = waivedMetricCount
    ?? (resolvedOutcome === 'waived' ? 1 : 0);
  const resolvedUnwaivedFailedMetricCount = unwaivedFailedMetricCount
    ?? (resolvedOutcome === 'failed' ? 1 : 0);
  return {
    binding_id: bindingId,
    guideline_id: guidelineId,
    revision_id: revisionId,
    title,
    enforcement,
    minimum_confidence: 80,
    status,
    failed_metric_count: failedMetricCount
      ?? resolvedWaivedMetricCount + resolvedUnwaivedFailedMetricCount,
    waived_metric_count: resolvedWaivedMetricCount,
    unwaived_failed_metric_count: resolvedUnwaivedFailedMetricCount,
    metrics: [{
      metric_id: metricId,
      code: metricCode,
      title: metricTitle,
      description: `${metricTitle} frozen description.`,
      description_truncated: descriptionTruncated,
      evaluation_rubric: `${metricTitle} frozen rubric.`,
      evaluation_rubric_truncated: rubricTruncated,
      assessment_outcome: resolvedOutcome,
      direction: 'minimum',
      default_threshold: threshold,
      effective_threshold: threshold,
      threshold_source: 'default',
    }],
  };
}

function frozenLifecycleDetails(
  bindings: PolicyComplianceLifecycleBinding[] = [frozenBinding()],
  contextOnly = 0,
): PolicyComplianceLifecycleDetails {
  const count = (status: PolicyComplianceLifecycleBinding['status']) =>
    bindings.filter((binding) => binding.status === status).length;
  const passed = count('passed');
  const failed = count('failed');
  const waived = count('waived');
  const skipped = count('skipped');
  const blocking = bindings.filter(
    (binding) => binding.enforcement === 'blocking',
  );
  const advisory = bindings.filter(
    (binding) => binding.enforcement === 'advisory',
  );
  return {
    counts: {
      applicable: bindings.length,
      completed: passed + failed + waived + skipped,
      passed,
      failed,
      waived,
      skipped,
      pending: count('pending'),
      context_only: contextOnly,
      inconsistent: count('inconsistent'),
      scope_inconsistent: 0,
      blocking: blocking.length,
      advisory: advisory.length,
      blocking_failed: blocking.filter((binding) => binding.status === 'failed').length,
      blocking_pending: blocking.filter((binding) => binding.status === 'pending').length,
      advisory_failed: advisory.filter((binding) => binding.status === 'failed').length,
      advisory_pending: advisory.filter((binding) => binding.status === 'pending').length,
      failed_metrics: bindings.reduce(
        (total, binding) => total + binding.failed_metric_count,
        0,
      ),
      waived_metrics: bindings.reduce(
        (total, binding) => total + binding.waived_metric_count,
        0,
      ),
      unwaived_failed_metrics: bindings.reduce(
        (total, binding) => total + binding.unwaived_failed_metric_count,
        0,
      ),
    },
    applicable_bindings: bindings,
  };
}

function finding(): SemanticFindingDetail {
  return {
    projection: 'detail',
    finding_id: 'finding-1',
    receipt_id: 'receipt-failed',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    lifecycle_state: 'history_only',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'guideline-1-revision-3',
    binding_id: 'binding-1',
    metric_id: 'metric-segregation',
    metric_code: 'architecture.segregation',
    currentness: 'current',
    currentness_reasons: [],
    created_at: '2026-07-30T01:01:00Z',
    metric_result_id: 'metric-result-failed',
    binding_revision: 3,
    rationale: 'Domain and technical responsibilities remain coupled.',
    evidence_refs: [
      evidence('spec-1', HASH_A),
      {
        source_type: 'architecture',
        source_id: 'architecture-1',
        source_version: 2,
        content_hash: HASH_B,
      },
    ],
    pinpoints: [{
      anchor_type: 'structured_child',
      anchor_ref: 'architecture.boundaries[0]',
      excerpt_hash: HASH_B,
      input_digest: HASH_A,
    }],
  };
}

function waiver(): SemanticWaiverDetail {
  return {
    projection: 'detail',
    waiver_id: 'waiver-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    lifecycle_state: 'history_only',
    finding_id: 'finding-1',
    receipt_id: 'receipt-failed',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'guideline-1-revision-3',
    binding_id: 'binding-1',
    metric_id: 'metric-segregation',
    metric_code: 'architecture.segregation',
    status: 'approved',
    waiver_revision: 2,
    currentness: 'current',
    currentness_reasons: [],
    requested_at: '2026-07-30T01:02:00Z',
    expires_at: null,
    last_event_type: 'approve',
    last_event_at: '2026-07-30T01:03:00Z',
    justification: 'Approved migration window.',
    requested_by: 'requester-agent',
    original_expires_at: null,
    reviewed_by: 'independent-reviewer',
    reviewed_at: '2026-07-30T01:03:00Z',
    review_reason: 'Bounded exception.',
    revoked_by: null,
    revoked_at: null,
    expire_reason: null,
    evidence_refs: [evidence()],
  };
}

function skip({
  status = 'active',
  currentness = 'current',
}: Partial<{
  status: 'active' | 'revoked';
  currentness: 'current' | 'stale';
}> = {}): SemanticSkipDetail {
  const revoked = status === 'revoked';
  return {
    projection: 'detail',
    skip_id: 'skip-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    lifecycle_state: 'history_only',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'guideline-1-revision-3',
    binding_id: 'binding-1',
    status,
    skip_revision: revoked ? 2 : 1,
    currentness,
    currentness_reasons:
      currentness === 'stale' ? ['subject_version_changed'] : [],
    created_at: '2026-07-30T01:04:00Z',
    last_event_type: revoked ? 'revoke' : 'create',
    last_event_at: revoked
      ? '2026-07-30T01:05:00Z'
      : '2026-07-30T01:04:00Z',
    binding_revision: 3,
    reason: 'Human-approved temporary exception.',
    created_by: 'human-owner',
    revoked_by: revoked ? 'human-owner' : null,
    revoked_at: revoked ? '2026-07-30T01:05:00Z' : null,
    revocation_reason: revoked ? 'Exception is no longer needed.' : null,
  };
}

function page<T>(
  items: T[],
  nextCursor: string | null = null,
): SemanticCursorPage<T> {
  return {
    items,
    projection: 'detail',
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

function blockedTransitionPreview(
  cause: 'assessment_unavailable' | 'assessment_inadmissible',
) {
  const unavailable = cause === 'assessment_unavailable';
  const decision: PolicyComplianceTransitionDecision = {
    projection: 'full',
    state: unavailable
      ? 'policy_assessment_unavailable'
      : 'policy_compliance_blocked',
    allowed: false,
    policy_compliance_required: true,
    reason_codes: unavailable
      ? ['policy_assessment_unavailable']
      : ['policy_compliance_blocked'],
    decision_digest: 'c'.repeat(64),
    fence_digest: 'd'.repeat(64),
    receipt_ids: [],
    currentness: null,
    currentness_reasons: [],
    applicable_metric_count: 1,
    applicable_blocking_metric_count: 1,
    failed_metric_count: 0,
    blocking_metric_count: 0,
    waived_metric_count: 0,
    advisory_issue_count: 0,
    skipped_binding_count: 0,
    diagnostic_codes: unavailable
      ? ['policy_assessment_unavailable']
      : ['policy_assessment_inadmissible'],
    binding_decisions: [{
      binding_id: 'binding-no-receipt',
      guideline_id: 'guideline-no-receipt',
      enforcement: 'blocking',
      applicable_metric_count: 1,
      allowed: false,
      assessment_available: !unavailable,
      receipt_id: null,
      currentness: null,
      currentness_reasons: [],
      inadmissibility_cause: unavailable
        ? null
        : 'confidence_below_minimum',
      failed_metric_count: 0,
      waived_metric_count: 0,
      blocking_metric_count: 0,
      advisory_issue_count: 0,
      skipped: false,
      diagnostic_codes: unavailable
        ? ['policy_assessment_unavailable']
        : ['policy_assessment_inadmissible'],
    }],
  };
  const transition: AllowedTransition = {
    to_status: 'validated',
    label: 'Validated',
    gate: 'approved_to_validated',
    blocked_reason: 'Semantic guideline evidence is not admissible.',
    blocked_facts: null,
    preconditions: [],
    capabilities: [],
    effects: [],
    reason_codes: [],
    policy_compliance: true,
    policy_compliance_decision: decision,
  };
  return {
    status: 'ready' as const,
    error: null,
    transitions: [transition],
  };
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function renderPanel(
  props: Partial<React.ComponentProps<typeof PolicyCompliancePanel>> = {},
) {
  return render(
    <PolicyCompliancePanel
      boardId="board-1"
      entityType="spec"
      subjectId="spec-1"
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionState.isLoading = false;
  permissionState.error = null;
  permissionState.ownerReviewRequired = false;
  dashboardApiMock.getBoardGuidelines.mockResolvedValue([]);
  grant('guidelines.assessments.read');
  policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
    page([assessment()]),
  );
  policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
    contract_version: 'v1',
    assessment: assessment(),
  });
  policyApiMock.listSemanticGuidelineFindings.mockResolvedValue(page([]));
  policyApiMock.listSemanticMetricWaivers.mockResolvedValue(page([]));
  policyApiMock.listSemanticPolicySkips.mockResolvedValue(page([]));
  policyApiMock.requestSemanticMetricWaiver.mockResolvedValue({
    waiver_id: 'waiver-request-1',
    status: 'requested',
    scope_digest: HASH_A,
  });
  policyApiMock.createSemanticPolicySkip.mockResolvedValue({
    skip_id: 'skip-created',
    scope_digest: HASH_A,
    created_by: 'human-owner',
  });
  policyApiMock.revokeSemanticPolicySkip.mockResolvedValue({
    skip_id: 'skip-1',
    skip_revision: 2,
    status: 'revoked',
    revoked_by: 'human-owner',
    replayed: false,
  });
});

describe('PolicyCompliancePanel semantic guideline contract', () => {
  it.each([
    ['permission lookup loading', true, null, false],
    ['permission lookup failure', false, new Error('lookup failed'), false],
    ['owner review required', false, null, true],
  ] as const)(
    'fails closed while %s',
    (_label, isLoading, error, ownerReviewRequired) => {
      permissionState.isLoading = isLoading;
      permissionState.error = error;
      permissionState.ownerReviewRequired = ownerReviewRequired;

      renderPanel();

      expect(
        policyApiMock.listSemanticGuidelineAssessments,
      ).not.toHaveBeenCalled();
    },
  );

  it('does not load evidence without guidelines.assessments.read', () => {
    grant();

    renderPanel();

    expect(
      screen.getByText(/guidelines\.assessments\.read/),
    ).toBeInTheDocument();
    expect(
      policyApiMock.listSemanticGuidelineAssessments,
    ).not.toHaveBeenCalled();
  });

  it('renders one authoritative current assessment per binding with confidence and metric score rings', async () => {
    const separation = metric({
      id: 'metric-segregation',
      resultId: 'metric-result-segregation',
      code: 'architecture.segregation',
      score: 88,
      direction: 'minimum',
      defaultThreshold: 75,
    });
    const coupling = metric({
      id: 'metric-coupling',
      resultId: 'metric-result-coupling',
      code: 'architecture.coupling',
      score: 18,
      direction: 'maximum',
      defaultThreshold: 25,
    });
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([
        assessment({
          receiptId: 'receipt-binding-1',
          bindingId: 'binding-1',
          guidelineId: 'guideline-1',
          confidence: 93,
          metricResults: [separation],
        }),
        assessment({
          receiptId: 'receipt-binding-2',
          bindingId: 'binding-2',
          guidelineId: 'guideline-2',
          confidence: 87,
          metricResults: [coupling],
        }),
      ]),
    );

    renderPanel();

    const cards = await screen.findAllByTestId('semantic-assessment-card');
    expect(cards).toHaveLength(2);
    expect(
      within(cards[0]).getByRole('img', {
        name: /Confidence score 93 out of 100.*minimum 80.*threshold met/i,
      }),
    ).toHaveAttribute('data-direction', 'higher-is-better');
    expect(
      within(cards[0]).getByRole('img', {
        name: /architecture\.segregation score 88 out of 100.*minimum 75.*threshold met/i,
      }),
    ).toHaveAttribute('data-status', 'met');
    expect(
      within(cards[1]).getByRole('img', {
        name: /architecture\.coupling score 18 out of 100.*maximum 25.*threshold met/i,
      }),
    ).toHaveAttribute('data-direction', 'lower-is-better');
    expect(
      screen.getByText(
        /All assessment receipt pages loaded; displayed receipts are admissible/i,
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [
      'subject mismatch',
      () => ({
        ...assessment(),
        subject_id: 'different-spec',
      }),
      /does not match the active subject/i,
    ],
    [
      'unknown field',
      () => ({
        ...assessment(),
        unexpected_contract_field: true,
      }),
      /unknown or missing field/i,
    ],
  ])('rejects %s fail-closed', async (_label, invalid, message) => {
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([invalid()]),
    );

    renderPanel();

    expect(
      await screen.findByText(
        /Assessment evidence could not be verified/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText(message)).toHaveLength(2);
    expect(
      screen.queryByTestId('semantic-assessment-card'),
    ).not.toBeInTheDocument();
  });

  it('continues opaque pagination without exposing the cursor in the DOM', async () => {
    const opaqueCursor = 'opaque.cursor.signature.must-not-render';
    policyApiMock.listSemanticGuidelineAssessments.mockImplementation(
      async (
        _boardId: string,
        options?: { cursor?: string },
      ) => options?.cursor === opaqueCursor
        ? page([
            assessment({
              receiptId: 'receipt-page-2',
              bindingId: 'binding-page-2',
              guidelineId: 'guideline-page-2',
            }),
          ])
        : page([
            assessment({
              receiptId: 'receipt-page-1',
              bindingId: 'binding-page-1',
              guidelineId: 'guideline-page-1',
            }),
          ], opaqueCursor),
    );

    renderPanel();

    const controls = await screen.findByTestId(
      'semantic-assessments-cursor',
    );
    expect(within(controls).getByText(/more available/i))
      .toBeInTheDocument();
    expect(screen.queryByText(opaqueCursor)).not.toBeInTheDocument();

    fireEvent.click(
      within(controls).getByRole('button', { name: 'Load more' }),
    );

    await waitFor(() => {
      expect(
        screen.getAllByTestId('semantic-assessment-card'),
      ).toHaveLength(2);
    });
    expect(
      policyApiMock.listSemanticGuidelineAssessments,
    ).toHaveBeenLastCalledWith(
      'board-1',
      expect.objectContaining({
        cursor: opaqueCursor,
        limit: 25,
        projection: 'detail',
        subjectType: 'spec',
        subjectId: 'spec-1',
      }),
    );
    expect(document.body).not.toHaveTextContent(opaqueCursor);
  });

  it('loads semantic findings and requests a metric waiver with structured immutable evidence', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.waiver.request',
      'guidelines.waiver.read',
    );
    const semanticFinding = finding();
    policyApiMock.listSemanticGuidelineFindings.mockResolvedValue(
      page([semanticFinding]),
    );
    policyApiMock.listSemanticMetricWaivers.mockResolvedValue(
      page([waiver()]),
    );

    renderPanel();
    await screen.findByTestId('semantic-assessment-card');

    fireEvent.click(
      screen.getByTestId('policy-compliance-findings-toggle'),
    );
    expect(
      await screen.findByText(semanticFinding.rationale),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'Request metric waiver' }),
    );

    const dialog = screen.getByRole('dialog', {
      name: 'Request semantic metric waiver',
    });
    fireEvent.change(within(dialog).getByLabelText('Justification'), {
      target: { value: 'Allow the bounded migration window.' },
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Request waiver' }),
    );

    await waitFor(() => {
      expect(
        policyApiMock.requestSemanticMetricWaiver,
      ).toHaveBeenCalledWith(
        'board-1',
        {
          metric_result_id: semanticFinding.metric_result_id,
          finding_id: semanticFinding.finding_id,
          receipt_id: semanticFinding.receipt_id,
          justification: 'Allow the bounded migration window.',
          evidence_refs: semanticFinding.evidence_refs,
          expires_at: null,
          idempotency_key: expect.any(String),
        },
      );
    });
    expect(
      policyApiMock.requestSemanticMetricWaiver.mock.calls[0][1]
        .evidence_refs,
    ).toEqual([
      expect.objectContaining({
        source_type: 'spec',
        source_id: 'spec-1',
        source_version: 7,
        content_hash: HASH_A,
      }),
      expect.objectContaining({
        source_type: 'architecture',
        source_id: 'architecture-1',
        source_version: 2,
        content_hash: HASH_B,
      }),
    ]);
  });

  it('keeps human binding skip controls hidden without guidelines.adoption.manage', async () => {
    renderPanel();

    await screen.findByTestId('semantic-assessment-card');

    expect(
      screen.queryByRole('button', { name: 'Skip this binding' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('semantic-skips-toggle'),
    ).not.toBeInTheDocument();
    expect(policyApiMock.listSemanticPolicySkips).not.toHaveBeenCalled();
  });

  it('creates a REST-only human binding skip under guidelines.adoption.manage', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.adoption.manage',
    );

    renderPanel();

    fireEvent.click(
      await screen.findByRole('button', { name: 'Skip this binding' }),
    );
    const dialog = screen.getByRole('dialog', {
      name: 'Skip guideline binding',
    });
    expect(dialog).toHaveTextContent(
      'Agents cannot create or revoke it',
    );
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'Human-approved release exception.' },
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Create skip' }),
    );

    await waitFor(() => {
      expect(
        policyApiMock.createSemanticPolicySkip,
      ).toHaveBeenCalledWith(
        'board-1',
        {
          subject_type: 'spec',
          subject_id: 'spec-1',
          expected_subject_version: 7,
          binding_id: 'binding-1',
          reason: 'Human-approved release exception.',
        },
        expect.any(String),
      );
    });
  });

  it.each([
    ['without an assessment receipt', 'assessment_unavailable'],
    ['with an inadmissible assessment', 'assessment_inadmissible'],
  ] as const)(
    'creates a human skip from lifecycle authority %s',
    async (_label, cause) => {
      grant(
        'guidelines.assessments.read',
        'guidelines.adoption.manage',
      );
      policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
        page([]),
      );

      renderPanel({
        subjectVersion: 7,
        transitionPreview: blockedTransitionPreview(cause),
      });

      fireEvent.click(
        await screen.findByRole('button', {
          name: 'Skip this binding',
        }),
      );
      const dialog = screen.getByRole('dialog', {
        name: 'Skip guideline binding',
      });
      fireEvent.change(within(dialog).getByLabelText('Reason'), {
        target: { value: 'Human-approved transition exception.' },
      });
      fireEvent.click(
        within(dialog).getByRole('button', { name: 'Create skip' }),
      );

      await waitFor(() => {
        expect(
          policyApiMock.createSemanticPolicySkip,
        ).toHaveBeenCalledWith(
          'board-1',
          {
            subject_type: 'spec',
            subject_id: 'spec-1',
            expected_subject_version: 7,
            binding_id: 'binding-no-receipt',
            reason: 'Human-approved transition exception.',
          },
          expect.any(String),
        );
      });
    },
  );

  it('fails closed when lifecycle authority lacks a host subject revision', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.adoption.manage',
    );
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([]),
    );

    renderPanel({
      transitionPreview:
        blockedTransitionPreview('assessment_unavailable'),
    });

    const button = await screen.findByRole('button', {
      name: 'Skip this binding',
    });
    expect(button).toBeDisabled();
    expect(screen.getByText(
      /does not expose the authoritative subject revision/i,
    )).toBeInTheDocument();
    expect(
      policyApiMock.createSemanticPolicySkip,
    ).not.toHaveBeenCalled();
  });

  it('keeps the create idempotency key stable while retrying the same intent', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.adoption.manage',
    );
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([]),
    );
    policyApiMock.createSemanticPolicySkip
      .mockRejectedValueOnce(new Error('Temporary transport failure.'))
      .mockResolvedValueOnce({
        skip_id: 'skip-created',
        scope_digest: HASH_A,
        created_by: 'human-owner',
      });

    renderPanel({
      subjectVersion: 7,
      transitionPreview:
        blockedTransitionPreview('assessment_unavailable'),
    });

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Skip this binding',
      }),
    );
    const dialog = screen.getByRole('dialog', {
      name: 'Skip guideline binding',
    });
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'Retry this exact human intent.' },
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Create skip' }),
    );
    expect(
      await within(dialog).findByRole('alert'),
    ).toHaveTextContent('Temporary transport failure.');

    const firstIdempotencyKey =
      policyApiMock.createSemanticPolicySkip.mock.calls[0][2];
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Create skip' }),
    );

    await waitFor(() => {
      expect(
        policyApiMock.createSemanticPolicySkip,
      ).toHaveBeenCalledTimes(2);
    });
    expect(
      policyApiMock.createSemanticPolicySkip.mock.calls[1][2],
    ).toBe(firstIdempotencyKey);
  });

  it('revokes a current REST-only human binding skip under guidelines.adoption.manage', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.adoption.manage',
    );
    policyApiMock.listSemanticPolicySkips.mockResolvedValue(
      page([skip()]),
    );

    renderPanel();

    fireEvent.click(
      await screen.findByRole('button', { name: 'Revoke human skip' }),
    );
    const dialog = screen.getByRole('dialog', {
      name: 'Revoke guideline skip',
    });
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'The migration exception is complete.' },
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Revoke skip' }),
    );

    await waitFor(() => {
      expect(
        policyApiMock.revokeSemanticPolicySkip,
      ).toHaveBeenCalledWith(
        'board-1',
        'skip-1',
        {
          expected_skip_revision: 1,
          reason: 'The migration exception is complete.',
          idempotency_key: expect.any(String),
        },
      );
    });
  });

  it('revokes an active stale skip from the list without an assessment or enabled evaluation', async () => {
    grant(
      'guidelines.assessments.read',
      'guidelines.adoption.manage',
    );
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([]),
    );
    policyApiMock.listSemanticPolicySkips.mockResolvedValue(
      page([skip({ currentness: 'stale' })]),
    );

    renderPanel({ evaluationEnabled: false });

    fireEvent.click(
      await screen.findByTestId('semantic-skips-toggle'),
    );
    const skipList = screen.getByTestId('semantic-skips-content');
    fireEvent.click(
      within(skipList).getByRole('button', {
        name: 'Revoke human skip',
      }),
    );
    const dialog = screen.getByRole('dialog', {
      name: 'Revoke guideline skip',
    });
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'Revoke stale exception directly.' },
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Revoke skip' }),
    );

    await waitFor(() => {
      expect(
        policyApiMock.revokeSemanticPolicySkip,
      ).toHaveBeenCalledWith(
        'board-1',
        'skip-1',
        {
          expected_skip_revision: 1,
          reason: 'Revoke stale exception directly.',
          idempotency_key: expect.any(String),
        },
      );
    });
  });

  it('labels stale binding evidence and marks current inadmissible evidence explicitly', async () => {
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([
        assessment({
          receiptId: 'receipt-stale',
          bindingId: 'binding-stale',
          currentness: 'stale',
          currentnessReasons: ['subject_content_changed'],
        }),
        assessment({
          receiptId: 'receipt-inadmissible',
          bindingId: 'binding-inadmissible',
          confidence: 55,
          minimumConfidence: 80,
          assessorIndependent: false,
        }),
      ]),
    );

    renderPanel();

    const cards = await screen.findAllByTestId('semantic-assessment-card');
    const staleCard = cards.find((item) =>
      item.textContent?.includes('Binding binding-stale'),
    );
    const inadmissibleCard = cards.find((item) =>
      item.textContent?.includes('Binding binding-…ssible'),
    );
    expect(staleCard).toBeDefined();
    expect(staleCard).toHaveTextContent('stale');
    expect(staleCard).toHaveTextContent(
      'Stale: subject_content_changed.',
    );
    expect(inadmissibleCard).toBeDefined();
    expect(inadmissibleCard).toHaveTextContent(
      'Confidence is below the binding minimum',
    );
    expect(inadmissibleCard).toHaveTextContent(
      'Assessor separation was not satisfied',
    );
    expect(
      within(inadmissibleCard!).getByRole('img', {
        name: /Confidence score 55 out of 100.*threshold not met/i,
      }),
    ).toHaveAttribute('data-status', 'not-met');

    fireEvent.click(
      screen.getByTestId('policy-compliance-history-toggle'),
    );
    const history = screen.getByTestId(
      'policy-compliance-history-content',
    );
    expect(within(history).getByText('stale')).toBeInTheDocument();
  });
});

describe('guideline compliance summary', () => {
  it('shows a clear empty state when no guideline metric applies', async () => {
    renderPanel();

    expect(
      await screen.findByTestId('guideline-compliance-none'),
    ).toHaveTextContent('No guideline metric applies to this spec.');
  });

  it('renders adopted metrics with tooltip, enforcement badge and result', async () => {
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([
      {
        id: 'guideline-1',
        guideline: {
          id: 'guideline-1',
          title: 'Hexagonal architecture',
          content: 'Core declares WHAT; community provides HOW.',
          tags: [],
          scope: 'global',
          board_id: null,
          owner_id: 'owner-1',
          revision_id: 'guideline-1-revision-3',
          created_at: '2026-07-27T00:00:00Z',
          updated_at: '2026-07-27T00:00:00Z',
        },
        priority: 10,
        scope: 'global',
        binding_id: 'binding-1',
        binding_revision: 3,
        enforcement: 'advisory',
        minimum_confidence: 80,
        metric_threshold_overrides: {},
        binding_state: 'active',
        source_kind: 'native',
      },
    ]);
    policyApiMock.getGuidelineRevision.mockResolvedValue({
      revision: {
        metrics: [
          {
            metric_id: 'metric-1',
            code: 'architecture.segregation',
            title: 'Segregation',
            description: 'Business vs technical separation.',
            evaluation_rubric: 'Rubric.',
            target_entity_types: ['spec', 'card'],
            direction: 'minimum',
            default_threshold: 75,
          },
          {
            metric_id: 'metric-card-only',
            code: 'environment.runtime_provenance',
            title: 'Runtime provenance',
            description: 'Card-only metric must not appear for a spec.',
            evaluation_rubric: 'Rubric.',
            target_entity_types: ['card'],
            direction: 'minimum',
            default_threshold: 90,
          },
        ],
      },
    });

    renderPanel();

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(within(card).getByText('Hexagonal architecture')).toBeVisible();
    expect(
      within(card).getByTestId('compliance-enforcement-advisory'),
    ).toBeVisible();
    expect(within(card).getByText('V1 · Read-only')).toBeVisible();
    expect(
      within(card).getByTitle('Business vs technical separation.'),
    ).toHaveTextContent('Segregation');
    const ring = within(card).getByTestId('guideline-metric-ring-metric-1');
    expect(ring).toHaveAttribute('data-status', 'met');
    expect(within(card).getByText(/Minimum 75/)).toBeVisible();
    expect(
      within(card).queryByText('Runtime provenance'),
    ).not.toBeInTheDocument();
  });

  it('keeps lifecycle current evidence edition-scoped and loads previous editions lazily', async () => {
    const current = assessment({
      receiptId: 'receipt-current-edition-2',
      validationEdition: 2,
    });
    const previous = assessment({
      receiptId: 'receipt-previous-edition-1',
      validationEdition: null,
      currentness: 'stale',
      currentnessReasons: ['subject_version_changed'],
    });
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([adoptedGuideline()]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(
      guidelineRevisionFor('spec'),
    );
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: current,
    });
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([current, previous]),
    );

    renderPanel({
      subjectEdition: 2,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails(),
    });

    await screen.findByTestId('guideline-compliance-binding-1');
    expect(policyApiMock.listSemanticGuidelineAssessments).not.toHaveBeenCalled();
    [
      /stale/i,
      /receipt-/i,
      /head r/i,
      /subject v/i,
      /contract version/i,
    ].forEach((pattern) => {
      screen.queryAllByText(pattern).forEach((node) => {
        expect(node).not.toBeVisible();
      });
    });

    fireEvent.click(
      screen.getByTestId('policy-compliance-previous-results-toggle'),
    );
    const previousContent = await screen.findByTestId(
      'policy-compliance-previous-results-content',
    );
    expect(within(previousContent).getByText('Legacy')).toBeInTheDocument();
    expect(within(previousContent).queryByText('Edition 1')).not.toBeInTheDocument();
    expect(within(previousContent).queryByText(/receipt-/i)).not.toBeInTheDocument();
    expect(
      policyApiMock.getCurrentSemanticGuidelineAssessment,
    ).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      'binding-1',
      'detail',
      expect.any(AbortSignal),
      2,
    );
  });

  it('keeps same-edition evidence visible when live currentness marks it stale after a version-only change', async () => {
    const stale = assessment({
      receiptId: 'receipt-stale-edition-2',
      validationEdition: 2,
      currentness: 'stale',
      currentnessReasons: ['subject_version_changed'],
    });
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([adoptedGuideline()]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(
      guidelineRevisionFor('spec'),
    );
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: stale,
    });
    policyApiMock.listSemanticGuidelineAssessments.mockResolvedValue(
      page([stale]),
    );

    renderPanel({
      subjectEdition: 2,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails(),
    });

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(await within(card).findByTestId('guideline-confidence-binding-1'))
      .toBeVisible();
    expect(within(card).getByTestId('lifecycle-policy-status-passed'))
      .toHaveTextContent('Passed');
    expect(within(card).queryByText('No assessment recorded'))
      .not.toBeInTheDocument();
    expect(within(card).queryByText(/stale/i)).not.toBeInTheDocument();
    expect(within(card).queryByText(/receipt-/i)).not.toBeInTheDocument();
    expect(policyApiMock.listSemanticGuidelineAssessments).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByTestId('policy-compliance-previous-results-toggle'),
    );
    const previousContent = await screen.findByTestId(
      'policy-compliance-previous-results-content',
    );
    expect(within(previousContent).getByText(
      'No previous policy results are available.',
    )).toBeInTheDocument();
    expect(within(previousContent).queryByText('Edition 2')).not.toBeInTheDocument();
    expect(within(previousContent).queryByText(/receipt-/i)).not.toBeInTheDocument();
  });

  it.each([
    'ideation',
    'refinement',
    'spec',
    'card',
    'sprint',
  ] as const)(
    'projects the same read-only v2 confidence and actionable pinpoint in the %s surface',
    async (entityType) => {
      const subjectId = `${entityType}-1`;
      const navigate = vi.fn();
      dashboardApiMock.getBoardGuidelines.mockResolvedValue([{
        id: 'guideline-1',
        guideline: {
          id: 'guideline-1',
          title: 'Hexagonal architecture',
          content: 'Core declares WHAT; community provides HOW.',
          tags: [],
          scope: 'global',
          board_id: null,
          owner_id: 'owner-1',
          revision_id: 'guideline-1-revision-3',
          created_at: '2026-07-27T00:00:00Z',
          updated_at: '2026-07-27T00:00:00Z',
        },
        priority: 10,
        scope: 'global',
        binding_id: 'binding-1',
        binding_revision: 3,
        enforcement: 'advisory',
        minimum_confidence: 80,
        metric_threshold_overrides: {},
        binding_state: 'active',
        source_kind: 'native',
      }]);
      policyApiMock.getGuidelineRevision.mockResolvedValue({
        revision: {
          metrics: [{
            metric_id: 'metric-1',
            code: 'architecture.segregation',
            title: 'Segregation',
            description: 'Business vs technical separation.',
            evaluation_rubric: 'Rubric.',
            target_entity_types: [entityType],
            direction: 'minimum',
            default_threshold: 75,
          }],
        },
      });
      policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue(
        currentV2Assessment(entityType, subjectId),
      );

      renderPanel({
        entityType,
        subjectId,
        resolveSemanticAnchor: () => ({
          state: 'available',
          navigationTarget: `${entityType}:${subjectId}:technical_requirements`,
        }),
        onNavigateSemanticAnchor: navigate,
      });

      const card = await screen.findByTestId('guideline-compliance-binding-1');
      expect(
        within(card).getByTestId('guideline-confidence-ring-binding-1'),
      ).toHaveAttribute('data-status', 'met');
      expect(within(card).getByText('Passed')).toBeVisible();
      expect(within(card).getByText('v2')).toBeVisible();
      expect(within(card).getByText('Domain boundary is explicit')).toBeVisible();
      expect(within(card).getByTestId('actionable-pinpoint-metric'))
        .toHaveTextContent('Segregation');
      expect(within(card).getByTestId('actionable-pinpoint-target'))
        .toHaveTextContent(
          'Technical requirements: Runtime adapters remain outside the domain boundary. (technical_requirements)',
        );
      expect(within(card).queryByRole('spinbutton')).not.toBeInTheDocument();

      fireEvent.click(within(card).getByRole('button', {
        name: 'Go to location',
      }));
      expect(navigate).toHaveBeenCalledWith(
        `${entityType}:${subjectId}:technical_requirements`,
      );
      expect(
        policyApiMock.getCurrentSemanticGuidelineAssessment,
      ).toHaveBeenCalledWith(
        'board-1',
        entityType,
        subjectId,
        'binding-1',
        'detail',
        expect.any(AbortSignal),
        undefined,
      );
    },
  );

  it('renders a legacy policy pinpoint as metric, human target with stable ID, then rationale', async () => {
    const legacyMetric = metric({
      code: 'architecture.segregation',
    });
    legacyMetric.rationale = 'The requirement leaves the adapter boundary explicit.';
    legacyMetric.pinpoints = [{
      anchor_type: 'structured_child',
      anchor_ref: 'technical_requirements.tr_boundary',
      excerpt_hash: HASH_B,
      input_digest: HASH_A,
    }];
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([adoptedGuideline()]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(
      guidelineRevisionFor('spec'),
    );
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: assessment({
        validationEdition: 2,
        metricResults: [legacyMetric],
      }),
    });

    renderPanel({
      subjectEdition: 2,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails(),
      resolveSemanticAnchor: () => ({
        state: 'available',
        navigationTarget: 'spec:requirement:tr_boundary',
        displayText: 'TR-1: Runtime adapters remain outside the domain boundary.',
        stableReference: 'tr_boundary',
      }),
    });

    const card = await screen.findByTestId('actionable-pinpoint');
    expect(within(card).getByTestId('actionable-pinpoint-metric'))
      .toHaveTextContent('Segregation');
    expect(within(card).getByTestId('actionable-pinpoint-target'))
      .toHaveTextContent(
        'TR-1: Runtime adapters remain outside the domain boundary. (tr_boundary)',
      );
    expect(within(card).getByTestId('actionable-pinpoint-detail'))
      .toHaveTextContent(
        'The requirement leaves the adapter boundary explicit.',
      );
    expect(within(card).queryByText('Legacy assessment evidence'))
      .not.toBeInTheDocument();
    expect(within(card).queryByText('Legacy location'))
      .not.toBeInTheDocument();
    const content = card.textContent ?? '';
    expect(content.indexOf('Segregation')).toBeLessThan(
      content.indexOf('TR-1:'),
    );
    expect(content.indexOf('TR-1:')).toBeLessThan(
      content.indexOf('The requirement leaves'),
    );
  });

  it('uses a frozen binding without a receipt and never substitutes a later live revision', async () => {
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([{
      ...adoptedGuideline(),
      guideline: {
        ...adoptedGuideline().guideline,
        title: 'Later live architecture policy',
        revision_id: 'guideline-1-revision-9',
      },
    }]);
    policyApiMock.getGuidelineRevision.mockResolvedValue({
      revision: {
        metrics: [{
          ...guidelineRevisionFor('spec').revision.metrics[0],
          title: 'Later live metric',
          default_threshold: 99,
        }],
      },
    });
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'Assessment not found.',
        status: 404,
        kind: 'not_found',
        code: 'semantic_assessment_not_found',
      }),
    );

    renderPanel({
      subjectEdition: 4,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails([
        frozenBinding({
          revisionId: 'guideline-1-revision-3',
          title: 'Frozen architecture policy',
          enforcement: 'blocking',
          status: 'pending',
          metricTitle: 'Frozen segregation metric',
          threshold: 72,
        }),
      ]),
    });

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(within(card).getByText('Frozen architecture policy')).toBeVisible();
    expect(within(card).getByText('Frozen segregation metric')).toBeVisible();
    expect(within(card).getByText(/Minimum 72/)).toBeVisible();
    expect(within(card).getByText('No assessment recorded')).toBeVisible();
    expect(within(card).getByTestId('lifecycle-policy-status-pending'))
      .toHaveTextContent('Not assessed');
    expect(screen.queryByText(/Later live/)).not.toBeInTheDocument();
    expect(dashboardApiMock.getBoardGuidelines).not.toHaveBeenCalled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
    expect(policyApiMock.getCurrentSemanticGuidelineAssessment).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      'binding-1',
      'detail',
      expect.any(AbortSignal),
      4,
    );
  });

  it('shows frozen passed, skipped and advisory outcomes with their enforcement', async () => {
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'Assessment detail not found.',
        status: 404,
        kind: 'not_found',
        code: 'semantic_assessment_not_found',
      }),
    );
    renderPanel({
      subjectEdition: 5,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails([
        frozenBinding({
          bindingId: 'binding-passed',
          guidelineId: 'guideline-passed',
          revisionId: 'revision-passed',
          title: 'Passed policy',
          enforcement: 'blocking',
          status: 'passed',
          metricId: 'metric-passed',
          metricCode: 'quality.passed',
        }),
        frozenBinding({
          bindingId: 'binding-skipped',
          guidelineId: 'guideline-skipped',
          revisionId: 'revision-skipped',
          title: 'Skipped policy',
          enforcement: 'blocking',
          status: 'skipped',
          metricId: 'metric-skipped',
          metricCode: 'quality.skipped',
        }),
        frozenBinding({
          bindingId: 'binding-advisory',
          guidelineId: 'guideline-advisory',
          revisionId: 'revision-advisory',
          title: 'Advisory policy',
          enforcement: 'advisory',
          status: 'failed',
          metricId: 'metric-advisory',
          metricCode: 'quality.advisory',
        }),
      ]),
    });

    const passedCard = await screen.findByTestId(
      'guideline-compliance-binding-passed',
    );
    const skippedCard = screen.getByTestId(
      'guideline-compliance-binding-skipped',
    );
    const advisoryCard = screen.getByTestId(
      'guideline-compliance-binding-advisory',
    );
    expect(within(passedCard).getByTestId('lifecycle-policy-status-passed'))
      .toHaveTextContent('Passed');
    expect(within(passedCard).getByTestId('compliance-enforcement-blocking'))
      .toBeVisible();
    expect(within(skippedCard).getByTestId('lifecycle-policy-status-skipped'))
      .toHaveTextContent('Skipped');
    expect(within(skippedCard).getByTestId('policy-compliance-skipped'))
      .toHaveTextContent('authorized human');
    expect(within(advisoryCard).getByTestId('lifecycle-policy-status-failed'))
      .toHaveTextContent('Advisory finding');
    expect(within(advisoryCard).getByTestId('compliance-enforcement-advisory'))
      .toBeVisible();
    expect(within(advisoryCard).getByTestId('policy-compliance-advisory-note'))
      .toHaveTextContent('does not block validation');
    expect(dashboardApiMock.getBoardGuidelines).not.toHaveBeenCalled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
  });

  it('shows a fully waived binding as resolved and labels the waived finding', async () => {
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: assessment({
        validationEdition: 10,
        metricResults: [metric({
          score: 60,
          defaultThreshold: 75,
          effectiveThreshold: 75,
        })],
      }),
    });

    renderPanel({
      subjectEdition: 10,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails([
        frozenBinding({ status: 'waived', enforcement: 'blocking' }),
      ]),
      resolveSemanticAnchor: () => ({
        state: 'available',
        navigationTarget: 'spec:spec-1:description',
      }),
    });

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(within(card).getByTestId('lifecycle-policy-status-waived'))
      .toHaveTextContent('Waived');
    expect(within(card).getByTestId('policy-compliance-waived'))
      .toHaveTextContent(
        'All 1 failed metric finding is covered by an approved waiver for this edition.',
      );
    expect(within(card).getByTestId('lifecycle-policy-metric-outcome-metric-1'))
      .toHaveTextContent('Waiver active');
    expect(await within(card).findByTestId('actionable-pinpoint-badges'))
      .toHaveTextContent('Waiver active');
    expect(within(card).queryByRole('button', {
      name: 'View reassessment guidance',
    })).not.toBeInTheDocument();
  });

  it('shows partial waiver coverage without hiding the residual failed finding', async () => {
    const partial = frozenBinding({ status: 'failed', enforcement: 'blocking' });
    partial.failed_metric_count = 2;
    partial.waived_metric_count = 1;
    partial.unwaived_failed_metric_count = 1;
    partial.metrics = [
      {
        ...partial.metrics[0],
        assessment_outcome: 'waived',
      },
      {
        ...partial.metrics[0],
        metric_id: 'metric-2',
        code: 'architecture.residual',
        title: 'Residual finding',
        assessment_outcome: 'failed',
      },
    ];
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: assessment({
        validationEdition: 11,
        metricResults: [
          metric({
            score: 60,
            defaultThreshold: 75,
            effectiveThreshold: 75,
          }),
          metric({
            id: 'metric-2',
            resultId: 'metric-result-2',
            code: 'architecture.residual',
            score: 55,
            defaultThreshold: 75,
            effectiveThreshold: 75,
          }),
        ],
      }),
    });

    renderPanel({
      subjectEdition: 11,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails([partial]),
      resolveSemanticAnchor: () => ({
        state: 'available',
        navigationTarget: 'spec:spec-1:description',
      }),
    });

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(within(card).getByTestId('policy-compliance-partial-waiver'))
      .toHaveTextContent(
        '1 failed metric finding is covered by an approved waiver; 1 finding remains unresolved.',
      );
    expect(within(card).getByTestId('lifecycle-policy-metric-outcome-metric-1'))
      .toHaveTextContent('Waiver active');
    expect(within(card).getByTestId('lifecycle-policy-metric-outcome-metric-2'))
      .toHaveTextContent('Finding remains');
    const pinpointBadges = await within(card).findAllByTestId(
      'actionable-pinpoint-badges',
    );
    expect(pinpointBadges[0]).toHaveTextContent('Waiver active');
    expect(pinpointBadges[1]).toHaveTextContent('Current issue');
  });

  it.each(['revoked', 'expired'] as const)(
    'does not treat a %s live waiver record as frozen coverage',
    async (waiverStatus) => {
      grant('guidelines.assessments.read', 'guidelines.waiver.read');
      const inactiveWaiver: SemanticWaiverDetail = {
        ...waiver(),
        status: waiverStatus,
        validation_edition: 12,
        lifecycle_state: 'current',
        last_event_type: waiverStatus === 'revoked' ? 'revoke' : 'expire',
        revoked_by: waiverStatus === 'revoked' ? 'independent-reviewer' : null,
        revoked_at: waiverStatus === 'revoked'
          ? '2026-07-30T01:04:00Z'
          : null,
        expire_reason: waiverStatus === 'expired' ? 'scheduled_expiry' : null,
      };
      policyApiMock.listSemanticMetricWaivers.mockResolvedValue(
        page([inactiveWaiver]),
      );
      policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
        contract_version: 'v1',
        assessment: assessment({
          validationEdition: 12,
          metricResults: [metric({
            score: 60,
            defaultThreshold: 75,
            effectiveThreshold: 75,
          })],
        }),
      });

      renderPanel({
        subjectEdition: 12,
        presentationMode: 'lifecycle-edition',
        lifecycleSnapshot: frozenLifecycleDetails([
          frozenBinding({ status: 'failed', enforcement: 'blocking' }),
        ]),
        resolveSemanticAnchor: () => ({
          state: 'available',
          navigationTarget: 'spec:spec-1:description',
        }),
      });

      const card = await screen.findByTestId('guideline-compliance-binding-1');
      expect(await within(card).findByTestId('actionable-pinpoint-badges'))
        .toHaveTextContent('Current issue');
      expect(within(card).getByTestId('lifecycle-policy-status-failed'))
        .toHaveTextContent('Failed');
      expect(within(card).queryByText('Waiver active')).not.toBeInTheDocument();

      fireEvent.click(screen.getByText('Technical audit'));
      await waitFor(() => {
        expect(policyApiMock.listSemanticMetricWaivers).toHaveBeenCalled();
      });
      fireEvent.click(screen.getByTestId('semantic-waivers-toggle'));
      expect(await screen.findByText(
        `architecture.segregation · ${waiverStatus}`,
      )).toBeVisible();
      expect(within(card).getByTestId('actionable-pinpoint-badges'))
        .toHaveTextContent('Current issue');
      expect(within(card).queryByText('Waiver active')).not.toBeInTheDocument();
    },
  );

  it('renders v2 evidence against the same frozen lifecycle authority as v1', async () => {
    const v2 = currentV2Assessment('spec', 'spec-1');
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      ...v2,
      assessment: {
        ...v2.assessment,
        validation_edition: 6,
      },
    });

    renderPanel({
      subjectEdition: 6,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails(),
      resolveSemanticAnchor: () => ({
        state: 'available',
        navigationTarget: 'spec:spec-1:technical_requirements',
      }),
    });

    const card = await screen.findByTestId('guideline-compliance-binding-1');
    expect(within(card).getByTestId('lifecycle-policy-status-passed'))
      .toHaveTextContent('Passed');
    expect(within(card).getByText('Domain boundary is explicit')).toBeVisible();
    expect(within(card).getByTestId('guideline-metric-ring-metric-1'))
      .toHaveAttribute('data-status', 'met');
    expect(dashboardApiMock.getBoardGuidelines).not.toHaveBeenCalled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
  });

  it('signals when frozen policy text was bounded instead of hiding truncation', async () => {
    const current = assessment({ validationEdition: 9 });
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: current,
    });
    renderPanel({
      subjectEdition: 9,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: frozenLifecycleDetails([
        frozenBinding({
          descriptionTruncated: true,
          rubricTruncated: true,
        }),
      ]),
    });

    const signal = await screen.findByTestId(
      'guideline-metric-truncated-metric-1',
    );
    expect(signal).toHaveTextContent('Policy text excerpt truncated');
    expect(signal).toHaveAttribute(
      'title',
      'The frozen policy text was shortened for this view.',
    );
  });

  it('fails closed when the lifecycle snapshot is unavailable without loading live authority', async () => {
    renderPanel({
      subjectEdition: 7,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: null,
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The frozen policy scope for this edition could not be verified.',
    );
    expect(dashboardApiMock.getBoardGuidelines).not.toHaveBeenCalled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
    expect(policyApiMock.getCurrentSemanticGuidelineAssessment)
      .not.toHaveBeenCalled();
  });

  it('keeps verified frozen cards visible when another scope item is inconsistent', async () => {
    const snapshot = frozenLifecycleDetails();
    snapshot.counts.scope_inconsistent = 1;
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockResolvedValue({
      contract_version: 'v1',
      assessment: assessment({ validationEdition: 8 }),
    });

    renderPanel({
      subjectEdition: 8,
      presentationMode: 'lifecycle-edition',
      lifecycleSnapshot: snapshot,
    });

    expect(await screen.findByTestId('policy-compliance-scope-inconsistent'))
      .toHaveTextContent('1 frozen policy scope item is unavailable');
    expect(screen.getByTestId('guideline-compliance-binding-1'))
      .toHaveTextContent('Hexagonal architecture');
    expect(screen.getByTestId('policy-compliance-scope-inconsistent'))
      .toHaveTextContent('no current board policy was substituted');
    expect(dashboardApiMock.getBoardGuidelines).not.toHaveBeenCalled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
  });

  it('preserves the last valid v2 evidence and offers retry after a refresh error', async () => {
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([adoptedGuideline()]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(
      guidelineRevisionFor('spec'),
    );
    policyApiMock.getCurrentSemanticGuidelineAssessment
      .mockResolvedValueOnce(currentV2Assessment('spec', 'spec-1'))
      .mockRejectedValueOnce(new Error('temporary transport failure'));

    renderPanel({
      resolveSemanticAnchor: () => ({
        state: 'available',
        navigationTarget: 'spec:spec-1:technical_requirements',
      }),
    });

    expect(await screen.findByText('Domain boundary is explicit')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /last valid evidence remains visible/i,
    );
    expect(screen.getByText('Domain boundary is explicit')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  it('renders a no-assessment state with reassessment guidance and no score input', async () => {
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([adoptedGuideline()]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(
      guidelineRevisionFor('spec'),
    );
    policyApiMock.getCurrentSemanticGuidelineAssessment.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'Assessment not found.',
        status: 404,
        kind: 'not_found',
        code: 'semantic_assessment_not_found',
      }),
    );

    renderPanel();

    expect(
      await screen.findByTestId('policy-compliance-no-assessment'),
    ).toHaveTextContent('Scores cannot be entered here.');
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {
      name: 'View reassessment guidance',
    }));
    expect(await screen.findByText(
      /Reassessment is performed by an independent agent/i,
    )).toBeVisible();
  });
});

describe('guideline authority loader stability', () => {
  it('fetches board guidelines exactly once per subject (no request loop)', async () => {
    const { rerender } = renderPanel();

    expect(
      await screen.findByTestId('guideline-compliance-none'),
    ).toBeVisible();

    rerender(
      <PolicyCompliancePanel
        boardId="board-1"
        entityType="spec"
        subjectId="spec-1"
      />,
    );
    await waitFor(() => {
      expect(dashboardApiMock.getBoardGuidelines).toHaveBeenCalledTimes(1);
    });
  });
});
