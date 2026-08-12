import { describe, expect, it } from 'vitest';

import {
  parseSemanticAssessmentDetail,
  parseCreatedSemanticSkipResponse,
  parseSemanticDetailPage,
  parseSemanticFindingDetail,
  parseSemanticSkipDetail,
  parseSemanticWaiverDetail,
  parseRequestedSemanticWaiverResponse,
  parseRevokedSemanticSkipResponse,
  semanticMetricDirection,
} from '../semanticPolicyModel';

const digest = 'a'.repeat(64);
const expected = {
  boardId: 'board-1',
  entityType: 'spec' as const,
  subjectId: 'spec-1',
};

function evidence() {
  return {
    source_type: 'spec',
    source_id: 'spec-1',
    source_version: 7,
    content_hash: digest,
  };
}

function pinpoint() {
  return {
    anchor_type: 'field',
    anchor_ref: 'architecture',
    excerpt_hash: digest,
    input_digest: digest,
  };
}

function assessment(overrides: Record<string, unknown> = {}) {
  return {
    projection: 'detail',
    receipt_id: 'receipt-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    validation_edition: null,
    binding_id: 'binding-1',
    guideline_id: 'guideline-1',
    guideline_revision_id: 'revision-1',
    enforcement: 'blocking',
    state: 'passed',
    currentness: 'current',
    currentness_reasons: [],
    confidence: 90,
    minimum_confidence: 80,
    metric_count: 1,
    failed_metric_count: 0,
    recorded_at: '2026-07-30T12:00:00Z',
    binding_revision: 2,
    assessor_agent_id: 'agent-1',
    assessor_model_id: 'model-1',
    assessor_independent: true,
    confidence_admissible: true,
    metric_results: [{
      metric_result_id: 'result-1',
      metric_id: 'metric-1',
      metric_code: 'Title.Clarity:v2',
      score: 85,
      direction: 'minimum',
      default_threshold: 70,
      effective_threshold: 80,
      threshold_source: 'override',
      outcome: 'pass',
      rationale: 'The architecture boundary is explicit.',
      evidence_refs: [evidence()],
      pinpoints: [pinpoint()],
    }],
    ...overrides,
  };
}

function finding(overrides: Record<string, unknown> = {}) {
  return {
    projection: 'detail',
    finding_id: 'finding-1',
    receipt_id: 'receipt-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    guideline_id: 'guideline-1',
    guideline_revision_id: 'revision-1',
    binding_id: 'binding-1',
    metric_id: 'metric-1',
    metric_code: 'Title.Clarity:v2',
    currentness: 'current',
    currentness_reasons: [],
    created_at: '2026-07-30T12:00:00Z',
    metric_result_id: 'result-1',
    binding_revision: 2,
    rationale: 'Boundary evidence is incomplete.',
    evidence_refs: [evidence()],
    pinpoints: [pinpoint()],
    ...overrides,
  };
}

function waiver(overrides: Record<string, unknown> = {}) {
  return {
    projection: 'detail',
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
    metric_code: 'Title.Clarity:v2',
    status: 'requested',
    waiver_revision: 1,
    currentness: 'current',
    currentness_reasons: [],
    requested_at: '2026-07-30T12:00:00Z',
    expires_at: null,
    last_event_type: 'request',
    last_event_at: '2026-07-30T12:00:00Z',
    justification: 'Temporary migration constraint.',
    requested_by: 'human-1',
    original_expires_at: null,
    reviewed_by: null,
    reviewed_at: null,
    review_reason: null,
    revoked_by: null,
    revoked_at: null,
    expire_reason: null,
    evidence_refs: [evidence()],
    ...overrides,
  };
}

function skip(overrides: Record<string, unknown> = {}) {
  return {
    projection: 'detail',
    skip_id: 'skip-1',
    board_id: 'board-1',
    entity_type: 'spec',
    subject_id: 'spec-1',
    subject_version: 7,
    guideline_id: 'guideline-1',
    guideline_revision_id: 'revision-1',
    binding_id: 'binding-1',
    status: 'active',
    skip_revision: 1,
    currentness: 'current',
    currentness_reasons: [],
    created_at: '2026-07-30T12:00:00Z',
    last_event_type: 'create',
    last_event_at: '2026-07-30T12:00:00Z',
    binding_revision: 2,
    reason: 'Human-approved temporary exception.',
    created_by: 'human-1',
    revoked_by: null,
    revoked_at: null,
    revocation_reason: null,
    ...overrides,
  };
}

describe('semanticPolicyModel', () => {
  it('accepts a complete detail assessment and maps metric direction', () => {
    const parsed = parseSemanticAssessmentDetail(assessment(), expected);

    expect(parsed.metric_results[0].metric_code).toBe('Title.Clarity:v2');
    expect(semanticMetricDirection('minimum')).toBe('higher-is-better');
    expect(semanticMetricDirection('maximum')).toBe('lower-is-better');
  });

  it('requires the exact validation edition for lifecycle current evidence', () => {
    const lifecycleExpectation = { ...expected, validationEdition: 2 };

    expect(() => parseSemanticAssessmentDetail(
      assessment({ validation_edition: null }),
      lifecycleExpectation,
    )).toThrow('active validation edition');
    expect(() => parseSemanticAssessmentDetail(
      assessment({ validation_edition: 1 }),
      lifecycleExpectation,
    )).toThrow('active validation edition');
    expect(parseSemanticAssessmentDetail(
      assessment({ validation_edition: 2 }),
      lifecycleExpectation,
    ).validation_edition).toBe(2);
  });

  it.each([
    ['unknown field', { unexpected: true }],
    ['mismatched subject', { subject_id: 'spec-2' }],
    ['invalid timestamp', { recorded_at: 'not-a-time' }],
    ['contradictory confidence', { confidence_admissible: false }],
    ['contradictory counts', { failed_metric_count: 1 }],
  ])('rejects assessment %s', (_label, override) => {
    expect(() =>
      parseSemanticAssessmentDetail(assessment(override), expected),
    ).toThrow();
  });

  it('rejects a metric outcome that contradicts score and threshold', () => {
    const value = assessment();
    value.metric_results[0].outcome = 'fail';

    expect(() => parseSemanticAssessmentDetail(value, expected)).toThrow(
      /contradicts/u,
    );
  });

  it('rejects duplicate evidence and malformed metric codes', () => {
    const duplicate = assessment();
    duplicate.metric_results[0].evidence_refs.push(evidence());
    expect(() => parseSemanticAssessmentDetail(duplicate, expected)).toThrow(
      /duplicates/u,
    );

    const malformed = assessment();
    malformed.metric_results[0].metric_code = '1 invalid';
    expect(() => parseSemanticAssessmentDetail(malformed, expected)).toThrow(
      /metric code/u,
    );
  });

  it('validates detail finding, waiver and skip projections', () => {
    expect(parseSemanticFindingDetail(finding(), expected).finding_id)
      .toBe('finding-1');
    expect(parseSemanticWaiverDetail(waiver(), expected).waiver_id)
      .toBe('waiver-1');
    expect(parseSemanticSkipDetail(skip(), expected).skip_id)
      .toBe('skip-1');
  });

  it('rejects partial waiver and contradictory skip lifecycle evidence', () => {
    expect(() =>
      parseSemanticWaiverDetail(
        waiver({ reviewed_by: 'reviewer-1' }),
        expected,
      ),
    ).toThrow(/partial/u);
    expect(() =>
      parseSemanticSkipDetail(
        skip({ status: 'revoked', last_event_type: 'revoke' }),
        expected,
      ),
    ).toThrow(/inconsistent/u);
  });

  it('parses an opaque detail cursor page without exposing its cursor value', () => {
    const parsed = parseSemanticDetailPage(
      {
        items: [assessment()],
        projection: 'detail',
        next_cursor: 'opaque-token',
        has_more: true,
      },
      (item) => parseSemanticAssessmentDetail(item, expected),
      25,
    );

    expect(parsed).toMatchObject({
      limit: 25,
      has_more: true,
      next_cursor: 'opaque-token',
    });
  });

  it.each([
    {
      items: [],
      projection: 'detail',
      next_cursor: 'cursor',
      has_more: true,
    },
    {
      items: [],
      projection: 'detail',
      next_cursor: 'cursor',
      has_more: false,
    },
    {
      items: [],
      projection: 'summary',
      next_cursor: null,
      has_more: false,
    },
    {
      items: [],
      projection: 'detail',
      next_cursor: null,
      has_more: false,
      unknown: true,
    },
  ])('rejects malformed or cross-projection cursor pages', (page) => {
    expect(() =>
      parseSemanticDetailPage(page, (item) => item, 25),
    ).toThrow();
  });

  it('validates closed waiver and skip mutation acknowledgements', () => {
    expect(parseRequestedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      status: 'requested',
      scope_digest: digest,
    }).waiver_id).toBe('waiver-1');
    expect(parseCreatedSemanticSkipResponse({
      skip_id: 'skip-1',
      scope_digest: digest,
      created_by: 'human-1',
    }).created_by).toBe('human-1');
    expect(parseRevokedSemanticSkipResponse({
      skip_id: 'skip-1',
      skip_revision: 2,
      status: 'revoked',
      revoked_by: 'human-1',
      replayed: false,
    }).skip_revision).toBe(2);
  });

  it('rejects unknown and partial mutation acknowledgement fields', () => {
    expect(() => parseRequestedSemanticWaiverResponse({
      waiver_id: 'waiver-1',
      status: 'requested',
      scope_digest: digest,
      unknown: true,
    })).toThrow(/unknown or missing/u);
    expect(() => parseCreatedSemanticSkipResponse({
      skip_id: 'skip-1',
      scope_digest: 'not-a-digest',
      created_by: 'human-1',
    })).toThrow(/scope digest/u);
    expect(() => parseRevokedSemanticSkipResponse({
      skip_id: 'skip-1',
      skip_revision: 2,
      status: 'active',
      revoked_by: 'human-1',
      replayed: false,
    })).toThrow(/state/u);
  });
});
